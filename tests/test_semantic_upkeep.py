import datetime as dt
import json

import httpx
import pytest

from bloom.memory import MemoryInterrupted
from bloom.models import Models
from bloom.runtime import Runtime
from bloom.semantic_upkeep import publish, run
from test_memory_authority import proposal
from test_runtime_delivery import message


def setup(store, *, outcome="approve"):
    store.config.data["memory"]["semantic_upkeep_enabled"] = True
    store.config.data["providers"]["fixture"] = {
        "kind": "responses",
        "auth": "none",
        "base_url": "http://localhost",
        "stream": False,
        "max_retries": 0,
    }
    store.config.profile("astra")["provider"] = "fixture"
    task = store.new_task("My lasting preference is accurate answers.")
    store.status(task, "completed")
    store.db.execute(
        "UPDATE items SET created_at=?",
        ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat(),),
    )
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        assert body["model"] == store.config.profile("astra")["model"]
        if outcome == "transport":
            raise httpx.ReadError("fixture disconnected")
        if outcome == "http":
            return httpx.Response(503, json={"error": "fixture unavailable"})
        payload = json.loads(body["input"][0]["content"])
        value = proposal({"request": payload["request"]})
        if outcome == "invalid" and len(seen) == 1:
            value["updates"][0]["quote"] = "Invented supporting source"
        answer = {"approve": True, "batch": payload["batch"], "proposal": value}
        if outcome == "decline":
            answer = {
                "approve": False,
                "batch": payload["batch"],
                "reason": "Evidence insufficient",
            }
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [message(json.dumps(answer))],
                "usage": {"input_tokens": 200, "output_tokens": 70},
            },
        )

    runtime = Runtime(store, Models(store, httpx.MockTransport(respond)))
    return runtime, task, seen


async def test_idle_approval_is_real_astra_exact_idempotent_and_does_not_edit_frozen_context(store):
    runtime, task, seen = setup(store)
    _, before = await runtime.context.compose("astra", task, [])
    originals = store.rows("SELECT * FROM items ORDER BY id")
    result = await run(runtime, task)
    assert result["published"] == 1 and len(seen) == 1
    batch = store.one("SELECT * FROM memory_batches")
    call = store.one("SELECT * FROM model_calls WHERE purpose='memory_upkeep'")
    assert call["task_id"] is None and call["agent_id"] == "astra"
    approval = store.one("SELECT * FROM memory_approvals")
    assert approval["call_id"] == call["id"]
    assert store.one("SELECT status FROM operations")["status"] == "completed"
    assert store.one("SELECT revision FROM memory_facts")["revision"] == 1
    assert not runtime.context.history.pending("astra", task)
    _, after = await runtime.context.compose("astra", task, [])
    assert before == after and store.rows("SELECT * FROM items ORDER BY id") == originals
    decision = json.loads(store.get(call["response_hash"])["output"][0]["content"][0]["text"])
    publish(runtime.context.history, batch, call["id"], decision)
    assert store.one("SELECT count(*) n FROM memory_fact_revisions")["n"] == 1
    assert (await run(runtime, task))["published"] == 0 and len(seen) == 1


@pytest.mark.parametrize("outcome", ["transport", "http", "decline"])
async def test_optional_failed_or_declined_upkeep_preserves_history_without_repurchase(
    store, outcome
):
    runtime, task, seen = setup(store, outcome=outcome)
    original = store.rows("SELECT * FROM items")
    first = await run(runtime, task)
    second = await run(runtime, task)
    assert first["published"] == second["published"] == 0 and len(seen) == 1
    assert not store.rows("SELECT * FROM memory_facts")
    assert store.rows("SELECT * FROM items") == original
    assert store.one("SELECT status FROM tasks")["status"] == "completed"
    assert runtime.context.history.pending("astra", task)


async def test_validation_correction_keeps_original_sources_and_publishes_only_corrected_receipt(
    store,
):
    runtime, task, seen = setup(store, outcome="invalid")
    result = await run(runtime, task)
    assert result["published"] == 1 and len(seen) == 2
    calls = store.rows("SELECT * FROM model_calls ORDER BY rowid")
    assert calls[1]["id"].endswith("_correction")
    assert store.one("SELECT call_id FROM memory_approvals")["call_id"] == calls[1]["id"]
    assert seen[1]["input"][0] == seen[0]["input"][0]
    assert (
        store.one("SELECT quote FROM memory_facts")["quote"]
        == "My lasting preference is accurate answers."
    )


async def test_owner_preemption_never_publishes_completed_but_stale_approval(store):
    runtime, task, seen = setup(store)
    handler = runtime.models.transport.handler

    def preempt(request):
        response = handler(request)
        store.new_task("New work takes priority")
        return response

    runtime.models.transport = httpx.MockTransport(preempt)
    with pytest.raises(MemoryInterrupted):
        await run(runtime, task)
    assert len(seen) == 1 and not store.rows("SELECT * FROM memory_approvals")
    assert not store.rows("SELECT * FROM memory_facts")


