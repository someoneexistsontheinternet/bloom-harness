import asyncio
import json

import httpx
import pytest

from bloom.delivery import Delivery, Discord
from bloom.models import Models
from bloom.runtime import Runtime
from bloom.store import now, uid


def message(text="done", phase="final_answer"):
    return {
        "type": "message",
        "role": "assistant",
        "phase": phase,
        "content": [{"type": "output_text", "text": text}],
    }


def saved(store, task, output, item_ids=None):
    row = store.one(
        "SELECT t.agent_id,a.role FROM tasks t JOIN agents a ON a.id=t.agent_id WHERE t.id=?",
        (task,),
    )
    call = uid("call")
    store.db.execute(
        "INSERT INTO model_calls(id,task_id,agent_id,purpose,model_requested,provider,status,request_hash,response_hash,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            call,
            task,
            row["agent_id"],
            "turn",
            store.config.profile(row["role"])["model"],
            "fixture",
            "completed",
            store.put({}),
            store.put({"output": output, "status": "completed"}),
            now(),
        ),
    )
    if item_ids is None:
        item_ids = [
            r["id"]
            for r in store.rows(
                "SELECT id FROM items WHERE session_id=? ORDER BY id", (row["agent_id"],)
            )
        ]
    store.db.executemany(
        "INSERT INTO model_call_items VALUES(?,?,?)",
        [(call, item, i) for i, item in enumerate(item_ids)],
    )
    from bloom.turns import attribute_call, current

    attribute_call(store, call, current(store, task))
    return call


async def test_completed_inference_recovered_without_purchase(store):
    task = store.new_task("saved reply")
    saved(store, task, [message("Original answer")])
    runtime = Runtime(store)

    async def forbidden(*a, **k):
        raise AssertionError("Completed inference must not be repeated")

    runtime.models.infer = forbidden
    await runtime.run(until_idle=True)
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert len(store.rows("SELECT * FROM model_calls")) == 1
    assert len(store.rows("SELECT * FROM deliveries")) == 1


def test_discord_paragraph_boundary_normalization_preserves_native_source_and_checks_interior(
    store,
):
    task = store.new_task("Check paragraph display")
    delivery = Delivery(store)
    source = "First paragraph.\n\nSecond paragraph.\n\n"
    item = store.item("astra", message(source), task)
    delivery.queue(item, task, source)
    row = store.one("SELECT * FROM deliveries")
    assert row["body"] == "First paragraph.\n\nSecond paragraph."
    assert store.one("SELECT text FROM items WHERE id=?", (item,))["text"] == source
    legacy = {**row, "body": source}
    assert delivery.matches(legacy, {"content": source.rstrip("\n")})
    assert not delivery.matches(legacy, {"content": source.replace("\n\n", " ").rstrip()})
    assert not delivery.matches(legacy, {"content": "First paragraph.\n\nChanged claim."})


async def test_recovery_does_not_replay_ambiguous_effect(store):
    task = store.new_task("uncertain send")
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
        ("effect", task, "tool", "write_file", "running", store.put({"path": "x"}), now()),
    )
    runtime = Runtime(store)
    runtime.recover()
    assert store.one("SELECT status FROM operations")["status"] == "uncertain"
    assert store.one("SELECT status FROM tasks")["status"] == "blocked"


async def test_actual_role_tree_and_all_of_wait(store):
    runtime = Runtime(store)
    root = store.new_task("owner objective")
    sol = await runtime.request(
        "astra",
        root,
        "sol",
        {"operation": "delegate", "role": "sol", "objective": "Own assignment"},
    )
    luna = await runtime.request(
        sol["agent_id"],
        sol["task_id"],
        "luna",
        {"operation": "delegate", "role": "luna", "objective": "Inspect evidence"},
    )
    assert (
        store.one("SELECT parent_id FROM agents WHERE id=?", (luna["agent_id"],))["parent_id"]
        == sol["agent_id"]
    )
    await runtime.request(
        "astra", root, "wait", {"operation": "wait", "children": [sol["task_id"], luna["task_id"]]}
    )
    store.status(luna["task_id"], "completed")
    runtime.wake_waits()
    assert store.one("SELECT status FROM tasks WHERE id=?", (root,))["status"] == "waiting"
    store.status(sol["task_id"], "completed")
    runtime.wake_waits()
    assert store.one("SELECT status FROM tasks WHERE id=?", (root,))["status"] == "ready"


