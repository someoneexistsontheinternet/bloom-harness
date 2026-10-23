"""Receipted research projections; full evidence never depends on prompt size."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from .store import now
from .web_document import result_document


class ResearchViews:
    def __init__(self, results=None, store=None):
        self.results = results
        self.store = store or results.store
        self.settings = self.store.config.data.get("research", {})

    def preflight(self, agent, task, operation, arguments):
        role = self.store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"]
        if role == "luna" or self.store.one(
            "SELECT 1 FROM web_runs WHERE operation_id=?", (operation,)
        ):
            return None
        used = self.store.one(
            "SELECT coalesce(sum(text_tokens),0) n FROM research_views WHERE task_id=? AND agent_id=?",
            (task, agent),
        )["n"]
        budget = self.settings.get(role + "_tokens", 18000 if role == "astra" else 64000)
        expansion = arguments.get("evidence_expansion")
        if expansion is not None and (not isinstance(expansion, str) or not expansion.strip()):
            raise ValueError("Evidence expansion needs the specific unresolved question")
        if used < budget or expansion:
            return None
        index = self.store.put(
            {
                "task": task,
                "receipts": self.store.rows(
                    "SELECT operation_id,kind,query,status,result_hash FROM web_runs WHERE task_id=? ORDER BY created_at",
                    (task,),
                ),
            }
        )
        from .evidence_grants import give

        give(self.store, agent, agent, task, index)
        return {
            "status": "evidence_budget_reached",
            "saved_evidence_index": index,
            "instruction": "No new capture or review was purchased. Synthesize existing evidence or inspect an exact saved range. If another source is essential, state the specific unresolved question in evidence_expansion; that explicit expansion is receipted.",
        }

    def applies(self, agent, call, raw):
        if self.store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"] == "luna":
            return False
        if call["name"] in ("web_search", "web_open", "web_fetch"):
            return True
        path = raw.get("path") if isinstance(raw, dict) else None
        return (
            call["name"] == "read_file"
            and path
            and Path(path).is_relative_to(self.store.config.artifacts / "runtime/web")
            and "_image" not in raw
        )

    def compact(self, raw):
        return result_document(raw)

    async def present(self, agent, task, call, raw, digest):
        old = self.store.one(
            "SELECT * FROM research_views WHERE operation_id=?", (call["operation_id"],)
        )
        if old:
            if old["source_hash"] != digest:
                raise ValueError("Research projection source changed")
            return self.store.get(old["view_hash"])
        role = self.store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"]
        budget = self.settings.get(role + "_tokens", 18000 if role == "astra" else 64000)
        used = self.store.one(
            "SELECT coalesce(sum(text_tokens),0) n FROM research_views WHERE task_id=? AND agent_id=?",
            (task, agent),
        )["n"]
        remaining = max(0, budget - used)
        args = self.store.get(call["arguments_hash"])
        if args.get("evidence_expansion"):
            remaining = max(remaining, self.settings.get("result_tokens", 6000))
        exact_range = (
            call["name"] == "read_file"
            and (args.get("start") is not None or args.get("end") is not None)
            and len(raw.get("text", "")) <= 8000
        )
        explicit_raw = call["name"] == "read_file" and args.get("exact") is True
        view = self.compact(raw)
        limit = min(self.settings.get("result_tokens", 6000), remaining)
        threshold = self.results.threshold
        from .results import text_of

        needs_review = len(text_of(raw)[0]) > threshold or self.results.count(view) > limit
        if needs_review and not exact_range and not explicit_raw:
            view = await self.results._present(
                agent,
                task,
                call,
                raw,
                digest,
                force=True,
                working_characters=min(
                    self.settings.get("summary_characters", 12000), max(1500, remaining * 3)
                ),
                deferred=remaining < 512,
            )
        else:
            view = (
                await self.results._present(agent, task, call, view, digest)
                if not exact_range
                else view
            )
        image_policy = self.choose_images(task, agent, call, raw, args)
        if isinstance(view, dict):
            view = {
                k: v for k, v in view.items() if k not in ("_image", "_images", "_mime", "_detail")
            }
            if isinstance(raw, dict) and raw.get("_images") and not image_policy["hashes"]:
                view["rendered_available"] = [
                    {k: i.get(k) for k in ("hash", "path", "width", "height", "start_y", "end_y")}
                    for i in raw["_images"]
                ]
                view["visual_note"] = (
                    "Captured renders remain available. Use read_file/view_image or include_image=true when visual evidence is needed."
                )
            view["evidence_budget"] = {
                "used_before": used,
                "budget": budget,
                "explicit_exact_range": bool(exact_range or explicit_raw),
                "expansion_reason": args.get("evidence_expansion"),
            }
        tokens = self.results.count(view)
        self.store.db.execute(
            "INSERT INTO research_views VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                call["operation_id"],
                task,
                agent,
                digest,
                self.store.put(view),
                self.store.put(image_policy),
                tokens,
                len(image_policy["hashes"]),
                budget,
                now(),
            ),
        )
        return view

    def choose_images(self, task, agent, call, raw, args):
        images = raw.get("_images", []) if isinstance(raw, dict) else []
        explicit = args.get("include_image") is True
        if args.get("include_image") is False:
            return {"hashes": [], "explicit": False}
        owner = self.store.one(
            "SELECT i.text FROM task_owner_turns t JOIN owner_turns o ON o.id=t.turn_id JOIN items i ON i.id=o.source_item WHERE t.task_id=?",
            (task,),
        )
        question = owner["text"] if owner else ""
        url = args.get("url", "")
        parsed = urlparse(url)
        owner_page = bool(
            url and (url in question or (parsed.netloc + parsed.path).rstrip("/") in question)
        )
        if call["name"] in ("web_open", "web_fetch") and not (explicit or owner_page):
            return {"hashes": [], "explicit": False}
        used = self.store.one(
            "SELECT coalesce(sum(image_count),0) n FROM research_views WHERE task_id=? AND agent_id=?",
            (task, agent),
        )["n"]
        allowance = (
            len(images)
            if explicit or owner_page
            else max(0, self.settings.get("visual_sources", 2) - used)
        )
        return {
            "hashes": [i["hash"] for i in images[:allowance]],
            "explicit": explicit or owner_page,
        }

    def images(self, agent, call, raw):
        if not self.applies(agent, call, raw):
            return {k: raw[k] for k in ("_image", "_images", "_mime", "_detail") if k in raw}
        saved = self.store.one(
            "SELECT image_policy_hash FROM research_views WHERE operation_id=?",
            (call["operation_id"],),
        )
        if not saved:
            return {}  # Failed projection never falls back to all images.
        policy = self.store.get(saved["image_policy_hash"])
        return {"_images": [i for i in raw.get("_images", []) if i["hash"] in policy["hashes"]]}
