"""Public-text-only streaming. A draft never becomes an accepted final message."""

from __future__ import annotations

import time

from .privacy import clean
from .store import now


class PublicStream:
    def __init__(self, store, agent, task, call, purpose):
        self.store = store
        self.task = task
        self.call = call
        self.enabled = bool(task and agent == "astra" and purpose == "turn")
        self.parts = {}
        self.last_write = 0
        self.changed = False

    def event(self, event):
        if not self.enabled:
            return
        kind = event.get("type")
        key = (event.get("output_index", 0), event.get("content_index", 0))
        if kind == "response.output_text.delta":
            self.parts[key] = self.parts.get(key, "") + event.get("delta", "")
            self.changed = True
        elif kind == "response.output_text.done":
            self.parts[key] = event.get("text", "")
            self.changed = True
        if self.changed and time.monotonic() - self.last_write >= 0.5:
            self.write("streaming")

    def write(self, status):
        if not self.enabled or not self.parts:
            return
        text = clean("\n\n".join(self.parts[key] for key in sorted(self.parts)))
        self.store.db.execute(
            "INSERT INTO model_text_streams VALUES(?,?,?,?,1,?) ON CONFLICT(call_id) DO UPDATE SET text=excluded.text,status=excluded.status,revision=revision+1,updated_at=excluded.updated_at",
            (self.call, self.task, text, status, now()),
        )
        self.last_write = time.monotonic()
        self.changed = False


class LivePreviews:
    def __init__(self, delivery):
        self.delivery = delivery
        self.store = delivery.store

    def latest(self):
        return self.store.rows(
            "SELECT s.*,c.status call_status FROM model_text_streams s JOIN model_calls c ON c.id=s.call_id JOIN tasks t ON t.id=s.task_id WHERE t.channel='discord' AND t.agent_id='astra' AND s.rowid=(SELECT max(s2.rowid) FROM model_text_streams s2 WHERE s2.task_id=s.task_id)"
        )

    def prepare(self):
        if (
            not self.delivery.discord.settings.get("stream_previews", True)
            or not self.delivery.discord.settings["enabled"]
        ):
            return
        for stream in self.latest():
            prior = self.store.one(
                "SELECT * FROM live_previews WHERE task_id=?", (stream["task_id"],)
            )
            if prior or stream["call_status"] != "running":
                continue
            with self.store.transaction():
                ident = self.delivery.queue_payload(
                    stream["task_id"],
                    "public-draft",
                    {"content": "Generating response · provisional draft"},
                    kind="stream",
                )
                self.store.db.execute(
                    "INSERT OR IGNORE INTO live_previews VALUES(?,?,?,?,0)",
                    (stream["task_id"], ident, stream["call_id"], 0),
                )

    async def flush(self):
        if (
            not self.delivery.discord.settings.get("stream_previews", True)
            or not self.delivery.discord.settings["enabled"]
        ):
            return
        for stream in self.latest():
            state = self.store.one(
                "SELECT * FROM live_previews WHERE task_id=?", (stream["task_id"],)
            )
            if not state or state["retry_at"] > time.time():
                continue
            terminal = stream["call_status"] not in ("prepared", "running")
            # A restart can reconcile the model row without a final stream write.
            revision = -stream["revision"] if terminal else stream["revision"]
            if state["call_id"] == stream["call_id"] and state["revision"] == revision:
                continue
            sent = self.store.one(
                "SELECT * FROM deliveries WHERE id=? AND status='sent' AND external_id IS NOT NULL",
                (state["delivery_id"],),
            )
            if not sent:
                continue  # Unknown initial POST is never blindly resent.
            if terminal:
                text = (
                    "Model response received. Tool progress or the final reply follows."
                    if stream["call_status"] == "completed"
                    else "Generation interrupted. The provisional draft was not a completed reply; retained evidence is available for recovery."
                )
            else:
                public = stream["text"]
                # This is a cosmetic moving preview, not history truncation.
                if len(public) > 1750:
                    public = "…" + public[-1749:]
                text = "**Generating · provisional draft**\n" + public
            try:
                await self.delivery.discord.request(
                    "PATCH",
                    f"/channels/{sent['address']}/messages/{sent['external_id']}",
                    {"content": text, "allowed_mentions": {"parse": []}},
                )
                self.store.db.execute(
                    "UPDATE live_previews SET call_id=?,revision=?,retry_at=? WHERE task_id=?",
                    (stream["call_id"], revision, time.time() + 1.5, stream["task_id"]),
                )
            except Exception as exc:
                # PATCH is idempotent. Cosmetic failures cannot hold the final
                # outbox or produce another owner attention notification.
                self.store.db.execute(
                    "UPDATE live_previews SET retry_at=? WHERE task_id=?",
                    (time.time() + getattr(exc, "seconds", 5), stream["task_id"]),
                )
                self.store.event(
                    "delivery.preview_error",
                    {"call": stream["call_id"], "error_type": type(exc).__name__},
                    stream["task_id"],
                )
