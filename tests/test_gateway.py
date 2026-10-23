import json
from urllib.parse import parse_qs, urlparse

import pytest

from bloom.gateway import Gateway
from bloom.runtime import Runtime
from bloom.store import now
from bloom.gateway import CatchupLimit


async def test_gateway_resumes_after_durable_intake_without_duplicate_task(store, monkeypatch):
    runtime = Runtime(store)
    discord = runtime.delivery.discord
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "fixture-gateway")
    settings = store.config.data["discord"]
    owner_message = {
        "id": "42",
        "channel_id": settings["channel_id"],
        "content": "One owner task",
        "author": {"id": settings["owner_id"]},
    }
    sequences = [
        [
            {
                "op": 0,
                "t": "READY",
                "s": 1,
                "d": {
                    "user": {"id": discord.bot_id},
                    "session_id": "durable-session",
                    "resume_gateway_url": "wss://gateway.discord.gg",
                },
            },
            {"op": 0, "t": "MESSAGE_CREATE", "s": 2, "d": owner_message},
            {"op": 7},
        ],
        [
            {"op": 0, "t": "RESUMED", "s": 3, "d": {}},
            {"op": 0, "t": "MESSAGE_CREATE", "s": 4, "d": owner_message},
            {"op": 7},
        ],
    ]
    sent = []

    class Socket:
        async def __aenter__(self):
            self.events = iter(sequences.pop(0))
            return self

        async def __aexit__(self, *args):
            return False

        async def recv(self):
            return json.dumps({"op": 10, "d": {"heartbeat_interval": 60000}})

        async def send(self, text):
            sent.append(json.loads(text))

        async def close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return json.dumps(next(self.events))
            except StopIteration:
                raise StopAsyncIteration

    monkeypatch.setattr("bloom.gateway.connect", lambda *a, **k: Socket())

    async def metadata(method, path, body=None):
        assert (method, path) == ("GET", "/gateway/bot")
        return {"url": "wss://gateway.discord.gg"}

    discord.request = metadata
    gateway = Gateway(store, discord, runtime.receive_discord)
    await gateway.session()
    await gateway.session()
    assert [s["op"] for s in sent] == [2, 6]
    assert sent[1]["d"]["seq"] == 2 and sent[1]["d"]["session_id"] == "durable-session"
    assert len(store.rows("SELECT * FROM tasks")) == 1
    assert len(store.rows("SELECT * FROM inbound_messages")) == 1
    assert gateway.state()["cursor"] == "4"


async def test_fresh_gateway_baseline_ignores_old_history_then_recovers_multiple_pages(store):
    runtime = Runtime(store)
    discord = runtime.delivery.discord
    settings = discord.settings
    history = [
        {
            "id": str(n),
            "channel_id": settings["channel_id"],
            "author": {"id": settings["owner_id"]},
            "content": "Owner input " + str(n),
        }
        for n in range(1, 251)
    ]
    calls = []

    async def request(method, path, body=None):
        query = parse_qs(urlparse(path).query)
        calls.append(query)
        before = int(query.get("before", ["999999"])[0])
        return sorted(
            [m for m in history if int(m["id"]) < before], key=lambda m: int(m["id"]), reverse=True
        )[: int(query["limit"][0])]

    discord.request = request
    gateway = Gateway(store, discord, runtime.receive_discord, catch_up=True)
    await gateway.initialize_cursors()
    assert store.one("SELECT message_id FROM discord_cursors")["message_id"] == "250"
    history.extend(
        {**history[0], "id": str(n), "content": "Owner input " + str(n)} for n in range(251, 506)
    )
    await gateway.reconcile_history()
    messages = store.rows("SELECT external_id FROM inbound_messages ORDER BY rowid")
    assert [int(m["external_id"]) for m in messages] == list(range(251, 506))
    assert len(store.rows("SELECT id FROM tasks")) == 1
    # Same durable cursor also survives a non-resumable gateway session reset.
    store.db.execute("DELETE FROM channel_state WHERE channel='discord_gateway'")
    gateway = Gateway(store, discord, runtime.receive_discord, catch_up=True)
    await gateway.reconcile_history()
    assert len(store.rows("SELECT external_id FROM inbound_messages")) == 255
    assert any("before" in c for c in calls)


async def test_catchup_cursor_waits_for_durable_intake_and_retains_gap_on_failure(store):
    runtime = Runtime(store)
    discord = runtime.delivery.discord
    address = discord.settings["channel_id"]
    store.db.execute("INSERT INTO discord_cursors VALUES(?,?,?)", (address, "10", now()))

    async def request(*args, **kwargs):
        return [
            {
                "id": str(n),
                "channel_id": address,
                "author": {"id": discord.settings["owner_id"]},
                "content": str(n),
            }
            for n in (13, 12, 11)
        ]

    discord.request = request

    def accept_then_fail(message):
        runtime.receive_discord(message)
        if message["id"] == "12":
            raise OSError("Intake persistence failed")

    gateway = Gateway(store, discord, accept_then_fail, catch_up=True)
    with pytest.raises(OSError, match="persistence failed"):
        await gateway.reconcile_history()
    assert store.one("SELECT message_id FROM discord_cursors")["message_id"] == "11"
    assert [r["external_id"] for r in store.rows("SELECT external_id FROM inbound_messages")] == [
        "11"
    ]
    gateway.on_message = runtime.receive_discord
    await gateway.reconcile_history()
    assert store.one("SELECT message_id FROM discord_cursors")["message_id"] == "13"
    assert len(store.rows("SELECT * FROM inbound_messages")) == 3


async def test_large_gap_is_explicitly_blocked_without_advancing_cursor(store):
    runtime = Runtime(store)
    discord = runtime.delivery.discord
    address = discord.settings["channel_id"]
    store.db.execute("INSERT INTO discord_cursors VALUES(?,?,?)", (address, "1", now()))
    discord.settings["max_recovery_messages"] = 2

    async def request(*args, **kwargs):
        return [{"id": str(n), "channel_id": address} for n in (4, 3, 2)]

    discord.request = request
    gateway = Gateway(store, discord, runtime.receive_discord, catch_up=True)
    with pytest.raises(CatchupLimit):
        await gateway.reconcile_history()
    assert store.one("SELECT message_id FROM discord_cursors")["message_id"] == "1"
    assert not store.rows("SELECT * FROM inbound_messages")
