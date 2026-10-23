import json
import time

import pytest

from bloom.runtime import Runtime
from bloom.store import now


def expire(store, task):
    store.db.execute("UPDATE waits SET deadline=0 WHERE task_id=?", (task,))


def assert_still_waiting(store, task, items_before):
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "waiting"
    assert (
        store.one("SELECT deadline FROM waits WHERE task_id=?", (task,))["deadline"] > time.time()
    )
    assert store.rows("SELECT id FROM items WHERE task_id=?", (task,)) == items_before
    assert not store.rows("SELECT * FROM model_calls")


@pytest.mark.parametrize("terminal", ["completed", "blocked", "cancelled"])
async def test_worker_watchdog_never_wakes_parent_for_status_poll(store, terminal):
    runtime = Runtime(store)
    parent = store.new_task("Wait for the implementation")
    child = await runtime.request(
        "astra",
        parent,
        "spawn",
        {"operation": "delegate", "role": "sol", "objective": "Implement the change"},
    )
    store.status(child["task_id"], "running")
    await runtime.request(
        "astra", parent, "join", {"operation": "wait", "children": [child["task_id"]]}
    )
    before = store.rows("SELECT id FROM items WHERE task_id=?", (parent,))
    expire(store, parent)
    runtime.wake_waits()
    assert_still_waiting(store, parent, before)
    # This also survives a new coordinator instance using the durable wait.
    resumed = Runtime(store)
    expire(store, parent)
    resumed.wake_waits()
    assert_still_waiting(store, parent, before)
    store.status(child["task_id"], terminal)
    resumed.wake_waits()
    assert store.one("SELECT status FROM tasks WHERE id=?", (parent,))["status"] == "ready"
    assert not store.rows("SELECT * FROM waits WHERE task_id=?", (parent,))
    notice = store.one("SELECT text FROM items WHERE task_id=? ORDER BY id DESC", (parent,))["text"]
    assert json.loads(notice.removeprefix("Wait settled: "))["guard_fired"] is False


async def test_process_watchdog_waits_for_terminal_receipt(store):
    runtime = Runtime(store)
    task = store.new_task("Wait for the command result")
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
        ("launch", task, "tool", "start_process", "completed", store.put({}), now()),
    )
    store.db.execute(
        "INSERT INTO processes(id,operation_id,agent_id,target,cwd,command_hash,status,directory,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "process",
            "launch",
            "astra",
            "local",
            str(store.config.root),
            store.put("fixture"),
            "running",
            str(store.config.workspace),
            now(),
        ),
    )
    await runtime.request("astra", task, "join", {"operation": "wait", "processes": ["process"]})
    before = store.rows("SELECT id FROM items WHERE task_id=?", (task,))
    expire(store, task)
    runtime.wake_waits()
    assert_still_waiting(store, task, before)
    store.db.execute("UPDATE processes SET status='completed',exit_code=0 WHERE id='process'")
    runtime.wake_waits()
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "ready"


@pytest.mark.parametrize("condition", ["owner_messages", "after_seconds"])
async def test_explicit_conditions_do_not_wake_early_on_shorter_guard(store, condition):
    runtime = Runtime(store)
    task = store.new_task("Wait for the requested event")
    await runtime.request(
        "astra",
        task,
        "join",
        {"operation": "wait", condition: 1 if condition == "owner_messages" else 3600},
    )
    before = store.rows("SELECT id FROM items WHERE task_id=?", (task,))
    expire(store, task)
    runtime.wake_waits()
    assert_still_waiting(store, task, before)
    if condition == "owner_messages":
        store.event("owner.message", {"source": "fixture"}, task, "astra")
    else:
        # Keep the elapsed-time condition declared after reaching its time.
        store.db.execute("UPDATE wait_conditions SET not_before=1 WHERE task_id=?", (task,))
    runtime.wake_waits()
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "ready"


async def test_timer_only_wait_and_owner_steering_remain_interruptible(store):
    runtime = Runtime(store)
    task = store.new_task("Explicit timer")
    await runtime.request("astra", task, "timer", {"operation": "wait", "seconds": 1})
    expire(store, task)
    runtime.wake_waits()
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "ready"
    await runtime.request("astra", task, "owner", {"operation": "wait", "owner_messages": 1})
    runtime.steer(task, "Resume now with the correction")
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "ready"
