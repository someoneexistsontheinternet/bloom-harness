"""Complete addressed text packages, published once and consumed independently."""

import json
import mimetypes
import os
from pathlib import Path
import shutil
import uuid
import yaml

from .agents import Actor, Agents
from .store import atomic, digest, encoded, identifier, sync_directory


def addressed(text):
    """Read optional YAML front matter; preserve the entire original body verbatim."""
    metadata = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is None:
            raise ValueError("Unclosed message addressing header")
        metadata = yaml.safe_load("\n".join(lines[1:end])) or {}
        if not isinstance(metadata, dict):
            raise ValueError("Message addressing must be a mapping")
    target = metadata.get("target")
    if target is not None and not isinstance(target, str):
        raise ValueError("Message target must be text")
    return target, metadata


class Messages:
    def __init__(self, store):
        self.store = store
        self.agents = Agents(store)

    def inbox(self, recipient):
        if recipient == "user":
            return self.store.root / "workspace/user/inbox"
        actor = self.resolve(recipient)
        return self.agents.path(actor) / "inbox"

    def resolve(self, recipient):
        if isinstance(recipient, Actor):
            self.agents.get(recipient)
            return recipient
        identifier(recipient)
        branch = self.store.latest("branch.created", recipient)
        actor = Actor(branch["data"]["owner"], recipient) if branch else Actor(recipient)
        self.agents.get(actor)
        return actor

    def deliver(
        self,
        sender,
        recipient,
        text,
        *,
        message_id,
        attachments=(),
        kind="message",
        reply_to=None,
        metadata=None,
    ):
        identifier(message_id)
        target = self.inbox(recipient)
        target.mkdir(parents=True, exist_ok=True)
        body = self.store.put(text, "text/plain")
        items = []
        for n, item in enumerate(attachments):
            ref = item["source"]
            self.store.read(ref)
            filename = Path(item.get("name", "attachment-" + str(n))).name
            if filename in ("", ".", ".."):
                raise ValueError("Invalid attachment name")
            name = f"{n:03d}-" + filename
            items.append(
                {
                    **item,
                    "name": name,
                    "media": item.get("media") or mimetypes.guess_type(filename)[0] or ref["media"],
                }
            )
        package = {
            "id": message_id,
            "sender": sender,
            "recipient": recipient if isinstance(recipient, str) else recipient.scope,
            "kind": kind,
            "reply_to": reply_to,
            "body": body,
            "attachments": items,
            "metadata": metadata or {},
        }
        intent = self.store.event(
            "message.prepared", message_id, package, key="message-prepare:" + message_id
        )
        destination = target / message_id
        if destination.exists():
            if json.loads((destination / "provenance.json").read_bytes()) != package:
                raise ValueError("Existing message package differs from delivery intent")
            if digest((destination / "message.txt").read_bytes()) != body["sha256"]:
                raise ValueError("Delivered message was modified")
            for item in items:
                if (
                    digest((destination / "attachments" / item["name"]).read_bytes())
                    != item["source"]["sha256"]
                ):
                    raise ValueError("Delivered attachment was modified")
        else:
            staged = target / (".pending-" + uuid.uuid4().hex)
            try:
                staged.mkdir()
                atomic(staged / "message.txt", self.store.read(body))
                for item in items:
                    atomic(staged / "attachments" / item["name"], self.store.read(item["source"]))
                atomic(staged / "provenance.json", encoded(package))
                os.rename(staged, destination)
                sync_directory(target)
            finally:
                if staged.exists():
                    shutil.rmtree(staged)
        for item in items:
            self.store.expose(item["source"], target.parent, links=item.get("links", []))
        receipt = self.store.event(
            "message.delivered",
            message_id,
            {"intent": intent["key"], "recipient": package["recipient"]},
            key="message-delivered:" + message_id,
        )
        return {
            "id": message_id,
            "recipient": package["recipient"],
            "status": "delivered",
            "receipt": receipt["key"],
        }

    def pending(self, recipient):
        target = self.inbox(recipient)
        result = []
        for path in sorted(target.glob("*/provenance.json")):
            if path.parent.name.startswith("."):
                continue
            package = json.loads(path.read_bytes())
            if self.store.by_key("message-consumed:" + package["id"]):
                continue
            if self.store.by_key("message-filed:" + package["id"]):
                continue
            if digest((path.parent / "message.txt").read_bytes()) != package["body"]["sha256"]:
                raise ValueError("Inbox message differs from its original")
            result.append(package)
        return sorted(result, key=lambda p: self.store.by_key("message-prepare:" + p["id"])["seq"])

    def consume(self, packages, turn):
        for package in packages:
            self.store.event(
                "message.consumed",
                package["id"],
                {"turn": turn},
                key="message-consumed:" + package["id"],
            )

    def file(self, packages, reason):
        for package in packages:
            self.store.event(
                "message.filed",
                package["id"],
                {"reason": reason},
                key="message-filed:" + package["id"],
            )

    def recover(self):
        for row in self.store.events("message.prepared"):
            if self.store.by_key("message-delivered:" + row["subject"]):
                for item in row["data"]["attachments"]:
                    self.store.expose(
                        item["source"],
                        self.inbox(row["data"]["recipient"]).parent,
                        links=item.get("links", []),
                    )
                continue
            p = row["data"]
            # Stored names already contain a package prefix; restore their original part.
            attachments = [{**a, "name": a["name"].split("-", 1)[1]} for a in p["attachments"]]
            self.deliver(
                p["sender"],
                p["recipient"],
                self.store.read(p["body"]).decode(),
                message_id=p["id"],
                attachments=attachments,
                kind=p["kind"],
                reply_to=p["reply_to"],
                metadata=p["metadata"],
            )
