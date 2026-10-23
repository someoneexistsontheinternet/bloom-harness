from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import tiktoken

from .privacy import clean
from .store import atomic, now, uid


class Context:
    def __init__(self, store, models):
        self.store = store
        self.models = models
        self.tokenizer = tiktoken.get_encoding("o200k_base")
        self.members = {}
        from .memory import Memory

        self.history = Memory(store, models, self.count)

    def count(self, value):
        from .token_estimate import Estimator

        return Estimator(self.store).measure(value, self.store.config.profile("astra")["model"])[
            "tokens"
        ]

    def request_size(self, agent, fixed, tools, items):
        from .token_estimate import Estimator

        role = self.store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"]
        return Estimator(self.store).request(
            agent, fixed, tools, items, self.store.config.profile(role)["model"]
        )["tokens"]

    def selected(self, agent, task=None):
        context = self.store.one(
            "SELECT * FROM contexts WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (agent,)
        )
        if not context:
            rows = self.store.rows("SELECT * FROM items WHERE session_id=? ORDER BY id", (agent,))
        else:
            rows = self.store.rows(
                "SELECT i.* FROM items i JOIN context_members m ON m.item_id=i.id WHERE m.context_id=? ORDER BY m.position",
                (context["id"],),
            ) + self.store.rows(
                "SELECT * FROM items WHERE session_id=? AND id>? AND id!=? ORDER BY id",
                (agent, context["through_item"], context["summary_item"]),
            )
        if self.history.enabled(agent):
            packets = {
                r["item_id"]
                for r in self.store.rows(
                    "SELECT item_id FROM memory_packets WHERE agent_id=?", (agent,)
                )
            }
            rows = [
                r
                for r in rows
                if r["id"] not in packets or (context and r["id"] == context["summary_item"])
            ]
        if task is not None:
            queued = {
                r["id"]
                for r in self.store.rows(
                    "SELECT id FROM tasks WHERE agent_id=? AND id!=? AND status='ready' AND turns=0",
                    (agent, task),
                )
            }
            rows = [r for r in rows if r["task_id"] not in queued]
            # A queued objective can predate a compaction of other work. It was
            # deliberately excluded then and must still reach its own decision.
            included = {r["id"] for r in rows}
            missing = self.store.rows(
                "SELECT i.* FROM items i WHERE i.task_id=? AND i.role='user' AND NOT EXISTS (SELECT 1 FROM model_call_items m JOIN model_calls c ON c.id=m.call_id WHERE m.item_id=i.id AND c.task_id=i.task_id AND c.status='completed' AND c.purpose IN ('turn','compaction')) AND NOT EXISTS (SELECT 1 FROM model_call_sources s JOIN model_calls c ON c.id=s.call_id WHERE s.item_id=i.id AND c.task_id=i.task_id AND c.status='completed' AND c.purpose='compaction') ORDER BY i.id",
                (task,),
            )
            rows.extend(r for r in missing if r["id"] not in included)
            if context and self.history.enabled(agent):
                covered = {
                    r["item_id"]
                    for r in self.store.rows(
                        "SELECT item_id FROM memory_context_coverage WHERE context_id=?",
                        (context["id"],),
                    )
                }
                rows = [
                    r for r in rows if r["id"] not in covered or r["id"] == context["summary_item"]
                ]
        if self.history.enabled(agent):
            # A queued message may have an old item ID but only become visible
            # now. Preserve the order actually sent, then append newly visible
            # items; never insert the queued input into an already cached prefix.
            previous = self.store.one(
                "SELECT c.id FROM model_calls c WHERE c.agent_id=? AND c.purpose='turn' AND EXISTS (SELECT 1 FROM model_call_items m WHERE m.call_id=c.id) ORDER BY c.rowid DESC LIMIT 1",
                (agent,),
            )
            if previous:
                by_id = {r["id"]: r for r in rows}
                prior = [
                    r["item_id"]
                    for r in self.store.rows(
                        "SELECT item_id FROM model_call_items WHERE call_id=? ORDER BY position",
                        (previous["id"],),
                    )
                ]
                prior_ids = set(prior)
                rows = [by_id[i] for i in prior if i in by_id] + [
                    r for r in rows if r["id"] not in prior_ids
                ]
        return rows

    async def compose(self, agent, task, tools, force=False):
        row = self.store.one("SELECT * FROM agents WHERE id=?", (agent,))
        profile = self.store.config.profile(row["role"])
        rows = self.selected(agent, task)
        fixed = self.instructions(row, task)
        if self.history.enabled(agent):
            return await self.compose_memory(agent, task, tools, fixed, force)
        limit = min(profile["compact_at"], profile["context_limit"] - profile["output_reserve"])
        if (
            force
            or self.request_size(
                agent, fixed, tools, [self.store.get(x["envelope_hash"]) for x in rows]
            )
            > limit
        ):
            budget = profile["compact_to"] - self.count([fixed, tools])
            if budget <= 0:
                raise RuntimeError(
                    "Fixed instructions/tools exceed the compaction target; adjust the explicit profile"
                )
            await self.compact(agent, task, rows, budget)
            rows = self.selected(agent, task)
        items = [self.store.get(x["envelope_hash"]) for x in rows]
        self.members[agent] = [x["id"] for x in rows]
        if (
            self.request_size(agent, fixed, tools, items)
            > profile["context_limit"] - profile["output_reserve"]
        ):
            raise RuntimeError(
                "Context still exceeds the configured window; exact records retained for a deliberate smaller selection"
            )
        return fixed, items

    async def compose_memory(self, agent, task, tools, fixed, force=False):
        self.history.index(agent)
        target = self.store.config.data["memory"]["context_tokens"]
        profile = self.store.config.profile("astra")
        limit = min(profile["compact_at"], profile["context_limit"] - profile["output_reserve"])
        reserved = self.count([fixed, tools])
        if not self.store.one("SELECT 1 FROM contexts WHERE session_id=?", (agent,)):
            self.history.begin(agent, task, target - reserved)
        rows = self.selected(agent, task)
        pending = self.store.one(
            "SELECT id FROM memory_compactions WHERE agent_id=? AND status='pending'", (agent,)
        )
        size = self.request_size(
            agent, fixed, tools, [self.store.get(r["envelope_hash"]) for r in rows]
        )
        # The snapshot and sent history are immutable until this boundary.
        # Store consolidation never patches the active input behind the model.
        if force or pending or size > limit:
            try:
                await self.compact(
                    agent, task, rows, target - reserved, retry=bool(force or pending)
                )
            except (InterruptedError, asyncio.CancelledError):
                raise
            except Exception as exc:
                # Compaction is useful maintenance, not permission to discard
                # history or block an answer while it still fits the real window.
                notice = (
                    "Working-context compaction deferred: "
                    + str(exc)
                    + ". Previous history and accepted memory retained."
                )
                self.store.item(
                    agent,
                    {"role": "user", "content": notice},
                    task,
                    key=task + ":compaction-deferred",
                    origin="runtime_notice",
                )
                self.store.db.execute(
                    "UPDATE memory_compactions SET status='failed' WHERE agent_id=? AND status='pending'",
                    (agent,),
                )
            rows = self.selected(agent, task)
        items = [self.store.get(r["envelope_hash"]) for r in rows]
        if (
            self.request_size(agent, fixed, tools, items)
            > profile["context_limit"] - profile["output_reserve"]
        ):
            raise RuntimeError(
                "Working context exceeds the configured model window after deferred compaction; originals retained"
            )
        self.members[agent] = [r["id"] for r in rows]
        return fixed, items

    def instructions(self, agent, task):
        prompt = (self.store.config.source / "prompts" / (agent["role"] + ".md")).read_text()
        scope = f"\nYour agent id is {agent['id']}. Assigned workspace: {agent['cwd']}. Permission mode: {agent['mode']}. Network: {bool(agent['network'])}."
        worktree = self.store.one(
            "SELECT w.repository FROM worktrees w JOIN tasks t ON t.id=w.task_id WHERE t.agent_id=?",
            (agent["id"],),
        )
        scope += "\nProject interpreter: " + str(
            self.store.config.project_python(
                agent["cwd"], worktree["repository"] if worktree else None
            )
        )
        if agent["mode"] != "read":
            controls = self.store.config.workspace / "authoring" / agent["id"]
            controls.mkdir(parents=True, exist_ok=True)
            scope += "\nAuthor TOML control files here, outside Git submissions: " + str(controls)
        common = (self.store.config.source / "prompts/common.md").read_text()
        recall = self.store.one(
            "SELECT parent_task FROM memory_retrievals WHERE task_id=? AND agent_id=?",
            (task, agent["id"]),
        )
        if recall:
            from .recall import PROMPT

            prompt = PROMPT
        evidence = self.store.one("SELECT 1 FROM evidence_workers WHERE task_id=?", (task,))
        if evidence:
            from .evidence import PROMPT

            prompt = PROMPT
        # Durable task changes are user/system items, not mutable cache-prefix instructions.
        memory_text = ""
        if not self.history.enabled(agent["id"]) and not recall and not evidence:
            objective = self.store.one("SELECT objective FROM tasks WHERE id=?", (task,))[
                "objective"
            ]
            memory_text = (
                "\nSelected accepted memories (global plus relevant topics; originals remain in the memory files):\n"
                + json.dumps(self.history.accepted_memories(objective), ensure_ascii=False)
            )
        paths = [Path.home() / ".codex/AGENTS.md"]
        cwd = Path(agent["cwd"]).resolve()
        paths += [p / "AGENTS.md" for p in reversed((cwd, *cwd.parents))]
        guidance = []
        seen = set()
        for path in paths:
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            guidance.append(str(path) + ":\n" + path.read_text())
        return (
            common
            + "\n"
            + prompt
            + "\nApplicable project instructions (already read):\n"
            + "\n\n".join(guidance)
            + scope
            + memory_text
        )

    async def compact(self, agent, task, rows=None, target=None, *, retry=False):
        rows = rows if rows is not None else self.selected(agent, task)
        if self.history.enabled(agent):
            from .working_context import compact

            return await compact(
                self,
                task,
                rows,
                target or self.store.config.data["memory"]["context_tokens"],
                retry=retry,
            )
        if len(rows) < 4:
            return None
        role = self.store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"]
        target = target or self.store.config.profile(role)["compact_to"]
        # Keep complete tool/result groups. Never cut between a call and its result.
        desired = max(1, target // 2)
        start = len(rows)
        tokens = 0
        while start and tokens < desired:
            start -= 1
            tokens += self.count(self.store.get(rows[start]["envelope_hash"]))
        call_positions = {
            row["call_id"]: index
            for index, row in enumerate(rows)
            if row["kind"] == "function_call"
        }
        first_response_item = {}
        for index, row in enumerate(rows):
            if row["model_call_id"]:
                first_response_item.setdefault(row["model_call_id"], index)
        # A long task need not contain another user message. Cut at a settled
        # native response/tool group, not only at a whole human turn boundary.
        while start:
            safe = start
            for index, row in enumerate(rows[start:], start):
                if row["kind"] == "function_call_output" and row["call_id"] in call_positions:
                    safe = min(safe, call_positions[row["call_id"]])
                if row["model_call_id"]:
                    safe = min(safe, first_response_item[row["model_call_id"]])
            if safe == start:
                break
            start = safe
        if start == 0:
            return None
        old, recent = rows[:start], rows[start:]
        tokens = self.count([self.store.get(r["envelope_hash"]) for r in recent])
        # Summarize only the old prefix, leaving all originals untouched.
        material = []
        for row in old:
            item = {
                k: row[k]
                for k in (
                    "id",
                    "kind",
                    "role",
                    "phase",
                    "call_id",
                    "tool_name",
                    "text",
                    "envelope_hash",
                )
            }
            item["origin"] = self.store.one(
                "SELECT kind,source_task,source_agent FROM item_origins WHERE item_id=?",
                (row["id"],),
            )
            if row["kind"] == "function_call":
                item["arguments"] = self.store.get(row["envelope_hash"]).get("arguments")
            material.append(item)
        recent_tasks = self.store.rows(
            "SELECT id,objective,plan,status FROM tasks WHERE agent_id=? AND (id=? OR status!='ready' OR turns>0) ORDER BY created_at DESC LIMIT 3",
            (agent, task),
        )
        activities = self.store.rows(
            "SELECT e.id,e.kind,e.detail_hash FROM events e LEFT JOIN tasks t ON t.id=e.task_id WHERE e.agent_id=? AND (t.id IS NULL OR t.id=? OR t.status!='ready' OR t.turns>0) ORDER BY e.id DESC LIMIT 5",
            (agent, task),
        )
        for activity in activities:
            activity["detail"] = self.store.get(activity["detail_hash"])
        instructions = "Summarize this older context for its same agent. Preserve original user goals and corrections, rationale, source IDs, unfinished work and uncertain effects. Keep concise summaries of the five recent activities and the three recent task rollouts. Distinguish measured evidence from proposals. Do not perform actions. Return concise Markdown."
        evidence = {
            "older_context": material,
            "recent_tasks": recent_tasks,
            "recent_activities": activities,
        }
        # This is one derived evidence envelope, not the original native items.
        # Its body retains every source ID/hash; do not claim a one-to-one
        # model_call_items mapping between many originals and this single item.
        call, response = await self.models.infer(
            agent,
            task,
            instructions,
            [{"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}],
            [],
            purpose="compaction",
        )
        self.store.event(
            "context.compaction_sources",
            {"call": call, "item_ids": [r["id"] for r in old]},
            task,
            agent,
        )
        summary = "\n".join(
            p["text"]
            for item in response["output"]
            if item.get("type") == "message"
            for p in item.get("content", [])
            if "text" in p
        )
        if not summary.strip() or self.count(summary) > max(1024, target - tokens):
            raise RuntimeError("Compaction summary did not fit; old context remains active")
        prior = self.store.one(
            "SELECT id FROM contexts WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (agent,)
        )
        with self.store.transaction():
            self.store.db.executemany(
                "INSERT OR IGNORE INTO model_call_sources VALUES(?,?,?)",
                [(call, row["id"], i) for i, row in enumerate(old)],
            )
            summary_id = self.store.item(
                agent,
                {
                    "role": "user",
                    "content": "Derived context summary (originals remain in SQLite):\n" + summary,
                },
                task,
                call,
                origin="compaction",
                source_task=task,
                source_agent=agent,
            )
            ident = uid("context")
            self.store.db.execute(
                "INSERT INTO contexts VALUES(?,?,?,?,?,?,?)",
                (
                    ident,
                    agent,
                    prior["id"] if prior else None,
                    summary_id,
                    max(r["id"] for r in rows),
                    self.count(summary) + tokens,
                    now(),
                ),
            )
            for position, item in enumerate([summary_id] + [r["id"] for r in recent]):
                self.store.db.execute(
                    "INSERT INTO context_members VALUES(?,?,?)", (ident, item, position)
                )
        self.store.event(
            "context.compacted", {"context": ident, "original_item_count": len(old)}, task, agent
        )
        return ident

    def memory_get(self, topic):
        if (
            not isinstance(topic, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_/-]{0,100}", topic)
            or ".." in topic
        ):
            raise ValueError("Invalid topic path")
        row = self.store.one("SELECT * FROM memories WHERE topic=?", (topic,))
        if not row:
            return {
                "topic": topic,
                "exists": False,
                "revision": 0,
                "summary": "",
                "detail": "",
                "sources": [],
            }
        revision = self.store.one(
            "SELECT * FROM memory_revisions WHERE topic=? AND revision=?", (topic, row["revision"])
        )
        return {
            "topic": topic,
            "exists": True,
            "revision": row["revision"],
            "summary": row["summary"],
            "detail": self.store.get(row["detail_hash"]) if row["detail_hash"] else "",
            "rationale": revision["rationale"],
            "sources": self.store.get(revision["source_hash"]) if revision["source_hash"] else [],
            "updated_at": row["updated_at"],
            "detail_hash": row["detail_hash"],
        }

    def memory_sources(self, task, operation, sources):
        if (
            not isinstance(sources, list)
            or not sources
            or any(not isinstance(s, str) for s in sources)
        ):
            raise ValueError(
                "Memory update requires observed item/artifact sources, or owner for the current owner request"
            )
        owner = self.store.one(
            "SELECT t.source_item FROM operation_turns o JOIN owner_turns t ON t.id=o.turn_id WHERE o.operation_id=?",
            (operation,),
        )
        if not owner:
            owner = self.store.one(
                "SELECT o.source_item FROM task_owner_turns t JOIN owner_turns o ON o.id=t.turn_id WHERE t.task_id=?",
                (task,),
            )
        visible = self.history.visible("astra", task)
        result = []
        for source in sources:
            if source == "owner":
                if not owner:
                    raise ValueError("No owner message is attributed to this memory update")
                source = "item:" + str(owner["source_item"])
            kind, _, value = source.partition(":")
            if kind == "item" and value.isdigit():
                item = self.store.one(
                    "SELECT session_id,task_id,kind FROM items WHERE id=?", (int(value),)
                )
                allowed = (
                    item
                    and item["session_id"] == "astra"
                    and item["task_id"] in visible
                    and item["kind"] != "reasoning"
                )
            elif kind == "artifact" and re.fullmatch(r"[0-9a-f]{64}", value):
                allowed = any(
                    r["task_id"] in visible
                    for r in self.store.rows(
                        "SELECT task_id FROM operations WHERE result_hash=?", (value,)
                    )
                )
                allowed = allowed or any(
                    r["task_id"] in visible
                    for r in self.store.rows(
                        "SELECT i.task_id FROM worker_launches w JOIN items i ON i.id=w.report_item WHERE w.report_hash=?",
                        (value,),
                    )
                )
            else:
                allowed = False
            if not allowed:
                raise ValueError(
                    "Memory source is unavailable or outside visible evidence: " + source
                )
            if source not in result:
                result.append(source)
        return result

    def memory(self, topic, expected_revision, summary, detail, rationale, sources, *, export=True):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_/-]{0,100}", topic) or ".." in topic:
            raise ValueError("Invalid topic path")
        limit = self.store.config.data["memory"][
            "global_tokens" if topic == "global" else "topic_tokens"
        ]
        if self.count(summary) > limit:
            raise ValueError("Memory summary exceeds its configured token budget")
        with self.store.transaction():
            previous = self.store.one("SELECT revision FROM memories WHERE topic=?", (topic,))
            revision = previous["revision"] if previous else 0
            if revision != expected_revision:
                raise ValueError(
                    f"Stale memory revision: expected {expected_revision}, current {revision}; candidate not applied"
                )
            revision += 1
            digest = self.store.put(clean(detail))
            self.store.db.execute(
                "INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?)",
                (
                    topic,
                    revision,
                    clean(summary),
                    digest,
                    clean(rationale),
                    self.store.put(sources),
                    now(),
                ),
            )
            self.store.db.execute(
                "INSERT INTO memories VALUES(?,?,?,?,?) ON CONFLICT(topic) DO UPDATE SET revision=excluded.revision,summary=excluded.summary,detail_hash=excluded.detail_hash,updated_at=excluded.updated_at",
                (topic, revision, clean(summary), digest, now()),
            )
        path = self.store.config.root / "memories" / (topic + ".md")
        if export:
            self.export_memory(topic)
        return {"topic": topic, "revision": revision, "path": str(path)}

    def export_memory(self, topic):
        """Rebuild the readable mirror from the committed SQLite revision."""
        value = self.memory_get(topic)
        if value["exists"]:
            path = self.store.config.root / "memories" / (topic + ".md")
            atomic(
                path,
                f"<!-- Accepted revision {value['revision']}; submit edits with expected_revision. -->\n\n{value['summary']}\n\n{value['detail']}\n".encode(),
            )
