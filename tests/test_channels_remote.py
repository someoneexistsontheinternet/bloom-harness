import json
import subprocess
import sys
from pathlib import Path

import httpx

from bloom.delivery import Delivery, Discord
from bloom.mcp_server import dispatch
from bloom.remote_files import handle
from bloom.runtime import Runtime


def test_remote_file_helper_exact_edits_and_source_hash(store):
    root = store.config.root
    first = handle(
        {
            "name": "write_file",
            "root": str(root),
            "path": "remote.txt",
            "text": "remote data\n",
            "expected_sha256": "absent",
        }
    )
    read = handle({"name": "read_file", "root": str(root), "path": "remote.txt"})
    assert read["sha256"] == first["sha256"]
    handle(
        {
            "name": "patch_file",
            "root": str(root),
            "path": "remote.txt",
            "old": "data",
            "new": "evidence",
            "expected_sha256": first["sha256"],
        }
    )
    assert (root / "remote.txt").read_text() == "remote evidence\n"


async def test_worker_threads_are_separate_and_do_not_add_owner(store, monkeypatch):
    store.config.data["discord"]["enabled"] = True
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "fixture")
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        return httpx.Response(
            200, json={"id": "thread-1" if request.url.path.endswith("/threads") else "message-1"}
        )

    discord = Discord(store, httpx.MockTransport(handler))
    delivery = Delivery(store, discord)
    runtime = Runtime(store, delivery=delivery)
    parent = store.new_task("owner task", channel="discord", address="owner-channel")
    child = await runtime.request(
        "astra",
        parent,
        "spawn",
        {"operation": "delegate", "role": "sol", "objective": "Inspect evidence"},
    )
    item = store.item(
        child["agent_id"],
        {
            "type": "message",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "Working in the child thread"}],
        },
        child["task_id"],
    )
    delivery.queue(item, child["task_id"], "Working in the child thread")
    await delivery.flush()
    assert (
        store.one("SELECT address FROM tasks WHERE id=?", (child["task_id"],))["address"]
        == "thread-1"
    )
    assert any(path.endswith("/thread-1/messages") for path, body in requests)
    assert not any("/thread-members/" in path for path, body in requests)
    assert all(
        body.get("allowed_mentions", {"parse": []}) == {"parse": []} for path, body in requests
    )


async def test_mcp_frontend_queues_into_same_astra(store):
    init = dispatch(store, "initialize", {"protocolVersion": "2099-01-01"})
    assert init["protocolVersion"] == "2025-06-18"
    dispatch(
        store,
        "tools/call",
        {"name": "bloom_message", "arguments": {"text": "Remember the owner objective"}},
    )
    runtime = Runtime(store)
    await runtime.inbox()
    assert store.one("SELECT agent_id FROM tasks")["agent_id"] == "astra"
    result = dispatch(store, "tools/call", {"name": "bloom_status", "arguments": {}})
    assert "Remember the owner objective" in result["content"][0]["text"]


async def test_worker_final_card_waits_for_thread_route(store, monkeypatch):
    store.config.data["discord"]["enabled"] = True
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "fixture-delayed-route")

    def handler(request):
        return httpx.Response(
            200, json={"id": "thread" if request.url.path.endswith("/threads") else "message"}
        )

    delivery = Delivery(store, Discord(store, httpx.MockTransport(handler)))
    runtime = Runtime(store, delivery=delivery)
    parent = store.new_task("owner", channel="discord", address="main")
    child = await runtime.request(
        "astra",
        parent,
        "child",
        {"operation": "delegate", "role": "sol", "objective": "Quick worker"},
    )
    store.status(child["task_id"], "completed")
    await delivery.activity(child["task_id"], final=True)
    assert (
        store.one("SELECT status FROM deliveries WHERE kind='completion'")["status"] == "prepared"
    )
    await delivery.flush()
    assert store.one("SELECT address,status FROM deliveries WHERE kind='completion'") == {
        "address": "thread",
        "status": "sent",
    }
