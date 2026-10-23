import json

import httpx

from bloom.delivery import Delivery, Discord, chunks
from bloom.runtime import Runtime
from bloom.store import now
from test_runtime_delivery import message


async def test_distinct_prose_rolling_five_feed_and_frozen_completion_survive_cosmetic_failure(
    store, monkeypatch
):
    store.config.data["discord"]["enabled"] = True
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "fixture-rendering")
    requests = []

    def handler(request):
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, body))
        if request.method == "PATCH":
            return httpx.Response(500, json={"message": "cosmetic failure"})
        return httpx.Response(200, json={"id": "message-" + str(len(requests))})

    delivery = Delivery(store, Discord(store, httpx.MockTransport(handler)))
    task = store.new_task("Deliver a usable answer", channel="discord", address="fixture-channel")
    first = store.item("astra", message("I will inspect the evidence.", "commentary"), task)
    delivery.queue(first, task, "I will inspect the evidence.")
    await delivery.flush()
    assert "nonce" not in requests[0][2]  # Activity placeholder appears above prose.
    for i in range(7):
        store.db.execute(
            "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
            (str(i), task, "tool", "read-" + str(i), "completed", store.put({}), now()),
        )
    await delivery.activity(task)
    feed = [body["content"] for method, path, body in requests if method == "PATCH"][-1]
    assert feed.splitlines() == ["read-" + str(i) + ": completed" for i in range(2, 7)]
    final = store.item("astra", message("The exact result is verified."), task)
    delivery.queue(final, task, "The exact result is verified.")
    store.status(task, "completed")
    await delivery.activity(task, final=True)
    await delivery.flush()
    frozen = store.one("SELECT body FROM deliveries WHERE kind='completion'")["body"]
    await delivery.activity(task, final=True)
    await delivery.flush()
    assert store.one("SELECT body FROM deliveries WHERE kind='completion'")["body"] == frozen
    assert len(store.rows("SELECT * FROM deliveries WHERE kind='completion'")) == 1
    sent = [body for method, path, body in requests if method == "POST" and "nonce" in body]
    assert [body["content"] for body in sent[:2]] == [
        "I will inspect the evidence.",
        "The exact result is verified.",
    ]
    assert "usage" not in feed.lower() and "cost" not in feed.lower()
    assert sum(bool(body["allowed_mentions"].get("users")) for body in sent) == 1
    assert store.one("SELECT 1 FROM events WHERE kind='activity.error'")


async def test_disabled_discord_cannot_send_retained_outbox(store, monkeypatch):
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "fixture-disabled")

    def forbidden(request):
        raise AssertionError("Disabled Discord must not make a network request")

    delivery = Delivery(store, Discord(store, httpx.MockTransport(forbidden)))
    task = store.new_task("Keep the retained answer", channel="discord", address="fixture")
    item = store.item("astra", message("Retained answer"), task)
    delivery.queue(item, task, "Retained answer")
    await delivery.flush()
    assert store.one("SELECT status FROM deliveries")["status"] == "prepared"


def test_long_code_is_split_into_readable_fenced_messages():
    text = "Intro\n```python\n" + 'print("evidence")\n' * 250 + "```\nConclusion"
    parts = chunks(text)
    assert len(parts) > 2 and all(len(p) <= 1900 for p in parts)
    assert all(p.count("```") % 2 == 0 for p in parts)
    assert sum(p.count('print("evidence")') for p in parts) == 250
    assert parts[0].startswith("Intro") and parts[-1].endswith("Conclusion")
