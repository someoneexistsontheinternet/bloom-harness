"""Versioned Markdown memory with exact-content approval and source references."""

import json

from .agents import Agents
from .store import Store, atomic, encoded, identifier


class Memory:
    """Accepted revisions are files plus immutable approval events, never mutable SQL facts."""

    def __init__(self, store: Store):
        self.store = store
        self.agents = Agents(store)

    def entries(self, owner):
        entries = {}
        if owner != "shared":
            agent = self.agents.get(owner)
            if agent.seed:
                for item in agent.seed.get("memories", []):
                    entries[(item["layer"], item["key"])] = item
        for event in self.store.events("memory.accepted", target=owner):
            item = {**event["data"], "event": event["id"]}
            entries[(item["layer"], item["key"])] = item
        return list(entries.values())

    def get(self, owner, layer, key):
        return next((e for e in self.entries(owner) if (e["layer"], e["key"]) == (layer, key)), None)

    def publish(self, actor, owner, layer, key, text, *, expected=0, sources=(), approval=None):
        """Approve these exact bytes; stale revisions and helper-authored memories fail."""
        identifier(owner); identifier(key)
        if layer not in ("L0", "L1", "L2") or not isinstance(text, str):
            raise ValueError("Publish Markdown to L0, L1 or L2; L3 is original events")
        if actor != "owner":
            agent = self.agents.get(actor)
            if not agent.writer or (owner == "shared" and actor != "astra") or (owner != "shared" and actor != owner):
                raise PermissionError("Only the persistent owner can approve private memory; Astra approves shared")
            if not approval:
                raise ValueError("An exact approving model call is required")
            approved = self.store.get(approval)
            if approved["kind"] != "model.completed" or approved["target"] != actor:
                raise ValueError("Approval must belong to this owner's completed call")
            response = json.loads(self.store.read(approved["data"]["response"]))
            candidates = []
            for call in response.get("output", []):
                if call.get("type") == "function_call" and call.get("name") == "memory_publish":
                    candidates.append(json.loads(call["arguments"]))
            if not any(a.get("owner", actor) == owner and a.get("layer") == layer and
                       a.get("key") == key and a.get("text") == text and
                       a.get("expected", 0) == expected and a.get("sources", []) == list(sources)
                       for a in candidates):
                raise ValueError("Approval does not contain this exact memory proposal")
        for source in sources:
            self.store.get(source)
        with self.store.locked():
            self.store.sync()
            if approval:
                for event in self.store.events("memory.accepted", target=owner):
                    item = event["data"]
                    if (item.get("approval"), item["layer"], item["key"], item["text"], item["revision"]) == (approval, layer, key, text, expected + 1):
                        return {**item, "event": event["id"]}
            old = self.get(owner, layer, key)
            revision = old["revision"] if old else 0
            if expected != revision:
                raise ValueError(f"Memory revision conflict: expected {expected}, actual {revision}")
            ref = self.store.put(text, "text/markdown")
            # The named immutable Markdown is independently readable. Events retain
            # the exact hash, source IDs, author and approval required for reconstruction.
            path = self.store.root / "agents" / owner / "memory" / layer / key / f"{revision + 1:06d}.md"
            if path.exists() and path.read_bytes() != text.encode():
                raise ValueError("Uncommitted memory revision collision; inspect before replacing")
            atomic(path, text.encode())
            data = dict(layer=layer, key=key, revision=revision + 1, source=ref,
                        file=str(path.relative_to(self.store.root)), sources=list(sources),
                        approval=approval, text=text)
            event = self.store.append("memory.accepted", data, actor=actor, target=owner)
            return {**data, "event": event["id"]}

    def packet(self, owner):
        """Return all accepted active layers; never discard an L1 obligation to fit a cap."""
        return [{"owner": scope, **entry} for scope in ("shared", owner) for entry in self.entries(scope)]

    def search(self, owner, query):
        allowed = {owner, "shared"}
        return [e for e in self.store.search(query, limit=100)
                if e["target"] in allowed and e["kind"] in
                ("memory.accepted", "input.received", "answer", "timeline.entry", "source.saved")]