async def test_discord_uncertain_send_is_not_retried(store, monkeypatch):
    store.config.data["discord"]["enabled"] = True
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "test-credential")
    attempts = []

    def handler(request):
        if "nonce" not in json.loads(request.content):
            return httpx.Response(200, json={"id": "activity"})
        attempts.append(request)
        raise httpx.ReadTimeout("lost acknowledgement")

    discord = Discord(store, httpx.MockTransport(handler))
    delivery = Delivery(store, discord)
    task = store.new_task("deliver", channel="discord", address="123")
    item = store.item("astra", message("hello"), task)
    delivery.queue(item, task, "hello")
    await delivery.flush()
    await delivery.flush()
    assert len(attempts) == 1
    assert store.one("SELECT status FROM deliveries")["status"] == "uncertain"


async def test_prose_with_tool_call_is_delivered_separately(store):
    task = store.new_task("prose and tools")
    runtime = Runtime(store)
    response = {
        "output": [
            message("I will inspect the file.", "commentary"),
            {
                "type": "function_call",
                "call_id": "native",
                "name": "read_file",
                "arguments": '{"path":"README.md"}',
            },
        ]
    }
    call = saved(store, task, response["output"])
    runtime.ingest_response(task, call, response)
    assert (
        store.one("SELECT body FROM deliveries")["body"]
        == "Tools: `read_file`\n\nI will inspect the file."
    )
    assert store.one("SELECT name FROM tool_calls")["name"] == "read_file"
    assert store.one("SELECT status FROM tasks")["status"] != "completed"


async def test_connect_failure_retries_saved_delivery_without_inference(store, monkeypatch):
    store.config.data["discord"]["enabled"] = True
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "fixture")
    calls = []

    def handler(request):
        if "nonce" not in json.loads(request.content):
            return httpx.Response(200, json={"id": "activity"})
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("DNS temporarily unavailable")
        return httpx.Response(200, json={"id": "delivered"})

    delivery = Delivery(store, Discord(store, httpx.MockTransport(handler)))
    task = store.new_task("recover delivery", channel="discord", address="123")
    item = store.item("astra", message("Saved answer"), task)
    delivery.queue(item, task, "Saved answer")
    await delivery.flush()
    assert store.one("SELECT status FROM deliveries")["status"] == "prepared"
    store.db.execute("UPDATE delivery_retries SET retry_at=0")
    await delivery.flush()
    assert store.one("SELECT status FROM deliveries")["status"] == "sent"
    assert len(calls) == 2 and not store.rows("SELECT * FROM model_calls")


async def test_gateway_ack_reconciles_ambiguous_post(store, monkeypatch):
    store.config.data["discord"]["enabled"] = True
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "fixture")

    def handler(request):
        raise httpx.ReadTimeout("Acknowledgement lost")

    delivery = Delivery(store, Discord(store, httpx.MockTransport(handler)))
    task = store.new_task("lost ack", channel="discord", address="123")
    item = store.item("astra", message("Saved answer"), task)
    delivery.queue(item, task, "Saved answer")
    await delivery.flush()
    row = store.one("SELECT * FROM deliveries")
    assert row["status"] == "uncertain"
    delivery.acknowledge(
        {
            "id": "confirmed",
            "channel_id": "123",
            "nonce": row["nonce"],
            "content": "Saved answer",
            "author": {"id": delivery.discord.bot_id},
        }
    )
    assert store.one("SELECT status,external_id FROM deliveries") == {
        "status": "sent",
        "external_id": "confirmed",
    }
