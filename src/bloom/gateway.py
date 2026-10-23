"""Discord gateway intake and acknowledgements; delivery remains in its outbox."""

from __future__ import annotations

import asyncio
import json
import random
import time
from urllib.parse import urlparse

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .privacy import clean
from .store import now


class CatchupLimit(RuntimeError):
    pass


class Gateway:
    def __init__(self, store, discord, on_message, catch_up=False):
        self.store = store
        self.discord = discord
        self.on_message = on_message
        self.stopping = False
        self.ready = asyncio.Event()
        self.status = "connecting"
        self.catch_up = catch_up

    def addresses(self):
        return {self.discord.settings["channel_id"]} | {
            r["external_id"]
            for r in self.store.rows("SELECT external_id FROM channel_routes WHERE status='ready'")
        }

    async def initialize_cursors(self):
        for address in sorted(self.addresses()):
            if self.store.one("SELECT 1 FROM discord_cursors WHERE address=?", (address,)):
                continue
            if address == self.discord.settings["channel_id"]:
                latest = await self.discord.request(
                    "GET", "/channels/" + address + "/messages?limit=1"
                )
                baseline = max((int(m["id"]) for m in latest), default=0)
            else:
                # These are v3-created threads; their snowflake is the lower bound.
                baseline = int(address)
            self.store.db.execute(
                "INSERT OR IGNORE INTO discord_cursors VALUES(?,?,?)",
                (address, str(baseline), now()),
            )

    def receive(self, message):
        with self.store.transaction():
            self.on_message(message)
            address = message["channel_id"]
            if self.catch_up and address in self.addresses():
                old = self.store.one(
                    "SELECT message_id FROM discord_cursors WHERE address=?", (address,)
                )
                cursor = max(int(message["id"]), int(old["message_id"]) if old else 0)
                self.store.db.execute(
                    "INSERT OR REPLACE INTO discord_cursors VALUES(?,?,?)",
                    (address, str(cursor), now()),
                )

    async def reconcile_history(self):
        """Read backwards, then accept oldest-first; never assume after-page order."""
        await self.initialize_cursors()
        for address in sorted(self.addresses()):
            boundary = int(
                self.store.one(
                    "SELECT message_id FROM discord_cursors WHERE address=?", (address,)
                )["message_id"]
            )
            before, pending = None, {}
            while True:
                path = "/channels/" + address + "/messages?limit=100"
                page = await self.discord.request(
                    "GET", path + ("&before=" + str(before) if before else "")
                )
                if not page:
                    break
                for message in page:
                    if int(message["id"]) > boundary:
                        pending[int(message["id"])] = message
                if len(pending) > self.discord.settings.get("max_recovery_messages", 5000):
                    raise CatchupLimit(
                        "Discord recovery exceeds max_recovery_messages; cursor retained. Increase the configured recovery bound after inspecting the backlog."
                    )
                oldest = min(int(m["id"]) for m in page)
                if oldest <= boundary or len(page) < 100:
                    break
                if before is not None and oldest >= before:
                    raise RuntimeError(
                        "Discord history pagination did not advance; cursor retained"
                    )
                before = oldest
            for identifier in sorted(pending):
                self.receive(pending[identifier])

    def state(self):
        return self.store.one("SELECT * FROM channel_state WHERE channel='discord_gateway'")

    async def session(self):
        if self.catch_up:
            # Close any older REST gap before accepting newer replay events;
            # otherwise a newer Gateway message could advance past a missing one.
            await self.reconcile_history()
        previous = self.state()
        if previous and previous["resume_url"] and previous["session_id"]:
            url = previous["resume_url"]
        else:
            url = (await self.discord.request("GET", "/gateway/bot"))["url"]
        parsed = urlparse(url)
        if parsed.scheme != "wss" or not (parsed.hostname or "").endswith(".discord.gg"):
            raise RuntimeError("Unexpected gateway destination; token was not sent")
        token = self.discord.token()
        seq = int(previous["cursor"]) if previous and previous["cursor"] else None
        async with connect(
            url.rstrip("/") + "/?v=10&encoding=json", ping_interval=None, max_size=8_000_000
        ) as socket:
            hello = json.loads(await asyncio.wait_for(socket.recv(), 30))
            if hello.get("op") != 10:
                raise RuntimeError("Expected Discord gateway Hello")
            interval = hello["d"]["heartbeat_interval"] / 1000
            acknowledged = True

            async def heartbeat():
                nonlocal acknowledged
                await asyncio.sleep(interval * random.random())
                while True:
                    if not acknowledged:
                        await socket.close()
                        return
                    acknowledged = False
                    await socket.send(json.dumps({"op": 1, "d": seq}))
                    await asyncio.sleep(interval)

            beats = asyncio.create_task(heartbeat())
            try:
                if previous and previous["session_id"] and seq is not None:
                    await socket.send(
                        json.dumps(
                            {
                                "op": 6,
                                "d": {
                                    "token": token,
                                    "session_id": previous["session_id"],
                                    "seq": seq,
                                },
                            }
                        )
                    )
                else:
                    await socket.send(
                        json.dumps(
                            {
                                "op": 2,
                                "d": {
                                    "token": token,
                                    "intents": (1 << 0) | (1 << 9) | (1 << 15),
                                    "properties": {
                                        "os": "macos",
                                        "browser": "bloom",
                                        "device": "bloom",
                                    },
                                },
                            }
                        )
                    )
                async for raw in socket:
                    event = json.loads(raw)
                    if event["op"] == 11:
                        acknowledged = True
                    elif event["op"] == 1:
                        await socket.send(json.dumps({"op": 1, "d": seq}))
                    elif event["op"] == 7:
                        return
                    elif event["op"] == 9:
                        if not event["d"]:
                            self.store.db.execute(
                                "DELETE FROM channel_state WHERE channel='discord_gateway'"
                            )
                        return
                    elif event["op"] == 0:
                        # The cursor advances only after durable intake/ack handling.
                        if event["t"] == "READY":
                            data = event["d"]
                            self.discord.bot_id = data["user"]["id"]
                            self.store.db.execute(
                                "INSERT OR REPLACE INTO channel_state(channel,cursor,session_id,resume_url) VALUES('discord_gateway',?,?,?)",
                                (str(event["s"]), data["session_id"], data["resume_gateway_url"]),
                            )
                            if self.catch_up:
                                await self.reconcile_history()
                            self.ready.set()
                            self.status = "ready"
                        elif event["t"] == "RESUMED":
                            if self.catch_up:
                                await self.reconcile_history()
                            self.ready.set()
                            self.status = "ready"
                        elif event["t"] == "MESSAGE_CREATE":
                            self.receive(event["d"])
                        seq = event.get("s", seq)
                        self.store.db.execute(
                            "UPDATE channel_state SET cursor=? WHERE channel='discord_gateway'",
                            (str(seq),),
                        )
            finally:
                beats.cancel()
                await asyncio.gather(beats, return_exceptions=True)

    async def run(self):
        delay = 1
        while not self.stopping:
            try:
                await self.session()
                delay = 1
            except asyncio.CancelledError:
                raise
            except CatchupLimit as exc:
                self.ready.clear()
                self.status = "blocked"
                self.store.event("discord.gateway_blocked", {"error": str(exc)})
                return
            except ConnectionClosed as exc:
                self.ready.clear()
                code = exc.rcvd.code if exc.rcvd else None
                if code in (4007, 4009):
                    self.store.db.execute(
                        "DELETE FROM channel_state WHERE channel='discord_gateway'"
                    )
                self.store.event("discord.gateway_closed", {"code": code, "retry_seconds": delay})
                if code in (4004, 4010, 4011, 4012, 4013, 4014):
                    self.status = "blocked"
                    self.store.event(
                        "discord.gateway_blocked",
                        {
                            "code": code,
                            "action": "Correct credentials or gateway/intents configuration before restarting this component.",
                        },
                    )
                    return
            except Exception as exc:
                self.store.event(
                    "discord.gateway_error",
                    {"error": clean(str(exc))[:500], "retry_seconds": delay},
                )
            self.ready.clear()
            self.status = "reconnecting"
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)
