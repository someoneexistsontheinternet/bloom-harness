"""Identified filesystem inputs with separate received and consumed receipts."""

import json
from pathlib import Path
import tomllib

from .store import Store, atomic, encoded, identifier, uid


class Mailbox:
    """Accept operator-authored inputs; model tools never impersonate the operator."""

    def __init__(self, store: Store):
        self.store = store

    def post(self, text, target="astra", *, urgent=False, request_id=None, task=None):
        identifier(target)
        request_id = identifier(request_id or uid("input"))
        envelope = dict(
            id=request_id, target=target, text=text, urgent=urgent, task=task or request_id
        )
        path = self.store.root / "inbox" / target / (request_id + ".json")
        if path.exists() and path.read_bytes() != encoded(envelope):
            raise ValueError("Inbox ID conflict")
        atomic(path, encoded(envelope))
        return envelope

    def ingest(self):
        """Receipt valid envelopes before moving them; replay after a crash is idempotent."""
        accepted = []
        for path in sorted((self.store.root / "inbox").glob("*/*")):
            if path.suffix not in (".json", ".toml") or path.name.startswith("."):
                continue
            with self.store.locked():
                if not path.exists():
                    continue
                try:
                    if path.is_symlink():
                        raise ValueError("Inbox entries must be regular files")
                    raw = path.read_bytes()
                    data = (
                        tomllib.loads(raw.decode()) if path.suffix == ".toml" else json.loads(raw)
                    )
                    identifier(data["id"])
                    identifier(data["target"])
                    if data["target"] != path.parent.name or not isinstance(data["text"], str):
                        raise ValueError("Input target/text mismatch")
                    if type(data.get("urgent", False)) is not bool:
                        raise ValueError("urgent must be a Boolean")
                    task = identifier(data.get("task", data["id"]))
                    envelope = dict(
                        id=data["id"],
                        target=data["target"],
                        text=data["text"],
                        urgent=bool(data.get("urgent", False)),
                        task=task,
                    )
                    ev = self.store.append(
                        "input.received",
                        envelope,
                        actor="owner",
                        target=envelope["target"],
                        task=task,
                        dedup="input:" + data["id"],
                    )
                    dest = self.store.root / "received" / envelope["target"] / path.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    path.replace(dest)
                    accepted.append(ev)
                except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                    ref = self.store.put(path.read_bytes()) if not path.is_symlink() else None
                    self.store.append(
                        "input.rejected", {"path": path.name, "reason": str(exc), "source": ref}
                    )
                    path.replace(self.store.root / "quarantine" / (uid("bad") + path.suffix))
        return accepted

    def pending(self, target):
        consumed = {
            x
            for e in self.store.events("input.consumed", target=target)
            for x in e["data"]["events"]
        }
        return [
            e for e in self.store.events("input.received", target=target) if e["id"] not in consumed
        ]

    def consume(self, target, event_ids, task=""):
        for ident in event_ids:
            event = self.store.get(ident)
            if event["target"] != target or event["kind"] != "input.received":
                raise ValueError("Input belongs to another recipient")
        event = self.store.append(
            "input.consumed", {"events": event_ids}, actor=target, target=target, task=task
        )
        for ident in event_ids:
            view = self.store.root / "seen" / target / (ident + ".json")
            atomic(view, encoded({"event": ident, "consumption": event["id"]}))
        return event
