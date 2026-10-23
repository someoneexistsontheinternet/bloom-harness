import asyncio
import json
import time

import pytest

from bloom.runtime import Runtime
from bloom.store import now
from test_runtime_delivery import message, saved
from test_control_provenance import invoke


async def test_final_at_turn_limit_is_delivered_without_another_model_call(store):
    task = store.new_task("Last allowed turn")
    saved(store, task, [message("Finished on the last turn")])
    store.db.execute("UPDATE tasks SET turns=max_turns WHERE id=?", (task,))
    runtime = Runtime(store)

    async def forbidden(*args, **kwargs):
        raise AssertionError("No additional inference is authorized")

    runtime.models.infer = forbidden
    await runtime.step(task)
    assert store.one("SELECT status FROM tasks")["status"] == "completed"
    assert store.one("SELECT body FROM deliveries")["body"] == "Finished on the last turn"


async def test_partial_process_output_cannot_implicitly_finish_and_recovery_reuses_handle(store):
    runtime = Runtime(store)
    task = store.new_task("Read the completed command output")
    directory = store.config.workspace / "processes/fixture"
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps({"exit_code": 0, "finished_at": time.time()}))
    (directory / "output.log").write_text("Verified archive location: /example/cold/bloom-v2.zip")
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
        ("started-command", task, "tool", "start_process", "completed", store.put({}), now()),
    )
    store.db.execute(
        "INSERT INTO processes(id,operation_id,agent_id,target,cwd,command_hash,status,directory,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "existing-handle",
            "started-command",
            "astra",
            "local",
            str(store.config.root),
            store.put("fixture"),
            "running",
            str(directory),
            now(),
        ),
    )
    saved(store, task, [message("I am following that pointer now.")])

    async def forbidden(*args, **kwargs):
        raise AssertionError("Completion check must not buy a model call")

    runtime.models.infer = forbidden
    await runtime.step(task)
    assert store.one("SELECT status FROM tasks")["status"] == "ready"
    updates = store.rows("SELECT body FROM deliveries WHERE kind='progress'")
    assert len(updates) == 1 and "I am following that pointer now." in updates[0]["body"]
    assert "still in progress" in updates[0]["body"]
    assert store.one("SELECT replied_at FROM owner_turns")["replied_at"] is None
    notices = store.rows(
        "SELECT i.text FROM items i JOIN item_origins o ON o.item_id=i.id WHERE o.kind='runtime_notice'"
    )
    assert len(notices) == 1 and "existing-handle" in notices[0]["text"]
    runtime.recover()
    await runtime.finish(task)
    assert len(store.rows("SELECT * FROM deliveries WHERE kind='progress'")) == 1
    assert len(store.rows("SELECT * FROM item_origins WHERE kind='runtime_notice'")) == 1
    # A native observation reads the saved result instead of restarting anything.
    await invoke(
        runtime,
        store,
        task,
        "observe_process",
        {"process_id": "existing-handle"},
        "observe-existing",
    )
    assert store.one("SELECT status FROM processes")["status"] == "completed"
    saved(store, task, [message("Located and verified /example/cold/bloom-v2.zip")])
    await runtime.step(task)
    assert store.one("SELECT status FROM tasks")["status"] == "completed"
    assert (
        store.one("SELECT body FROM deliveries WHERE kind='message' ORDER BY rowid DESC")["body"]
        == "Located and verified /example/cold/bloom-v2.zip"
    )
    assert len(store.rows("SELECT * FROM processes")) == 1


@pytest.mark.parametrize("status", ["completed", "blocked", "cancelled"])
async def test_explicit_process_handoff_or_stop_preserves_outcome(store, status):
    runtime = Runtime(store)
    task = store.new_task("Keep the requested service running")
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
        ("service-start", task, "tool", "start_process", "completed", store.put({}), now()),
    )
    store.db.execute(
        "INSERT INTO processes(id,operation_id,agent_id,target,cwd,command_hash,status,directory,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "service",
            "service-start",
            "astra",
            "local",
            ".",
            store.put("fixture"),
            "running",
            ".",
            now(),
        ),
    )
    await runtime.request(
        "astra",
        task,
        "handoff",
        {
            "operation": "outcome",
            "status": status,
            "reason": "Verified startup; service intentionally continues with its saved handle",
        },
    )
    saved(
        store,
        task,
        [
            message(
                "Service handle: service. Use observe_process to monitor and input_process cancel to stop."
            )
        ],
    )
    await runtime.step(task)
    assert store.one("SELECT status FROM tasks")["status"] == status
    assert store.one("SELECT status FROM processes")["status"] == "running"
    assert len(store.rows("SELECT * FROM deliveries")) == 1