@pytest.mark.parametrize("tamper", ["model", "source"])
async def test_upkeep_rejects_impersonated_astra_or_changed_review_payload(store, tamper):
    runtime, task, seen = setup(store)
    review = runtime.context.history.prepare("astra", task)
    batch = store.one("SELECT * FROM memory_batches")
    payload = {
        "batch": review["batch"],
        "source_hash": batch["request_hash"],
        "request": review["request"],
    }
    if tamper == "source":
        payload["request"] = {**review["request"], "version": 987}
    call, response = await runtime.models.infer(
        "astra",
        None,
        "fixture",
        [{"role": "user", "content": json.dumps(payload)}],
        [],
        purpose="memory_upkeep",
    )
    if tamper == "model":
        store.db.execute(
            "UPDATE model_calls SET model_requested=? WHERE id=?",
            (store.config.profile("luna")["model"], call),
        )
    with pytest.raises(PermissionError):
        publish(
            runtime.context.history,
            batch,
            call,
            json.loads(response["output"][0]["content"][0]["text"]),
        )
    assert not store.rows("SELECT * FROM memory_approvals")


async def test_idle_trigger_respects_threshold_and_retries_no_failed_maintenance(
    store, monkeypatch
):
    runtime, task, seen = setup(store, outcome="transport")
    count = runtime.context.count
    monkeypatch.setattr(
        runtime.context, "count", lambda x: 100001 if isinstance(x, list) and x else count(x)
    )

    # Simulate a successful independent working memo after optional upkeep fails.
    async def compact(*args, **kwargs):
        return "context-preserved"

    monkeypatch.setattr(runtime.context, "compact", compact)
    result = await runtime.memory_maintenance()
    assert result["context_id"] == "context-preserved" and not result["permanent_memory_changed"]
    assert len(seen) == 1 and not store.rows("SELECT * FROM memory_approvals")


async def test_upkeep_activity_includes_rejected_and_approved_calls_without_rebilling_owner(store):
    from bloom.activity import queue_records
    from bloom.delivery import Delivery

    runtime, task, seen = setup(store, outcome="invalid")
    store.config.data["discord"].update(enabled=True, activity_channel_id="activity")
    store.db.execute("UPDATE tasks SET channel='discord',address='main' WHERE id=?", (task,))
    delivery = Delivery(store)
    await run(runtime, task)
    queue_records(delivery)
    queue_records(delivery)
    rows = store.rows("SELECT * FROM deliveries WHERE kind='activity'")
    assert len(rows) == 2 and all(r["address"] == "activity" for r in rows)
    assert not store.rows("SELECT * FROM model_call_turns")
    assert all(r["task_id"] is None for r in store.rows("SELECT task_id FROM model_calls"))


def test_new_maintenance_feed_never_backfills_historical_compactions(store):
    from bloom.activity import queue_records
    from bloom.delivery import Delivery
    from bloom.store import now
    from test_runtime_delivery import saved

    task = store.new_task("Historical completed conversation", channel="discord", address="main")
    call = saved(store, task, [message("Historical memo")])
    store.db.execute("UPDATE model_calls SET task_id=NULL,purpose='compaction' WHERE id=?", (call,))
    store.db.execute(
        "INSERT INTO working_compactions(id,task_id,source_hash,call_id,status,created_at) VALUES(?,?,?,?,'completed',?)",
        ("historic", task, store.put({}), call, now()),
    )
    store.db.execute("INSERT INTO activity_feed VALUES(1,'activity',0)")
    store.config.data["discord"].update(enabled=True, activity_channel_id="activity")
    queue_records(Delivery(store))
    assert not store.rows("SELECT * FROM deliveries")


async def test_idle_backlog_compacts_once_after_multiple_semantic_passes(store, monkeypatch):
    runtime, task, seen = setup(store)
    second = store.new_task("Another historical task")
    store.status(second, "completed")
    store.db.execute(
        "UPDATE items SET created_at=?",
        ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat(),),
    )
    store.config.data["memory"]["max_batches_per_pass"] = 1
    original = runtime.models.transport.handler

    def timeline(request):
        response = original(request)
        body = json.loads(response.content)
        value = json.loads(body["output"][0]["content"][0]["text"])
        value["proposal"]["updates"] = []
        body["output"][0]["content"][0]["text"] = json.dumps(value)
        return httpx.Response(200, json=body)

    runtime.models.transport = httpx.MockTransport(timeline)
    count = runtime.context.count
    monkeypatch.setattr(
        runtime.context, "count", lambda x: 100001 if isinstance(x, list) and x else count(x)
    )
    compactions = []

    async def compact(*args, **kwargs):
        compactions.append(args)
        return "one-context"

    monkeypatch.setattr(runtime.context, "compact", compact)
    first = await runtime.memory_maintenance()
    assert first["skipped"] == "semantic_backlog_in_progress" and not compactions
    assert store.one("SELECT * FROM semantic_upkeep_due")
    second = await runtime.memory_maintenance()
    assert second["context_id"] == "one-context" and len(compactions) == 1 and len(seen) == 2
    assert not store.rows("SELECT * FROM semantic_upkeep_due")
