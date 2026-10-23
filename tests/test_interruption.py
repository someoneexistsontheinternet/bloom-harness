import asyncio
import json

from bloom.runtime import Runtime
from bloom.store import now
from test_runtime_delivery import saved, message


async def test_pause_closes_unexecuted_tool_protocol_without_effect(store):
    task = store.new_task("pause before write")
    runtime = Runtime(store)
    output = [
        {
            "type": "function_call",
            "call_id": "write1",
            "name": "write_file",
            "arguments": json.dumps(
                {"path": "must-not-exist", "text": "x", "expected_sha256": "absent"}
            ),
        }
    ]
    call = saved(store, task, output)
    runtime.ingest_response(task, call, {"output": output})
    runtime.pause(task)
    assert not (store.config.root / "must-not-exist").exists()
    assert store.one("SELECT status FROM operations")["status"] == "cancelled"
    assert store.one("SELECT * FROM tool_results")["is_error"] == 1
    assert (
        "not_executed"
        in store.one("SELECT text FROM items WHERE kind='function_call_output'")["text"]
    )


async def test_pause_retains_generation_that_finishes_in_flight(store):
    task = store.new_task("pause inference")
    runtime = Runtime(store)
    started, release = asyncio.Event(), asyncio.Event()

    async def infer(*args, **kwargs):
        started.set()
        await release.wait()
        output = [message("Saved while paused")]
        call = saved(store, task, output, kwargs["item_ids"])
        return call, {"output": output}

    runtime.models.infer = infer
    job = asyncio.create_task(runtime.step(task))
    await started.wait()
    runtime.pause(task)
    release.set()
    await job
    assert store.one("SELECT status FROM tasks")["status"] == "paused"
    assert not store.rows("SELECT * FROM deliveries")
    runtime.resume(task)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Saved final must be reused")

    runtime.models.infer = forbidden
    await runtime.step(task)
    assert store.one("SELECT status FROM tasks")["status"] == "completed"
    assert store.one("SELECT body FROM deliveries")["body"] == "Saved while paused"


async def test_steering_deadline_cancels_owned_inference_and_keeps_children(store):
    task = store.new_task("steer")
    runtime = Runtime(store)
    store.config.data["runtime"]["mention_seconds"] = 0.02
    child = await runtime.request(
        "astra",
        task,
        "child",
        {"operation": "delegate", "role": "sol", "objective": "Keep working"},
    )
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def infer(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    runtime.models.infer = infer
    job = asyncio.create_task(runtime.step(task))
    await started.wait()
    runtime.steer(task, "Change direction")
    await asyncio.wait_for(job, 2)
    assert cancelled.is_set()
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "ready"
    assert (
        store.one("SELECT status FROM tasks WHERE id=?", (child["task_id"],))["status"] == "ready"
    )


def test_restart_uncertainty_is_a_protocol_result_not_a_false_failure(store):
    task = store.new_task("unknown write")
    runtime = Runtime(store)
    output = [
        {"type": "function_call", "call_id": "write1", "name": "write_file", "arguments": "{}"}
    ]
    call = saved(store, task, output)
    runtime.ingest_response(task, call, {"output": output})
    tool = store.one("SELECT * FROM tool_calls")
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
        (
            tool["operation_id"],
            task,
            "tool",
            "write_file",
            "running",
            tool["arguments_hash"],
            now(),
        ),
    )
    runtime.recover()
    assert store.one("SELECT status FROM operations")["status"] == "uncertain"
    result = json.loads(
        store.one("SELECT text FROM items WHERE kind='function_call_output'")["text"]
    )
    assert result["status"] == "uncertain"
    assert "not_executed" not in result


async def test_steering_at_generation_boundary_prevents_old_write(store):
    task = store.new_task("old intent")
    runtime = Runtime(store)

    async def infer(*args, **kwargs):
        runtime.steer(task, "Do not make the old change")
        output = [
            {
                "type": "function_call",
                "call_id": "old-write",
                "name": "write_file",
                "arguments": json.dumps(
                    {"path": "obsolete", "text": "bad", "expected_sha256": "absent"}
                ),
            }
        ]
        return saved(store, task, output, kwargs["item_ids"]), {"output": output}

    runtime.models.infer = infer
    await runtime.step(task)
    assert not (store.config.root / "obsolete").exists()
    assert store.one("SELECT status FROM operations")["status"] == "cancelled"
    assert store.one("SELECT status FROM tasks")["status"] == "ready"
