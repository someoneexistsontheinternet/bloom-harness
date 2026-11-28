"""Authenticated message primitives. Preparation strategy lives in send_message.py."""

import mimetypes
import json
from pathlib import Path
import re

from .agents import Agents
from .messages import Messages
from .store import atomic, digest, encoded
from .tool import resolve_path


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
        return resolve_path(
            self.agents.path(actor),
            value,
            read_roots=self.agents.read_roots(actor),
            shared=self.store.root / "workspace/shared",
            memory=self.agents.path(actor.agent) / "memory",
        )

    def attachment(
        self, actor, job, name, source, *, expose="inline", provenance=None, links=(), preview=None
    ):
        ident = "attachment_" + digest(encoded([job["id"], name, source, expose]))[:24]
        data = {
            "id": ident,
            "job": job["id"],
            "name": Path(name).name,
            "source": source,
            "media": source["media"],
            "expose": expose,
            "provenance": provenance or {"job": job["id"]},
            "source_id": "source:" + source["sha256"],
            "links": list(links),
            **({"preview": preview} if preview else {}),
        }
        self.store.event("attachment.prepared", actor.scope, data, key=ident)
        self.store.expose(source, self.agents.path(actor), links=links)
        return data

    async def attach_file(self, actor, job, args, _trail=None):
        path = self.path(actor, args["path"])
        reference = args.get("reference", False)
        key = "file-attachment:" + digest(encoded([job["id"], str(path), reference]))
        old = self.store.by_key(key)
        if old:
            return old["data"]
        raw = path.read_bytes()
        metadata = path.with_name(path.name + ".json")
        published = (
            json.loads(metadata.read_bytes())
            if path.parent.name == "sources"
            and re.fullmatch(r"[a-f0-9]{64}", path.name)
            and metadata.is_file()
            else {}
        )
        media = (
            published.get("media")
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )
        source = self.store.put(raw, media)
        try:
            large = not media.startswith("image/") and len(raw.decode()) > 50_000
        except UnicodeError:
            large = False
        record = self.attachment(
            actor,
            job,
            path.name,
            source,
            expose="reference" if large or reference else "inline",
            provenance={"path": str(path), "sha256": source["sha256"]},
            links=published.get("links", []),
        )
        snapshot = self.store.expose(
            source, self.agents.path(actor), links=published.get("links", [])
        )
        trail = _trail if _trail is not None else set()
        trail.add(record["source_id"])
        related = []
        for link in published.get("links", []):
            if link in trail:
                continue
            child = await self.attach_file(actor, job, {"path": link, "reference": True}, trail)
            related.extend([child["id"], *child.get("related", [])])
        result = {
            "id": record["id"],
            "source_id": record["source_id"],
            "related": list(dict.fromkeys(related)),
            "snapshot_path": str(snapshot),
        }
        self.store.event("attachment.file", actor.scope, result, key=key)
        return result

    async def attach_value(self, actor, job, args):
        key = "value-attachment:" + digest(
            encoded([actor.scope, job["id"], args["name"], args["value"]])
        )
        old = self.store.by_key(key)
        if old:
            result = old["data"]
            # Extraction may still be queued. Reuse the frozen preparation, and
            # restore its derived local view if an owner edited or removed it.
            snapshot = Path(result["snapshot_path"])
            raw = self.store.read(result["source"])
            if not snapshot.exists() or snapshot.read_bytes() != raw:
                atomic(snapshot, raw)
            return result
        value = self.jobs.clean(args["value"])
        output = value.get("output", value) if isinstance(value, dict) else {}
        related = []
        if isinstance(output, dict):
            published = {}
            paths = list(output.get("files", [])) if isinstance(output.get("files"), list) else []
            if output.get("image_path"):
                paths.append(output["image_path"])
            for path in dict.fromkeys(paths):
                if isinstance(path, str):
                    attached = await self.attach_file(actor, job, {"path": path})
                    related.extend([attached["id"], *attached.get("related", [])])
                    published[path] = attached["source_id"]
            for path in dict.fromkeys(output.get("reference_files", [])):
                if isinstance(path, str):
                    attached = await self.attach_file(actor, job, {"path": path, "reference": True})
                    related.extend([attached["id"], *attached.get("related", [])])
                    published[path] = attached["source_id"]
            # The derived message view uses immutable references. The native
            # tool result remains unchanged in its job receipt. Include those
            # references in this view's hash so revised files get a new identity.
            for field in ("files", "reference_files"):
                if isinstance(output.get(field), list):
                    output[field] = [published.get(p, p) for p in output[field]]
            if output.get("image_path"):
                output["image_path"] = published.get(output["image_path"], output["image_path"])
        ref = self.store.put(value)
        large = len(encoded(value).decode()) > 50_000
        related = list(dict.fromkeys(related))
        links = ["source:" + self.store.by_key(i)["data"]["source"]["sha256"] for i in related]
        summary = output.get("summary") if isinstance(output, dict) else None
        record = self.attachment(
            actor,
            job,
            args["name"],
            ref,
            expose="reference" if large else "inline",
            links=links,
            preview=summary
            if large and isinstance(summary, str) and len(summary) <= 50_000
            else None,
        )
        snapshot = self.store.expose(ref, self.agents.path(actor), links=links)
        result = {
            **record,
            "related": related,
            "snapshot_path": str(snapshot),
        }
        self.store.event("attachment.value", actor.scope, result, key=key)
        return result

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
        # A typed published-source citation is a request to share that source,
        # not a private filesystem path. Missing references never block prose.
        missing = []
        for source_id in dict.fromkeys(re.findall(r"\bsource:[a-f0-9]{64}\b", text)):
            try:
                selected = await self.attach_file(
                    actor, job, {"path": source_id, "reference": True}
                )
                for ident in [selected["id"], *selected.get("related", [])]:
                    if ident not in attachments:
                        attachments.append(ident)
                        items.append(self.store.by_key(ident)["data"])
            except (FileNotFoundError, PermissionError) as exc:
                missing.append({"source_id": source_id, "error": str(exc)})
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
                **({"unavailable_sources": missing} if missing else {}),
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
