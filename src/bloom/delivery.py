from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import time

import httpx

from .privacy import clean, register
from .store import now
from . import turns


class RateLimited(RuntimeError):
    def __init__(self, seconds):
        self.seconds = max(1, min(float(seconds), 3600))
        super().__init__(f"Discord rate limited for {self.seconds} seconds")


def chunks(text, limit=1900):
    # Original prose remains in items; display-only fences make long code readable.
    result, offset, fence, language = [], 0, "", ""
    markers = iter(re.finditer(r"(?m)^ {0,3}(`{3,}|~{3,})([^\n]*)\n?", text))
    marker = next(markers, None)
    while offset < len(text):
        prefix = fence + language + "\n" if fence else ""
        capacity = limit - len(prefix) - max(5, len(fence) + 2)
        if capacity < 1:
            raise ValueError("Code fence exceeds the message display limit")
        end = min(len(text), offset + capacity)
        if end < len(text):
            paragraph = text.rfind("\n\n", offset, end)
            # A blank line inside a code block is not a paragraph boundary.
            local_fence = fence
            safe_paragraphs = []
            for line in re.finditer(r".*(?:\n|$)", text[offset:end]):
                value = line.group()
                mark = re.match(r"^ {0,3}(`{3,}|~{3,})([^\n]*)", value)
                if mark:
                    if not local_fence:
                        local_fence = mark.group(1)
                    elif (
                        mark.group(1)[0] == local_fence[0]
                        and len(mark.group(1)) >= len(local_fence)
                        and not mark.group(2).strip()
                    ):
                        local_fence = ""
                if value == "\n" and not local_fence:
                    safe_paragraphs.append(offset + line.end())
            newline = text.rfind("\n", offset, end)
            if paragraph >= offset and safe_paragraphs:
                end = safe_paragraphs[-1]
            elif newline > offset + capacity // 2:
                end = newline + 1
        while marker and marker.end() <= end:
            mark, info = marker.group(1), marker.group(2)
            if not fence:
                fence, language = mark, info
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not info.strip():
                fence, language = "", ""
            marker = next(markers, None)
        suffix = "\n" + fence if fence else ""
        result.append(prefix + text[offset:end] + suffix)
        offset = end
    return result


