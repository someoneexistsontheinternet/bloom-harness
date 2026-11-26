"""Authenticated message primitives. Preparation strategy lives in send_message.py."""

import mimetypes
from pathlib import Path

from .agents import Agents
from .messages import Messages
from .store import atomic, digest, encoded


class Delivery:
    def __init__(self, store, jobs):
        self.store = store
        self.jobs = jobs
        self.agents = Agents(store)
        self.messages = Messages(store)

    def handlers(self):
        return {
            name: getattr(self, name)
            for name in ("message_body", "attach_file", "attach_value", "publish_message")
        }

    def turn(self, actor, job):
        row = self.store.latest("turn.state", job.get("turn"))
        if not row or row["data"]["scope"] != actor.scope:
            raise PermissionError("Message content must belong to this sender's recorded response")
        return row["data"]

    async def message_body(self, actor, job, args):
        turn = self.turn(actor, job)
        return {"text": self.store.read(turn["final"]).decode(), "turn": turn["id"]}

    def path(self, actor, value):
        root = self.agents.path(actor)
        path = Path(value)
        if not path.is_absolute():
            path = (
                root / path
                if path.parts
                and path.parts[0] in ("work", "memory", "tools", "inbox", "outbound", "history")
                else root / "work" / path
            )
        path = path.resolve()
        if not any(path.is_relative_to(r) for r in (root, *self.agents.read_roots(actor))):
            raise PermissionError("File is outside this sender's source grants")
        return path

    def attachment(self, actor, job, name, source, *, expose="inline", provenance=None):
        ident = "attachment_" + digest(encoded([job["id"], name, source, expose]))[:24]
        data = {
            "id": ident,
            "job": job["id"],
            "name": Path(name).name,
            "source": source,
            "media": source["media"],
            "expose": expose,
            "provenance": provenance or {"job": job["id"]},
        }
        self.store.event("attachment.prepared", actor.scope, data, key=ident)
        return data

    async def attach_file(self, actor, job, args):
        path = self.path(actor, args["path"])
        reference = args.get("reference", False)
        key = "file-attachment:" + digest(encoded([job["id"], str(path), reference]))
        old = self.store.by_key(key)
        if old:
            return old["data"]
        raw = path.read_bytes()
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        source = self.store.put(raw, media)
        try:
            needs_extraction = not media.startswith("image/") and len(raw.decode()) > 50_000
        except UnicodeError:
            needs_extraction = False
        record = self.attachment(
            actor,
            job,
            path.name,
            source,
            expose="reference" if needs_extraction or reference else "inline",
            provenance={"path": str(path), "sha256": source["sha256"]},
        )
        snapshot = self.agents.path(actor) / "work/.sources" / source["sha256"] / path.name
        atomic(snapshot, raw)
        result = {
            "id": record["id"],
            "needs_extraction": needs_extraction and not reference,
            "snapshot_path": str(snapshot),
        }
        self.store.event("attachment.file", actor.scope, result, key=key)
        return result

    async def attach_value(self, actor, job, args):
        value = self.jobs.clean(args["value"])
        ref = self.store.put(value)
        needs_extraction = len(encoded(value).decode()) > 50_000
        record = self.attachment(
            actor, job, args["name"], ref, expose="reference" if needs_extraction else "inline"
        )
        snapshot = self.agents.path(actor) / "work/.sources" / ref["sha256"] / "result.json"
        atomic(snapshot, self.store.read(ref))
        output = value.get("output", value) if isinstance(value, dict) else {}
        related = []
        if isinstance(output, dict):
            paths = list(output.get("files", [])) if isinstance(output.get("files"), list) else []
            if output.get("image_path"):
                paths.append(output["image_path"])
            for path in dict.fromkeys(paths):
                if isinstance(path, str):
                    attached = await self.attach_file(actor, job, {"path": path})
                    related.append(attached["id"])
            for path in dict.fromkeys(output.get("reference_files", [])):
                if isinstance(path, str):
                    attached = await self.attach_file(actor, job, {"path": path, "reference": True})
                    related.append(attached["id"])
        return {
            **record,
            "related": related,
            "needs_extraction": needs_extraction,
            "snapshot_path": str(snapshot),
        }

    async def publish_message(self, actor, job, args):
        recipient = args["recipient"]
        self.messages.inbox(recipient)
        turn = self.turn(actor, job)
        text = self.store.read(turn["final"]).decode()
        message_id = "message_" + digest(encoded([turn["id"], recipient]))[:24]
        existing = self.store.by_key("message-delivered:" + message_id)
        if existing:
            sent = self.store.by_key("message-prepare:" + message_id)["data"]
            if sent["metadata"].get("sender_job") != job["id"]:
                raise ValueError("This response already sent a message to that recipient")
            return {
                "id": message_id,
                "recipient": recipient,
                "status": "delivered",
                "receipt": existing["key"],
            }
        attachments = list(args.get("attachments", []))
        for ident in attachments:
            row = self.store.by_key(ident)
            if not row or row["kind"] != "attachment.prepared" or row["subject"] != actor.scope:
                raise PermissionError("Attachment is outside the sender's prepared evidence")
        if not text.strip():
            self.store.event(
                "message.draft",
                actor.scope,
                {"job": job["id"], "recipient": recipient, "attachments": attachments},
                key="draft:" + job["id"],
            )
            return {
                "status": "needs_final_message",
                "recipient": recipient,
                "draft": job["id"],
                "instruction": "Context is prepared. Put the addressed message in your next final response; it will be sent without another send call.",
            }
        drafts = [
            r
            for r in self.store.events("message.draft", actor.scope)
            if r["data"]["recipient"] == recipient
            and not self.store.by_key("draft-used:" + r["data"]["job"])
        ]
        attachments = list(
            dict.fromkeys([*attachments, *(i for r in drafts for i in r["data"]["attachments"])])
        )
        items = [self.store.by_key(i)["data"] for i in attachments]
        result = self.messages.deliver(
            actor.scope,
            recipient,
            text,
            message_id=message_id,
            attachments=items,
            kind=turn.get("message_kind", "message"),
            reply_to=turn.get("reply_to"),
            metadata={
                "turn": turn["id"],
                "origin": turn.get("origin"),
                "sender_job": job["id"],
                **turn.get("message_metadata", {}),
            },
        )
        for draft in drafts:
            self.store.event(
                "message.draft_used",
                actor.scope,
                {"message": message_id},
                key="draft-used:" + draft["data"]["job"],
            )
        return result
