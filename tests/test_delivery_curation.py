import json
import os
import time
from pathlib import Path

import httpx
import pytest

from bloom import turns
from bloom.delivery import Delivery, Discord, chunks
from bloom.models import Models
from bloom.runtime import Runtime
from bloom.storage import ColdStorageUnavailable
from test_control_provenance import invoke
from test_exact_large_output import curator, last_result
from test_runtime_delivery import message, saved
from test_storage import archive


async def test_named_results_compress_after_inactivity_and_search_offline(
    store, archive, monkeypatch
):
    runtime = Runtime(store)
    task = store.new_task("Find searchable evidence")
    curator(runtime, store, monkeypatch)
    source = "firstkeyword\n" + "ordinary evidence\n" * 4000 + "lastkeyword\n"
    (store.config.root / "source.txt").write_text(source)
    await invoke(runtime, store, task, "read_file", {"path": "source.txt"}, "large")
    path = Path(last_result(store)["full_result_path"])
    old = time.time() - 8 * 86400
    os.utime(path, (old, old))
    store.status(task, "completed")
    cutoff = time.time() - 7 * 86400
    assert archive.offload(path, cutoff) is None
    store.db.execute("UPDATE tool_result_files SET accessed_at=?", (old,))
    assert archive.offload(path, cutoff) == len(source.encode())
    relative = str(path.relative_to(store.config.root))
    row = archive.record(relative)
    compressed = archive.object_path(row["sha256"]).with_suffix(".gz")
    assert compressed.exists() and compressed.stat().st_size < len(source.encode()) // 10
    assert archive.read(relative) == source.encode()
    assert archive.range(relative, 0, 12)["output"] == "firstkeyword"
    archive.online["value"] = False
    assert archive.search("firstkeyword lastkeyword")[0]["path"] == relative
    with pytest.raises(ColdStorageUnavailable):
        archive.read(relative)
    assert not path.exists()
    archive.online["value"] = True
    archive.restore(row["id"])
    assert path.read_text() == source


async def test_compressed_original_envelope_is_readable_through_store(store, archive, monkeypatch):
    runtime = Runtime(store)
    task = store.new_task("Archive a retained original")
    curator(runtime, store, monkeypatch)
    (store.config.root / "source.txt").write_text("retained exact evidence\n" * 4000)
    operation = await invoke(runtime, store, task, "read_file", {"path": "source.txt"}, "large")
    digest = operation["result_hash"]
    original = store.get(digest)
    path = store.artifact_path(digest)
    old = time.time() - 8 * 86400
    os.utime(path, (old, old))
    store.status(task, "completed")
    store.db.execute("UPDATE artifact_access SET accessed_at=? WHERE hash=?", (old, digest))
    assert archive.offload(path, time.time() - 7 * 86400)
    assert archive.object_path(digest).with_suffix(".gz").exists()
    monkeypatch.setattr("bloom.storage.Archive.volume_ready", lambda self: True)
    assert store.get(digest) == original and path.exists()


def test_pending_attachment_bytes_are_pinned_even_after_seven_days(store, archive):
    task = store.new_task("Retain an unsent attachment", channel="discord", address="main")
    store.status(task, "completed")
    delivery = Delivery(store)
    raw = b"pending exact attachment"
    digest = store.put(raw)
    delivery.queue_payload(
        task,
        "pending",
        {"content": "Evidence"},
        [
            {
                "filename": "evidence.txt",
                "hash": digest,
                "bytes": len(raw),
                "content_type": "text/plain",
            }
        ],
    )
    path = store.artifact_path(digest)
    old = time.time() - 8 * 86400
    os.utime(path, (old, old))
    store.db.execute("UPDATE artifact_access SET accessed_at=?", (old,))
    assert digest in archive.pins()
    assert archive.offload(path, time.time() - 7 * 86400) is None and path.read_bytes() == raw


async def test_owner_turn_frozen_across_retry_new_message_and_worker(store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    for role in ("astra", "sol", "luna"):
        store.config.data["profiles"][role]["provider"] = "openai"
    store.config.data["providers"]["openai"].update(stream=False, retry_delay_seconds=0)
    task = store.new_task("First question")
    first = turns.current(store, task)
    runtime = Runtime(store)
    child = await runtime.request(
        "astra",
        task,
        "worker",
        {"operation": "delegate", "role": "sol", "objective": "First question evidence"},
    )
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            store.item(
                "astra", {"role": "user", "content": "Follow-up question"}, task, origin="owner"
            )
            raise httpx.ConnectError("temporary fixture failure")
        return httpx.Response(
            200,
            json={
                "id": "r",
                "status": "completed",
                "output": [message()],
                "usage": {
                    "input_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 5},
                    "output_tokens": 10,
                },
            },
        )

    models = Models(store, httpx.MockTransport(handler))
    await models.infer("astra", task, "fixture", [{"role": "user", "content": "first"}], [])
    second = turns.current(store, task)
    assert first != second
    await models.infer(
        child["agent_id"], child["task_id"], "fixture", [{"role": "user", "content": "worker"}], []
    )
    items = store.rows("SELECT id,envelope_hash FROM items WHERE task_id=?", (task,))
    call, response = await models.infer(
        "astra",
        task,
        "fixture",
        [store.get(i["envelope_hash"]) for i in items],
        [],
        item_ids=[i["id"] for i in items],
    )
    runtime.ingest_response(task, call, response)
    store.status(child["task_id"], "completed")
    await runtime.finish(task)
    assert turns.usage(store, first) == {
        "calls": 3,
        "input": 40,
        "cached": 10,
        "output": 20,
        "unknown_calls": 1,
    }
    assert turns.usage(store, second)["calls"] == 1
    assert all(r["replied_at"] for r in store.rows("SELECT * FROM owner_turns"))
    assert len(store.rows("SELECT * FROM model_call_turns")) == 4


async def test_activity_preserves_raw_and_actual_phase_without_private_reasoning(
    store, monkeypatch
):
    store.config.data["discord"].update(enabled=True, activity_channel_id="activity")
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "test-only")
    delivery = Delivery(store)
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("Activity fixture", channel="discord", address="main")
    (store.config.root / "source.txt").write_text("Exact source evidence")
    response = {
        "output": [
            {"type": "reasoning", "encrypted_content": "private-test-value"},
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "file",
                "arguments": '{"path":"source.txt"}',
            },
            message("I am reading the source.", "commentary"),
        ]
    }
    call = saved(store, task, response["output"])
    runtime.ingest_response(task, call, response)
    await runtime.execute(task, store.one("SELECT * FROM tool_calls"))
    from bloom.activity import queue_records

    queue_records(delivery)
    queue_records(delivery)
    rows = store.rows("SELECT * FROM deliveries WHERE kind='activity'")
    assert len(rows) == 1
    payload = delivery.payload(rows[0])
    data = store.bytes(payload["files"][0]["hash"])
    assert b"private-test-value" not in data
    record = json.loads(data)
    assert record["native_output"][-1]["phase"] == "commentary"
    assert record["tools"][0]["original_result"]["text"] == "Exact source evidence"
    assert record["tools"][0]["model_facing_result"]["type"] == "function_call_output"
    assert rows[0]["address"] == "activity"