async def test_worker_failure_and_later_completion_both_reach_parent_once(store):
    runtime = Runtime(store)
    parent = store.new_task("Accept the worker evidence")
    child = await runtime.request(
        "astra",
        parent,
        "spawn",
        {"operation": "delegate", "role": "sol", "objective": "Do the scoped work"},
    )
    runtime.block(child["task_id"], "Missing configured resource")
    runtime.notify_parent(child["task_id"])
    reports = store.rows(
        "SELECT i.* FROM items i JOIN item_origins o ON o.item_id=i.id WHERE o.kind='child_report'"
    )
    assert len(reports) == 1 and "Missing configured resource" in reports[0]["text"]
    runtime.resume(child["task_id"])
    saved(store, child["task_id"], [message("Resource restored; evidence checked")])
    await runtime.step(child["task_id"])
    reports = store.rows(
        "SELECT i.text FROM items i JOIN item_origins o ON o.item_id=i.id WHERE o.kind='child_report' ORDER BY i.id"
    )
    assert len(reports) == 2 and "Resource restored" in reports[-1]["text"]
    assert store.one("SELECT status FROM tasks WHERE id=?", (parent,))["status"] == "ready"


async def test_wait_requires_every_declared_condition_including_coalesced_owner_messages(store):
    runtime = Runtime(store)
    parent = store.new_task("Wait for evidence")
    child = await runtime.request(
        "astra",
        parent,
        "spawn",
        {"operation": "delegate", "role": "sol", "objective": "Produce evidence"},
    )
    store.db.execute(
        "INSERT INTO processes(id,agent_id,target,cwd,command_hash,status,directory,created_at) VALUES('p','astra','local','.',?,'running','.',?)",
        (store.put("fixture"), now()),
    )
    await runtime.request(
        "astra",
        parent,
        "wait",
        {
            "operation": "wait",
            "children": [child["task_id"]],
            "processes": ["p"],
            "owner_messages": 2,
            "after_seconds": 60,
        },
    )
    store.status(child["task_id"], "completed")
    store.db.execute("UPDATE processes SET status='completed',exit_code=0 WHERE id='p'")
    settings = store.config.data["discord"]
    for number in (1, 2):
        runtime.receive_discord(
            {
                "id": str(number),
                "channel_id": settings["channel_id"],
                "author": {"id": settings["owner_id"]},
                "content": "Queued message " + str(number),
            }
        )
    assert len(store.rows("SELECT id FROM tasks WHERE channel='discord'")) == 1  # Burst coalesced.
    runtime.wake_waits()
    assert store.one("SELECT status FROM tasks WHERE id=?", (parent,))["status"] == "waiting"
    store.db.execute("UPDATE wait_conditions SET not_before=?", (time.time() - 1,))
    runtime.wake_waits()
    assert store.one("SELECT status FROM tasks WHERE id=?", (parent,))["status"] == "ready"
    assert (
        '"guard_fired": false'
        in store.one("SELECT text FROM items WHERE task_id=? ORDER BY id DESC", (parent,))["text"]
    )
    await runtime.request(
        "astra",
        parent,
        "timer",
        {"operation": "wait", "children": [], "processes": [], "seconds": 1},
    )
    runtime.wake_waits()
    assert store.one("SELECT status FROM tasks WHERE id=?", (parent,))["status"] == "waiting"
    store.db.execute("UPDATE waits SET deadline=0")
    runtime.wake_waits()
    assert (
        '"guard_fired": true'
        in store.one("SELECT text FROM items WHERE task_id=? ORDER BY id DESC", (parent,))["text"]
    )