class Discord:
    def __init__(self, store, transport=None):
        self.store = store
        self.settings = store.config.data["discord"]
        self.transport = transport
        self.bot_id = self.settings.get("bot_id")
        self._keychain_token = None

    def token(self):
        token = os.environ.get(self.settings["token_env"])
        if not token and self.settings.get("keychain_service"):
            token = self._keychain_token
            if not token:
                result = subprocess.run(
                    [
                        "security",
                        "find-generic-password",
                        "-s",
                        self.settings["keychain_service"],
                        "-a",
                        self.settings["keychain_account"],
                        "-w",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode:
                    raise RuntimeError(
                        "Keychain lookup failed in this process context (status "
                        + str(result.returncode)
                        + "); this does not establish that the credential is missing"
                    )
                token = result.stdout.strip()
                self._keychain_token = token
        if not token:
            raise RuntimeError("Discord credential is unavailable: " + self.settings["token_env"])
        register(token)
        return token

    async def request(self, method, path, body=None, files=None):
        async with httpx.AsyncClient(transport=self.transport, timeout=30) as client:
            payload = (
                {"data": {"payload_json": json.dumps(body, ensure_ascii=False)}, "files": files}
                if files
                else {"json": body}
            )
            response = await client.request(
                method,
                "https://discord.com/api/v10" + path,
                headers={"Authorization": "Bot " + self.token()},
                **payload,
            )
            if response.status_code == 429:
                # Caller keeps the same durable delivery identity; no internal resend loop.
                raise RateLimited(response.json().get("retry_after", 5))
            if response.status_code == 401:
                self._keychain_token = None
            response.raise_for_status()
            return response.json() if response.content else {}

    async def inspect(self):
        me = await self.request("GET", "/users/@me")
        channel = await self.request("GET", "/channels/" + self.settings["channel_id"])
        return {
            "bot_id": me["id"],
            "bot_name": me["username"],
            "channel_id": channel["id"],
            "channel_type": channel["type"],
        }


class Delivery:
    def __init__(self, store, discord=None):
        self.store = store
        self.discord = discord or Discord(store)
        self.lock = asyncio.Lock()
        self.last_typing = {}
        channel = self.discord.settings.get("activity_channel_id")
        if self.discord.settings["enabled"] and channel:
            boundary = store.one("SELECT coalesce(max(rowid),0) AS n FROM model_calls")["n"]
            store.db.execute(
                "INSERT OR IGNORE INTO activity_feed VALUES(1,?,?)", (channel, boundary)
            )
            store.db.execute(
                "INSERT OR IGNORE INTO activity_maintenance_feed VALUES(1,?)", (boundary,)
            )

    def _order(self, delivery, after):
        if after is not None:
            self.store.db.execute(
                "INSERT INTO delivery_ordering VALUES(?,?)", (delivery, json.dumps(list(after)))
            )

    def queue(self, item_id, task, text, kind="message", *, after=None):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        with self.store.transaction():
            for part, body in enumerate(chunks(text)):
                # Discord removes trailing line breaks at message boundaries.
                # Journal the actual display form; the native item stays exact.
                body = body.rstrip("\n")
                key = hashlib.sha256(
                    f"{item_id}:{task}:{kind}:{part}:{row['channel']}:{row['address']}".encode()
                ).hexdigest()
                inserted = self.store.db.execute(
                    "INSERT OR IGNORE INTO deliveries(id,item_id,task_id,channel,address,part,body,kind,status,nonce,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        item_id,
                        task,
                        row["channel"],
                        row["address"],
                        part,
                        body,
                        kind,
                        "prepared",
                        str(int(key[:15], 16)),
                        now(),
                    ),
                )
                if inserted.rowcount:
                    self._order(key, after)  # Never retrofit an already accepted delivery.

    def queue_final(self, task):
        """Publish a fresh terminal card/prose group with durable send barriers."""
        with self.store.transaction():
            row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
            final = self.store.one("SELECT id,text FROM items WHERE id=?", (row["final_item"],))
            existing = final and self.store.one(
                "SELECT d.id FROM deliveries d JOIN delivery_ordering o ON o.delivery_id=d.id WHERE d.item_id=? AND d.kind='message'",
                (final["id"],),
            )
            if existing:
                return  # Atomic group already exists, even if a thread route changed its address.
            cards = []
            if row["channel"] == "discord" and self.discord.settings["enabled"]:
                # Existing final rows belong to the old ordering or a recovered
                # group. Reuse them as-is, including sending/uncertain/sent rows.
                existing = final and self.store.one(
                    "SELECT 1 FROM deliveries WHERE item_id=? AND kind='message'", (final["id"],)
                )
                after = (
                    None
                    if existing
                    else [
                        d["id"]
                        for d in self.store.rows(
                            "SELECT id FROM deliveries WHERE task_id=? AND channel=? AND (address=? OR address='') "
                            "AND kind IN ('message','progress','completion') AND status!='sent' ORDER BY created_at,part",
                            (task, row["channel"], row["address"]),
                        )
                    ]
                )
                cards = self.usage_cards(row, after=after)
            if final:
                # A legacy card may also have been queued before the upgrade.
                # Do not create a cycle with its legacy message-before-card gate.
                ordered = cards and all(
                    self.store.one("SELECT 1 FROM delivery_ordering WHERE delivery_id=?", (card,))
                    for card in cards
                )
                self.queue(final["id"], task, final["text"], after=cards if ordered else None)

    async def flush(self):
        async with self.lock:
            from .activity import queue_records
            from .live_preview import LivePreviews

            previews = LivePreviews(self)
            previews.prepare()
            try:
                queue_records(self)
            except Exception as exc:
                self.store.event("activity.queue_error", {"error": clean(str(exc))[:1000]})
            await self._flush()
            await previews.flush()

    def queue_payload(
        self,
        task,
        identity,
        body,
        files=(),
        *,
        address=None,
        kind="activity",
        notify_owner=False,
        after=None,
    ):
        row = self.store.one("SELECT channel,address FROM tasks WHERE id=?", (task,))
        address = row["address"] if address is None else address
        key = hashlib.sha256(
            f"{task}:{kind}:{identity}:{row['channel']}:{address}".encode()
        ).hexdigest()
        with self.store.transaction():
            if self.store.one("SELECT 1 FROM deliveries WHERE id=?", (key,)):
                return key  # The first durable payload is authoritative.
            self.store.db.execute(
                "INSERT INTO deliveries(id,task_id,channel,address,body,kind,status,nonce,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    task,
                    row["channel"],
                    address,
                    body.get("content", ""),
                    kind,
                    "prepared",
                    str(int(key[:15], 16)),
                    now(),
                ),
            )
            payload = {"body": body, "files": list(files), "notify_owner": notify_owner}
            self.store.db.execute(
                "INSERT INTO delivery_payloads VALUES(?,?)", (key, self.store.put(payload))
            )
            self._order(key, after)
        return key

    def payload(self, row):
        saved = self.store.one(
            "SELECT payload_hash FROM delivery_payloads WHERE delivery_id=?", (row["id"],)
        )
        return self.store.get(saved["payload_hash"]) if saved else None

    def matches(self, row, remote):
        payload = self.payload(row)
        content = remote.get("content", "")
        mention = "<@" + self.discord.settings["owner_id"] + ">"
        expected = (
            row["body"],
            row["body"] + "\n" + mention,
            (row["body"] + "\n" if row["body"] else "") + mention,
        )
        # Compatibility for already-sent paragraph chunks. Only the observed
        # boundary newline normalization is accepted; interior prose stays exact.
        if content not in {*expected, *(value.rstrip("\n") for value in expected)}:
            return False
        if not payload:
            return True

        def includes(expected, actual):
            if isinstance(expected, dict):
                if not isinstance(actual, dict):
                    return False
                for key, value in expected.items():
                    if key not in actual:
                        return False
                    if key == "timestamp":
                        try:
                            left = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                            right = dt.datetime.fromisoformat(actual[key].replace("Z", "+00:00"))
                            if left.replace(
                                microsecond=left.microsecond // 1000 * 1000
                            ) != right.replace(microsecond=right.microsecond // 1000 * 1000):
                                return False
                        except (ValueError, TypeError, AttributeError):
                            return False
                    elif not includes(value, actual[key]):
                        return False
                return True
            if isinstance(expected, list):
                return (
                    isinstance(actual, list)
                    and len(expected) == len(actual)
                    and all(includes(a, b) for a, b in zip(expected, actual))
                )
            return expected == actual

        if not includes(payload["body"].get("embeds", []), remote.get("embeds", [])):
            return False
        wanted = [(f["filename"], f["bytes"]) for f in payload["files"]]
        actual = [(f.get("filename"), f.get("size")) for f in remote.get("attachments", [])]
        return wanted == actual

    async def _flush(self):
        await self.routes()
        for row in self.store.rows(
            "SELECT d.* FROM deliveries d LEFT JOIN delivery_retries r ON r.delivery_id=d.id WHERE d.status='prepared' AND coalesce(r.retry_at,0)<=? ORDER BY d.created_at,d.part",
            (time.time(),),
        ):
            if row["channel"] == "discord" and not self.discord.settings["enabled"]:
                continue
            if row["item_id"] is not None and self.store.one(
                "SELECT id FROM deliveries WHERE item_id=? AND part<? AND status!='sent' LIMIT 1",
                (row["item_id"], row["part"]),
            ):
                continue
            ordering = self.store.one(
                "SELECT predecessors_json FROM delivery_ordering WHERE delivery_id=?", (row["id"],)
            )
            if ordering:
                # Failed, sending, uncertain and backoff-delayed predecessors all
                # hold the barrier. Only durable acknowledgement releases prose.
                if any(
                    not self.store.one(
                        "SELECT 1 FROM deliveries WHERE id=? AND status='sent'", (predecessor,)
                    )
                    for predecessor in json.loads(ordering["predecessors_json"])
                ):
                    continue
            elif row["kind"] == "completion" and self.store.one(
                "SELECT id FROM deliveries WHERE task_id=? AND kind='message' AND status!='sent' AND created_at<=? LIMIT 1",
                (row["task_id"], row["created_at"]),
            ):
                continue  # Legacy cards keep message-first order, not a gate on future turns.
            if row["channel"] == "local":
                self.store.db.execute(
                    "UPDATE deliveries SET status='sent',sent_at=? WHERE id=?", (now(), row["id"])
                )
                continue
            task = self.store.one(
                "SELECT address,status,parent_id,final_item FROM tasks WHERE id=?",
                (row["task_id"],),
            )
            if task and task["status"] == "paused":
                continue
            if not row["address"]:
                if not task or not task["address"]:
                    continue
                row["address"] = task["address"]
                self.store.db.execute(
                    "UPDATE deliveries SET address=? WHERE id=?", (row["address"], row["id"])
                )
            # Keep the existing cosmetic activity placeholder above the whole
            # terminal group, rather than inserting it between card and prose.
            starts_final = (
                row["kind"] == "completion" and ordering and task["final_item"] is not None
            )
            if not self.store.one(
                "SELECT 1 FROM channel_state WHERE channel=?", ("activity:" + row["task_id"],)
            ):
                if starts_final:
                    attempt = "final-activity:" + row["task_id"] + ":" + str(task["final_item"])
                    inserted = self.store.db.execute(
                        "INSERT OR IGNORE INTO channel_state(channel) VALUES(?)", (attempt,)
                    )
                    if inserted.rowcount:
                        # Cosmetic failure/ambiguity must not cause a new POST
                        # between cards or final chunks, including after restart.
                        await self.activity(row["task_id"])
                elif row["kind"] == "message" and not ordering:
                    await self.activity(row["task_id"])
            self.store.db.execute("UPDATE deliveries SET status='sending' WHERE id=?", (row["id"],))
            try:
                payload = self.payload(row)
                body = dict(payload["body"]) if payload else {"content": row["body"]}
                body.update(nonce=row["nonce"], enforce_nonce=True, allowed_mentions={"parse": []})
                last_part = not self.store.one(
                    "SELECT 1 FROM deliveries WHERE task_id=? AND kind=? AND part>? LIMIT 1",
                    (row["task_id"], row["kind"], row["part"]),
                )
                notify_owner = (
                    payload["notify_owner"]
                    if payload
                    else (
                        (row["kind"] == "completion" or row["kind"].startswith("attention:"))
                        and not task["parent_id"]
                        and last_part
                    )
                )
                if notify_owner:
                    owner = self.discord.settings["owner_id"]
                    body["content"] = (
                        (body["content"] + "\n" if body.get("content") else "") + "<@" + owner + ">"
                    )
                    body["allowed_mentions"] = {"parse": [], "users": [owner]}
                files = (
                    [
                        (
                            f"files[{i}]",
                            (f["filename"], self.store.bytes(f["hash"]), f["content_type"]),
                        )
                        for i, f in enumerate(payload["files"])
                    ]
                    if payload
                    else []
                )
                if files:
                    body["attachments"] = [
                        {"id": i, "filename": f["filename"]} for i, f in enumerate(payload["files"])
                    ]
                response = await self.discord.request(
                    "POST",
                    "/channels/" + row["address"] + "/messages",
                    body,
                    **({"files": files} if files else {}),
                )
                self.store.db.execute(
                    "UPDATE deliveries SET status='sent',external_id=?,sent_at=? WHERE id=?",
                    (response["id"], now(), row["id"]),
                )
                self.store.db.execute(
                    "DELETE FROM delivery_retries WHERE delivery_id=?", (row["id"],)
                )
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.PoolTimeout,
                RateLimited,
            ) as exc:
                previous = self.store.one(
                    "SELECT attempts FROM delivery_retries WHERE delivery_id=?", (row["id"],)
                )
                attempts = (previous["attempts"] if previous else 0) + 1
                delay = (
                    exc.seconds if isinstance(exc, RateLimited) else min(60, 2 ** min(attempts, 6))
                )
                self.store.db.execute(
                    "INSERT OR REPLACE INTO delivery_retries VALUES(?,?,?,?)",
                    (row["id"], attempts, time.time() + delay, type(exc).__name__),
                )
                self.store.db.execute(
                    "UPDATE deliveries SET status='prepared',error=? WHERE id=?",
                    (type(exc).__name__, row["id"]),
                )
            except httpx.HTTPStatusError as exc:
                # A definite rejection can be explicitly retried; unknown sends cannot.
                status = "uncertain" if exc.response.status_code >= 500 else "failed"
                self.store.db.execute(
                    "UPDATE deliveries SET status=?,error=? WHERE id=?",
                    (status, str(exc.response.status_code), row["id"]),
                )
            except Exception as exc:
                self.store.db.execute(
                    "UPDATE deliveries SET status='uncertain',error=? WHERE id=?",
                    (clean(str(exc))[:1000], row["id"]),
                )

    def retry_failed(self, delivery_id):
        """Explicit operator retry after a definite rejection, never an unknown send."""
        with self.store.transaction():
            row = self.store.one("SELECT task_id,status FROM deliveries WHERE id=?", (delivery_id,))
            if not row or row["status"] != "failed":
                raise ValueError(
                    "Only a definitely failed delivery can be retried; reconcile uncertain sends"
                )
            self.store.db.execute(
                "UPDATE deliveries SET status='prepared',error=NULL WHERE id=?", (delivery_id,)
            )
            self.store.db.execute(
                "DELETE FROM delivery_retries WHERE delivery_id=?", (delivery_id,)
            )
            self.store.event("delivery.retry_requested", {"delivery": delivery_id}, row["task_id"])

    async def reconcile(self, delivery_id, external_id):
        row = self.store.one("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        remote = await self.discord.request(
            "GET", "/channels/" + row["address"] + "/messages/" + external_id
        )
        if (
            remote.get("author", {}).get("id") != self.discord.bot_id
            or remote.get("channel_id") != row["address"]
            or not self.matches(row, remote)
        ):
            raise ValueError("Message content differs; delivery remains uncertain")
        self.store.db.execute(
            "UPDATE deliveries SET status='sent',external_id=?,sent_at=?,error=NULL WHERE id=?",
            (external_id, now(), delivery_id),
        )

    def acknowledge(self, message):
        nonce = message.get("nonce")
        if nonce is None or message.get("author", {}).get("id") != self.discord.bot_id:
            return
        row = self.store.one("SELECT * FROM deliveries WHERE nonce=?", (str(nonce),))
        if not row or row["address"] != message["channel_id"]:
            return
        if not self.matches(row, message):
            self.store.event(
                "delivery.conflicting_ack",
                {"delivery": row["id"], "message": message["id"]},
                row["task_id"],
            )
            return
        self.store.db.execute(
            "UPDATE deliveries SET status='sent',external_id=?,sent_at=?,error=NULL WHERE id=?",
            (message["id"], now(), row["id"]),
        )

    async def routes(self):
        if not self.discord.settings["enabled"]:
            return
        for row in self.store.rows(
            "SELECT c.* FROM channel_routes c LEFT JOIN route_retries r ON r.task_id=c.task_id WHERE c.status='prepared' AND coalesce(r.retry_at,0)<=?",
            (time.time(),),
        ):
            self.store.db.execute(
                "UPDATE channel_routes SET status='creating' WHERE task_id=?", (row["task_id"],)
            )
            try:
                result = await self.discord.request(
                    "POST",
                    "/channels/" + row["parent_channel"] + "/threads",
                    {"name": row["name"], "type": 11, "auto_archive_duration": 1440},
                )
                with self.store.transaction():
                    self.store.db.execute(
                        "UPDATE channel_routes SET status='ready',external_id=? WHERE task_id=?",
                        (result["id"], row["task_id"]),
                    )
                    self.store.db.execute(
                        "UPDATE tasks SET address=? WHERE id=?", (result["id"], row["task_id"])
                    )
                    self.store.db.execute(
                        "DELETE FROM route_retries WHERE task_id=?", (row["task_id"],)
                    )
                    self.queue(
                        None,
                        row["parent_task"],
                        row["name"] + " → <#" + result["id"] + ">",
                        "worker:" + row["task_id"],
                    )
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.PoolTimeout,
                RateLimited,
            ) as exc:
                previous = self.store.one(
                    "SELECT attempts FROM route_retries WHERE task_id=?", (row["task_id"],)
                )
                attempts = (previous["attempts"] if previous else 0) + 1
                delay = (
                    exc.seconds if isinstance(exc, RateLimited) else min(60, 2 ** min(attempts, 6))
                )
                self.store.db.execute(
                    "INSERT OR REPLACE INTO route_retries VALUES(?,?,?,?)",
                    (row["task_id"], attempts, time.time() + delay, type(exc).__name__),
                )
                self.store.db.execute(
                    "UPDATE channel_routes SET status='prepared',error=? WHERE task_id=?",
                    (type(exc).__name__, row["task_id"]),
                )
            except httpx.HTTPStatusError as exc:
                self.store.db.execute(
                    "UPDATE channel_routes SET status=?,error=? WHERE task_id=?",
                    (
                        "uncertain" if exc.response.status_code >= 500 else "failed",
                        str(exc.response.status_code),
                        row["task_id"],
                    ),
                )
            except Exception as exc:
                self.store.db.execute(
                    "UPDATE channel_routes SET status='uncertain',error=? WHERE task_id=?",
                    (clean(str(exc))[:1000], row["task_id"]),
                )

    async def activity(self, task, final=False):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        if row["channel"] != "discord" or not self.discord.settings["enabled"]:
            return
        if not row["address"] and not final:
            return
        calls = self.store.rows(
            "SELECT name,status FROM operations WHERE task_id=? ORDER BY rowid DESC LIMIT 5",
            (task,),
        )
        body = "\n".join(f"{x['name']}: {x['status']}" for x in reversed(calls)) or "Working…"
        if final:
            self.usage_cards(row)
            return
        # Cosmetic updates have their own failure path, never blocking final delivery.
        state = self.store.one("SELECT * FROM channel_state WHERE channel=?", ("activity:" + task,))
        try:
            if state and state["activity_id"]:
                await self.discord.request(
                    "PATCH",
                    f"/channels/{row['address']}/messages/{state['activity_id']}",
                    {"content": body},
                )
            else:
                result = await self.discord.request(
                    "POST",
                    f"/channels/{row['address']}/messages",
                    {"content": body, "allowed_mentions": {"parse": []}},
                )
                self.store.db.execute(
                    "INSERT OR REPLACE INTO channel_state(channel,activity_id) VALUES(?,?)",
                    ("activity:" + task, result["id"]),
                )
        except Exception as exc:
            self.store.event("activity.error", {"error": clean(str(exc))[:500]}, task)

    def usage_cards(self, task, *, after=None):
        cards = []
        owners = self.store.rows(
            "SELECT t.*,i.text FROM owner_turns t JOIN items i ON i.id=t.source_item WHERE t.task_id=? AND (t.reply_item=? OR t.replied_at IS NULL) ORDER BY t.source_item",
            (task["id"], task["final_item"]),
        )
        for index, owner in enumerate(owners or [None]):
            if owner:
                breakdown = turns.usage_by_model(self.store, turn=owner["id"])
                identity = owner["id"]
                scope = "Owner message → reply · includes workers, helpers and retries"
                question = owner["text"]
                started = owner["created_at"]
                finished = owner["replied_at"] or now()
            else:
                breakdown = turns.usage_by_model(self.store, task=task["id"])
                identity = task["id"]
                scope = (
                    "Worker assignment subtotal"
                    if task["parent_id"]
                    else "Legacy task totals · owner-message attribution unavailable"
                )
                question = task["objective"]
                started = task["created_at"]
                finished = task["updated_at"]
            seconds = max(
                0,
                int(
                    (
                        dt.datetime.fromisoformat(finished) - dt.datetime.fromisoformat(started)
                    ).total_seconds()
                ),
            )
            context = turns.latest_context(self.store, task["id"], owner["id"] if owner else None)
            profiles = self.store.config.data["profiles"]
            footer = (
                scope
                + " · Context = answering agent’s last request · Token usage, not a dollar bill"
            )

            def amount(values, key):
                return f"**{values[key]:,}**" if values[key + "_calls"] else "Unreported"

            def field(name, values):
                value = (
                    f"**{values['calls']:,}** model calls\n"
                    f"{amount(values, 'cached')} cached input tokens\n"
                    f"{amount(values, 'uncached')} uncached input tokens\n"
                    f"{amount(values, 'output')} output tokens"
                )
                if context and context["model"] == values["model"]:
                    size = context["input_tokens"]
                    profile = profiles[context["role"]]
                    window = (
                        f" / {profile['context_limit']:,}"
                        if profile["model"] == context["model"]
                        else ""
                    )
                    value += "\nContext (last request): " + (
                        f"**{size:,}{window}** tokens" if size is not None else "Unreported"
                    )
                if values["unknown_calls"]:
                    value += (
                        f"\nIncomplete usage: {values['unknown_calls']:,} calls; known totals only"
                    )
                return {"name": name, "value": value, "inline": True}

            def label(row):
                roles = [
                    role
                    for role in ("astra", "sol", "luna")
                    if profiles[role]["model"] == row["model"]
                ]
                return roles[0].title() if len(roles) == 1 else row["model"]

            breakdown.sort(
                key=lambda row: (
                    ["Astra", "Sol", "Luna"].index(label(row))
                    if label(row) in ("Astra", "Sol", "Luna")
                    else 3,
                    row["model"],
                )
            )
            embed = {
                "title": task["status"].title() + " · Turn usage",
                "description": question[:500],
                "color": 0x57F287 if task["status"] == "completed" else 0xFEE75C,
                "fields": (
                    [field(label(row), row) for row in breakdown]
                    or [{"name": "Usage", "value": "No model calls", "inline": False}]
                )
                + [
                    {
                        "name": "Elapsed",
                        "value": f"{seconds // 60}m {seconds % 60}s",
                        "inline": False,
                    }
                ],
                "footer": {"text": footer},
                "timestamp": finished,
            }
            card = self.queue_payload(
                task["id"],
                identity,
                {"embeds": [embed]},
                kind="completion",
                notify_owner=not task["parent_id"] and index == max(0, len(owners) - 1),
                after=after,
            )
            cards.append(card)
            if after is not None:
                after = [card]  # The last card/mention follows earlier owner cards.
        return cards

    async def typing(self):
        if not self.discord.settings["enabled"]:
            return
        for row in self.store.rows(
            "SELECT id,address FROM tasks WHERE channel='discord' AND status='running' AND address!=''"
        ):
            if time.monotonic() - self.last_typing.get(row["id"], -10) < 8:
                continue
            self.last_typing[row["id"]] = time.monotonic()
            try:
                await self.discord.request("POST", "/channels/" + row["address"] + "/typing")
            except Exception as exc:
                self.store.event("typing.error", {"error": clean(str(exc))[:500]}, row["id"])