async def test_embed_attachment_uncertainty_and_ack_validation(store, monkeypatch):
    store.config.data["discord"]["enabled"] = True
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "test-only")
    attempts = []

    def handler(request):
        attempts.append(request)
        assert "multipart/form-data" in request.headers["content-type"]
        assert b"payload_json" in request.content and b"files[0]" in request.content
        raise httpx.ReadTimeout("uncertain send")

    delivery = Delivery(store, Discord(store, httpx.MockTransport(handler)))
    task = store.new_task("Deliver evidence", channel="discord", address="main")
    blob = b'{"exact":true}'
    file = {
        "filename": "evidence.json",
        "hash": store.put(blob),
        "bytes": len(blob),
        "content_type": "application/json",
    }
    ident = delivery.queue_payload(
        task,
        "fixture",
        {"embeds": [{"title": "Usage", "fields": [{"name": "Calls", "value": "2"}]}]},
        [file],
    )
    await delivery.flush()
    await delivery.flush()
    assert len(attempts) == 1
    row = store.one("SELECT * FROM deliveries WHERE id=?", (ident,))
    assert row["status"] == "uncertain"
    ack = {
        "id": "remote",
        "channel_id": "main",
        "nonce": row["nonce"],
        "author": {"id": delivery.discord.bot_id},
        "content": "",
        "embeds": [
            {
                "title": "Usage",
                "fields": [{"name": "Calls", "value": "2", "inline": False}],
                "type": "rich",
            }
        ],
        "attachments": [{"filename": "evidence.json", "size": 999}],
    }
    delivery.acknowledge(ack)
    assert store.one("SELECT status FROM deliveries")["status"] == "uncertain"
    ack["attachments"][0]["size"] = len(blob)
    delivery.acknowledge(ack)
    assert store.one("SELECT status FROM deliveries")["status"] == "sent"


async def test_final_phase_with_malformed_tool_requires_followup_and_recovery(store):
    runtime = Runtime(store)
    task = store.new_task("Assess tool failures")
    output = [
        {
            "type": "function_call",
            "call_id": "bad",
            "name": "read_file",
            "arguments": "invalid JSON",
        },
        message("I will inspect the source."),
    ]
    call = saved(store, task, output)
    runtime.ingest_response(task, call, {"output": output})

    async def answer(*args, **kwargs):
        assert store.one("SELECT is_error FROM tool_results")["is_error"] == 1
        assert store.one("SELECT status FROM tasks")["status"] != "completed"
        final = [message("The read failed before execution.")]
        ident = saved(store, task, final, kwargs["item_ids"])
        return ident, {"output": final}

    runtime.models.infer = answer
    runtime.recover()
    await runtime.step(task)
    assert store.one("SELECT status FROM tasks")["status"] == "completed"
    assert store.one("SELECT turns FROM tasks")["turns"] == 1
    assert store.get(
        store.one("SELECT detail_hash FROM events WHERE kind='model.phase_policy'")["detail_hash"]
    )["requested_shape"]


def test_paragraphs_precede_single_newline_splits():
    text = "First paragraph.\n\n" + "long line\n" * 200
    parts = chunks(text)
    assert parts[0] == "First paragraph.\n\n"
    assert "".join(parts) == text and all(len(p) <= 1900 for p in parts)


def test_embed_ack_accepts_discord_timestamp_normalization(store):
    task = store.new_task("Normalize the provider timestamp", channel="discord", address="main")
    delivery = Delivery(store)
    ident = delivery.queue_payload(
        task,
        "timestamp",
        {"embeds": [{"timestamp": "2026-10-14T10:35:40.345777+00:00"}]},
        notify_owner=True,
    )
    row = store.one("SELECT * FROM deliveries WHERE id=?", (ident,))
    assert delivery.matches(
        row,
        {
            "content": "<@" + delivery.discord.settings["owner_id"] + ">",
            "embeds": [{"timestamp": "2026-10-14T10:35:40.345Z"}],
        },
    )
    assert not delivery.matches(
        row, {"content": "", "embeds": [{"timestamp": "2026-10-15T10:35:40.345Z"}]}
    )
