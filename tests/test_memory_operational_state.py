import json

import pytest

from bloom.memory import Memory
from bloom.runtime import Runtime
from bloom.store import now


async def test_finished_helpers_are_exact_references_but_live_obligations_and_effects_stay_full(
    store,
):
    runtime = Runtime(store)
    old = store.new_task("Finished assignment")
    worker = await runtime.request(
        "astra",
        old,
        "old-worker",
        {
            "operation": "delegate",
            "role": "sol",
            "objective": "Historical exact assignment. " * 10000,
        },
    )
    store.status(worker["task_id"], "blocked", "Historical helper failure")
    store.status(old, "completed")
    continuing = await runtime.request(
        "astra",
        old,
        "continuing-worker",
        {
            "operation": "delegate",
            "role": "sol",
            "objective": "Actually still running under a terminal root",
        },
    )
    store.status(continuing["task_id"], "running")
    current = store.new_task("Current unfinished assignment")
    current_worker = await runtime.request(
        "astra",
        current,
        "current-worker",
        {
            "operation": "delegate",
            "role": "sol",
            "objective": "Keep this exact unfinished obligation in full.",
        },
    )
    store.status(current_worker["task_id"], "blocked", "Requires a real owner decision")
    store.status(current, "running")
    # Uncertain effects and continuing processes outlive a terminal parent.
    digest = store.put({"fixture": True})
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
        ("old-effect", worker["task_id"], "process", "start_process", "uncertain", digest, now()),
    )
    store.db.execute(
        "INSERT INTO processes(id,operation_id,agent_id,target,cwd,command_hash,status,directory,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "still-running",
            "old-effect",
            worker["agent_id"],
            "local",
            str(store.config.root),
            digest,
            "running",
            str(store.config.root),
            now(),
        ),
    )
    original = store.rows("SELECT * FROM tasks ORDER BY id")
    memory = Memory(store)
    state = memory.operational("astra", current)
    assert {r["id"] for r in state["children"]} == {
        current_worker["task_id"],
        continuing["task_id"],
    }
    assert (
        next(r for r in state["children"] if r["id"] == current_worker["task_id"])["objective"]
        == "Keep this exact unfinished obligation in full."
    )
    assert state["historical_children"] == [
        {
            "id": worker["task_id"],
            "parent_id": old,
            "status": "blocked",
            "root_id": old,
            "root_status": "completed",
        }
    ]
    assert state["unsettled_operations"][0]["id"] == "old-effect"
    assert state["processes"][0]["id"] == "still-running"
    assert store.rows("SELECT * FROM tasks ORDER BY id") == original
    item = memory.packet("astra", current, "Current unfinished assignment", 100000, retrieve=False)
    packet = json.loads(store.one("SELECT text FROM items WHERE id=?", (item,))["text"])
    assert packet["state"]["operational"] == state
    # The original long assignment can still be opened exactly by its ID.
    reopened = await runtime.request(
        "astra", current, "read-old", {"operation": "task_status", "task": worker["task_id"]}
    )
    assert "Historical exact assignment. " * 10000 in json.dumps(reopened)


async def test_real_active_state_overflow_is_still_rejected_without_truncating_it(store):
    runtime = Runtime(store)
    task = store.new_task("Unfinished work must be kept")
    worker = await runtime.request(
        "astra",
        task,
        "worker",
        {
            "operation": "delegate",
            "role": "sol",
            "objective": "Active obligation with exact requirements. " * 10000,
        },
    )
    store.status(worker["task_id"], "blocked", "Waiting on input")
    before = store.rows("SELECT * FROM tasks ORDER BY id")
    with pytest.raises(RuntimeError, match="Active memory state exceeds"):
        Memory(store).packet("astra", task, "unfinished", 100000, retrieve=False)
    assert store.rows("SELECT * FROM tasks ORDER BY id") == before
    assert not store.rows("SELECT * FROM memory_packets")


@pytest.mark.parametrize("status", ["prepared", "sent", "uncertain", "failed"])
async def test_recovery_failure_retains_one_attention_identity_and_latest_error(store, status):
    runtime = Runtime(store)
    task = store.new_task("Recover this exact task")
    runtime.block(task, "First error")
    original = store.one("SELECT * FROM deliveries")
    store.db.execute("UPDATE deliveries SET status=? WHERE id=?", (status, original["id"]))
    original = store.one("SELECT * FROM deliveries")
    runtime.resume(task)
    runtime.block(task, "Different error during the same recovery")
    runtime.recover()
    runtime.notify_parent(task)
    assert store.rows("SELECT * FROM deliveries") == [original]
    assert (
        store.one("SELECT error FROM tasks WHERE id=?", (task,))["error"]
        == "Different error during the same recovery"
    )
    event = store.one("SELECT detail_hash FROM events WHERE kind='task.attention_retained'")
    assert (
        store.get(event["detail_hash"])["latest_error"]
        == "Different error during the same recovery"
    )
    other = store.new_task("A genuinely separate task")
    runtime.block(other, "Independent failure")
    assert len(store.rows("SELECT * FROM deliveries")) == 2


async def test_two_legacy_alerts_cannot_become_a_third_during_recovery(store):
    runtime = Runtime(store)
    task = store.new_task("Resume incident with two historical alerts")
    runtime.block(task, "First historical failure")
    runtime.delivery.queue(None, task, "Second historical failure", "attention:legacy-second")
    await runtime.delivery.flush()
    original = store.rows("SELECT * FROM deliveries ORDER BY rowid")
    assert len(original) == 2 and all(r["status"] == "sent" for r in original)
    runtime.resume(task)
    runtime.block(task, "A further recovery failure")
    await runtime.delivery.flush()
    assert store.rows("SELECT * FROM deliveries ORDER BY rowid") == original
