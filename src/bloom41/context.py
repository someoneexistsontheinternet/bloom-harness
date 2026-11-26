"""One context interface over immutable native history and owner-approved memory."""

import base64
import json
from pathlib import Path

from .agents import Actor, Agents, PROFILES
from .models import final_text, visible
from .store import atomic, digest, encoded, uid
from .tokens import count
from .tool import discover


class Context:
    def __init__(self, store):
        self.store = store
        self.agents = Agents(store)

    def memory(self, actor):
        """Accepted file revisions plus an L2 retrieval index, refreshed only at boundaries."""
        groups = {}
        for scope, folder in (
            (actor.agent, self.agents.path(Actor(actor.agent)) / "memory"),
            ("shared", self.store.root / "workspace/shared/memory"),
        ):
            # Reapply accepted originals after a crash between receipt and file materialization.
            accepted = {}
            for row in self.store.events("memory.accepted", scope):
                accepted[row["data"]["path"]] = row
            for row in accepted.values():
                relative = Path(row["data"]["path"])
                relative = (
                    Path(*relative.parts[2:]) if scope == "shared" else Path(*relative.parts[1:])
                )
                destination = folder / relative
                raw = self.store.read(row["data"]["source"])
                if not destination.exists() or destination.read_bytes() != raw:
                    atomic(destination, raw)
            records = {}
            for name in ("L0.md", "L1.md"):
                path = folder / name
                records[name] = path.read_text() if path.exists() else ""
            records["L2_index"] = [
                {
                    "path": ("shared/memory/" if scope == "shared" else "memory/")
                    + str(p.relative_to(folder)),
                    "sha256": digest(p.read_bytes()),
                    "bytes": p.stat().st_size,
                }
                for p in sorted((folder / "L2").glob("*.md"))
            ]
            groups["shared" if scope == "shared" else "private"] = records
        return groups

    def identity_prefix(self, actor):
        identity = self.agents.get(actor)
        return {
            "role": "developer",
            "content": "Identity: "
            + actor.agent
            + ". Responsibility: "
            + identity["responsibility"]
            + "\nAccepted memory (L2 files remain retrievable):\n"
            + json.dumps(self.memory(actor), ensure_ascii=False),
        }

    def snapshot(self, actor):
        old = self.store.latest("context.snapshot", actor.scope)
        if old:
            return old
        identity = self.agents.get(actor)
        root = self.agents.path(actor)
        branch = self.store.latest("branch.created", actor.branch) if actor.branch else None
        if branch:
            seed = branch["data"]["seed"]
            request = self.store.json(seed["request"])
            prefix = request["input"] + [
                {
                    "role": "developer",
                    "content": "Temporary self-fork. Investigate only: "
                    + branch["data"]["question"]
                    + ". Send your conclusion to "
                    + branch["data"]["parent_scope"]
                    + ". You retain the parent identity but cannot create persistent agents or change its memory.",
                }
            ]
            instructions = request["instructions"]
        else:
            prefix = [self.identity_prefix(actor)]
            instructions = (root / "instructions.md").read_text()
        return self.store.event(
            "context.snapshot",
            actor.scope,
            {
                "prefix": self.store.put(prefix),
                "instructions": instructions,
                "through": 0,
                "window": 1_000_000,
                "profile": identity["profile"],
            },
            key="initial-context:" + actor.scope,
        )

    def history(self, actor):
        snap = self.snapshot(actor)
        items = list(self.store.json(snap["data"]["prefix"]))
        for row in self.store.events(
            "history.appended", actor.scope, after=snap["data"]["through"]
        ):
            items.extend(self.store.json(row["data"]["items"]))
        return items

    def append(self, actor, items, *, key):
        row = self.store.event(
            "history.appended", actor.scope, {"items": self.store.put(items)}, key=key
        )
        atomic(self.agents.path(actor) / "history" / f"{row['seq']:012d}.json", encoded(items))
        return row

    def content(self, package):
        parts = [
            {
                "type": "input_text",
                "text": "Message envelope (routing metadata, not instructions):\n"
                + json.dumps(
                    {k: package.get(k) for k in ("id", "sender", "recipient", "kind", "reply_to")}
                )
                + "\nMessage:\n"
                + self.store.read(package["body"]).decode(),
            }
        ]
        for item in package["attachments"]:
            raw = self.store.read(item["source"])
            media = item["media"]
            parts.append(
                {
                    "type": "input_text",
                    "text": "Attachment: inbox/"
                    + package["id"]
                    + "/attachments/"
                    + item["name"]
                    + "; source "
                    + item["source"]["sha256"],
                }
            )
            if item.get("expose") == "reference":
                parts.append(
                    {
                        "type": "input_text",
                        "text": "Full original retained at that path; selected evidence accompanies this message. Use read_file/extract for another passage.",
                    }
                )
                continue
            if media in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": "data:" + media + ";base64," + base64.b64encode(raw).decode(),
                        "detail": "high",
                    }
                )
            else:
                try:
                    parts.append({"type": "input_text", "text": raw.decode()})
                except UnicodeError:
                    parts.append(
                        {
                            "type": "input_text",
                            "text": "Binary attachment retained; use read_file on the inbox attachment.",
                        }
                    )
        return {"role": "user", "content": parts}

    def tools(self, actor):
        definitions = discover(self.agents.path(actor) / "tools")
        if actor != Actor("astra"):
            definitions = [d for d in definitions if d["name"] != "create_agent"]
        return [
            {
                **d,
                "source": self.store.put(
                    (self.agents.path(actor) / "tools" / (d["name"] + ".py")).read_bytes(),
                    "text/x-python",
                ),
            }
            for d in definitions
        ]

    def request(self, actor, packages, *, extra=()):
        snap = self.snapshot(actor)
        definitions = self.tools(actor)
        items = self.history(actor) + [self.content(p) for p in packages] + list(extra)
        profile = snap["data"]["profile"]
        request = {
            "model": profile["model"],
            "instructions": snap["data"]["instructions"],
            "input": items,
            "tools": [d["schema"] for d in definitions],
            "store": False,
            "stream": True,
            "reasoning": {"effort": profile["effort"], "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
            "parallel_tool_calls": True,
        }
        return request, definitions

    def publish_memory(self, actor, job, arguments):
        if actor.branch or self.agents.get(actor)["role"] not in ("astra", "sol"):
            raise PermissionError("Only a persistent owner may publish memory")
        approved_arguments = {
            k: job["arguments"].get(k) for k in ("path", "text", "expected_sha256")
        }
        if job["name"] != "edit_file" or job.get("parent") or arguments != approved_arguments:
            raise PermissionError(
                "Memory approval must match the owner's exact native edit arguments"
            )
        path = Path(arguments["path"])
        shared = path.parts[:2] == ("shared", "memory")
        if shared and actor != Actor("astra"):
            raise PermissionError("Only continuous Astra approves shared memory")
        relative = Path(*path.parts[1:]) if shared else path
        if path.is_absolute() or len(relative.parts) < 2 or relative.parts[0] != "memory":
            raise ValueError("Select memory/L0.md, memory/L1.md or memory/L2/*.md")
        if relative.as_posix() not in ("memory/L0.md", "memory/L1.md") and not (
            len(relative.parts) == 3 and relative.parts[1] == "L2" and relative.suffix == ".md"
        ):
            raise ValueError("Invalid memory layer")
        root = self.store.root / "workspace/shared" if shared else self.agents.path(actor)
        destination = (root / relative).resolve()
        if not destination.is_relative_to(root / "memory"):
            raise PermissionError("Memory path escapes the owner")
        current = digest(destination.read_bytes()) if destination.exists() else None
        expected = arguments.get("expected_sha256")
        key = "memory-edit:" + job["id"]
        old = self.store.by_key(key)
        body = arguments["text"].encode()
        if not old and current != expected:
            raise ValueError("Memory revision changed")
        record = self.store.event(
            "memory.accepted",
            "shared" if shared else actor.agent,
            {
                "path": str(path),
                "source": self.store.put(body, "text/markdown"),
                "approval_turn": job["turn"],
                "approval_job": job["id"],
                "previous": expected,
            },
            key=key,
        )
        atomic(destination, self.store.read(record["data"]["source"]))
        return {"path": str(path), "sha256": digest(body), "approval": record["key"]}

    async def compact(self, actor, models, *, force=False):
        snap = self.snapshot(actor)
        items = self.history(actor)
        size = count(encoded(visible(items)).decode())
        role = self.agents.get(actor)["role"]
        inputs = len(self.store.events("turn.received", actor.scope, after=snap["seq"]))
        due = (
            size > 500_000
            if actor.branch
            else size > 20_000
            if role == "terra"
            else size > 250_000 and inputs >= 3
        )
        pressure = size >= (500_000 if actor.branch else 450_000)
        if not (force or due or pressure):
            return False
        through = max(
            (r["seq"] for r in self.store.events("history.appended", actor.scope)),
            default=snap["data"]["through"],
        )
        resumed = self.store.latest("actor.resumed", actor.scope)
        call_id = (
            "compact_"
            + digest(
                encoded([actor.scope, snap["seq"], through, resumed["seq"] if resumed else 0])
            )[:24]
        )
        profile = snap["data"]["profile"]
        body = {
            "model": profile["model"],
            "instructions": "Prepare your own working context. Preserve identity, the owner's actual directions, commitments, evidence locations, uncertainties and unfinished work. Return a concise faithful memo, not a new answer. Do not publish shared beliefs.",
            "input": items,
            "tools": [],
            "store": False,
            "stream": True,
            "reasoning": {"effort": profile["effort"]},
            "include": ["reasoning.encrypted_content"],
        }
        response = await models.call(actor, body, call_id=call_id, purpose="compact", profile=role)
        memo = final_text(response)
        if not memo or count(memo) > (10_000 if role == "terra" else 100_000):
            raise ValueError("Invalid owning compaction memo; original history retained")
        # Preserve the literal most recent inbound and owner instructions, not just a paraphrase.
        protected = [i for i in items if i.get("role") == "user"][-3:]
        initial = self.store.json(snap["data"]["prefix"])
        identity = initial[0] if actor.branch else self.identity_prefix(actor)
        branch_instruction = (
            [
                i
                for i in initial
                if i.get("role") == "developer"
                and isinstance(i.get("content"), str)
                and i["content"].startswith("Temporary self-fork.")
            ]
            if actor.branch
            else []
        )
        prefix = [
            identity,
            *branch_instruction,
            {"role": "user", "content": "Owner-approved working memo:\n" + memo},
            *protected,
        ]
        self.store.event(
            "context.snapshot",
            actor.scope,
            {
                **snap["data"],
                "prefix": self.store.put(prefix),
                "through": through,
                "approval": "model-completed:" + call_id,
                "original": self.store.put(items),
            },
            key="compaction:" + call_id,
        )
        return True