async def test_waiting_astra_does_not_start_queued_task_until_wake(store):
    runtime = Runtime(store)
    first = store.new_task("Existing work")
    await runtime.request("astra", first, "wait", {"operation": "wait", "seconds": 60})
    queued = store.new_task("Ordinary queued message")
    selected = []

    async def step(task):
        selected.append(task)
        store.status(task, "completed")

    runtime.step = step
    runner = asyncio.create_task(runtime.run())
    await asyncio.sleep(0.25)
    assert selected == []
    runtime.steer(first, "Inspect the queue now")
    await asyncio.sleep(0.35)
    runtime.stopping = True
    await runner
    assert selected == [first, queued]


async def test_accepted_control_receipt_recovers_without_repeating_delegation(store):
    runtime = Runtime(store)
    task = store.new_task("Recover a committed delegation")
    operation = "accepted-delegate"
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
        (operation, task, "tool", "apply_request", "running", store.put({}), now()),
    )
    child = await runtime.request(
        "astra",
        task,
        operation,
        {"operation": "delegate", "role": "sol", "objective": "One assignment"},
    )
    runtime.recover()
    saved_operation = store.one("SELECT * FROM operations WHERE id=?", (operation,))
    assert saved_operation["status"] == "completed"
    assert store.get(saved_operation["result_hash"]) == child
    assert len(store.rows("SELECT * FROM agents")) == 2
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "ready"


async def test_invalid_delegation_does_not_launch_orphan_worker(store):
    runtime = Runtime(store)
    task = store.new_task("Validate before delegation")
    with pytest.raises(RuntimeError):
        await runtime.request(
            "astra",
            task,
            "invalid-repo",
            {
                "operation": "delegate",
                "role": "sol",
                "objective": "Do work",
                "repository": str(store.config.root / "missing-repo"),
            },
        )
    assert len(store.rows("SELECT * FROM agents")) == 1
    assert len(store.rows("SELECT * FROM tasks")) == 1


async def test_fresh_status_and_repeated_wait_are_not_stale_content_cache_hits(store):
    runtime = Runtime(store)
    task = store.new_task("Current state must be observable")
    first = await runtime.request("astra", task, "query-1", {"operation": "task_status"})
    store.status(task, "waiting")
    second = await runtime.request("astra", task, "query-2", {"operation": "task_status"})
    assert first["task"]["status"] == "ready" and second["task"]["status"] == "waiting"
    assert await runtime.request("astra", task, "query-1", {"operation": "task_status"}) == first
    with pytest.raises(ValueError, match="identity was reused"):
        await runtime.request(
            "astra", task, "query-1", {"operation": "plan", "text": "Different effect"}
        )
    await runtime.request("astra", task, "wait-1", {"operation": "wait", "seconds": 1})
    store.db.execute("UPDATE waits SET deadline=0")
    runtime.wake_waits()
    await runtime.request("astra", task, "wait-2", {"operation": "wait", "seconds": 1})
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "waiting"


async def test_resume_tree_preserves_earlier_blocked_and_separately_paused_workers(store):
    runtime = Runtime(store)
    root = store.new_task("Owner work")
    children = []
    for n in range(3):
        children.append(
            await runtime.request(
                "astra",
                root,
                str(n),
                {"operation": "delegate", "role": "sol", "objective": "Assignment " + str(n)},
            )
        )
    blocked, paused, active = (c["task_id"] for c in children)
    runtime.block(blocked, "Pre-existing independent blocker")
    runtime.pause(paused)
    runtime.pause(root)
    runtime.resume(root)
    assert store.one("SELECT status FROM tasks WHERE id=?", (root,))["status"] == "ready"
    assert store.one("SELECT status FROM tasks WHERE id=?", (active,))["status"] == "ready"
    assert store.one("SELECT status FROM tasks WHERE id=?", (blocked,))["status"] == "blocked"
    assert store.one("SELECT status FROM tasks WHERE id=?", (paused,))["status"] == "paused"
    runtime.resume(paused)
    assert store.one("SELECT status FROM tasks WHERE id=?", (paused,))["status"] == "ready"


def test_recovery_does_not_clear_explicit_pause_on_uncertain_effect(store):
    runtime = Runtime(store)
    task = store.new_task("Explicitly paused work")
    runtime.pause(task)
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
        ("effect", task, "tool", "write_file", "running", store.put({}), now()),
    )
    runtime.recover()
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "paused"
    runtime.resume(task)
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "blocked"
