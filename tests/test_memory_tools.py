import asyncio
import json

import pytest

from bloom.runtime import Runtime
from bloom.storage import Archive
from test_runtime_delivery import saved


async def invoke(runtime, store, task, name, arguments, identifier):
    await runtime.context.compose("astra", task, runtime.tools.schemas("astra"))
    output = [
        {
            "type": "function_call",
            "call_id": identifier,
            "name": name,
            "arguments": json.dumps(arguments),
        }
    ]
    call = saved(store, task, output, runtime.context.members["astra"])
    runtime.ingest_response(task, call, {"output": output})
    tool = store.one(
        "SELECT c.* FROM tool_calls c JOIN items i ON i.id=c.item_id WHERE i.model_call_id=?",
        (call,),
    )
    await runtime.execute(task, tool)
    return store.one("SELECT * FROM operations WHERE id=?", (tool["operation_id"],))


def update(**changes):
    return dict(
        topic="global",
        expected_revision=0,
        summary="Use direct, candid replies.",
        detail="Preserve the owner preference with its source.",
        rationale="Explicit owner preference.",
        sources=["owner"],
        **changes,
    )


async def test_typed_memory_is_versioned_sourced_and_extends_frozen_history(store):
    runtime = Runtime(store)
    task = store.new_task("Remember to give direct, candid replies.")
    store.status(task, "running")
    fixed, before = await runtime.context.compose("astra", task, runtime.tools.schemas("astra"))
    snapshot = store.one("SELECT * FROM memory_packets")
    owner = runtime.recall.owner(task)
    original = store.one("SELECT * FROM items WHERE id=?", (owner["id"],))
    missing = await invoke(runtime, store, task, "memory_get", {"topic": "global"}, "get")
    assert store.get(missing["result_hash"])["revision"] == 0
    result = await invoke(runtime, store, task, "memory_update", update(), "save")
    assert result["status"] == "completed"
    accepted = store.get(result["result_hash"])
    assert accepted["revision"] == 1 and accepted["sources"] == ["item:" + str(owner["id"])]
    assert runtime.context.memory_get("global")["detail"] == update()["detail"]
    assert store.one("SELECT * FROM items WHERE id=?", (owner["id"],)) == original
    assert update()["summary"] in (store.config.root / "memories/global.md").read_text()
    fixed_after, after = await runtime.context.compose(
        "astra", task, runtime.tools.schemas("astra")
    )
    assert fixed_after == fixed and after[: len(before)] == before
    assert store.one("SELECT * FROM memory_packets") == snapshot
    assert after[-1]["type"] == "function_call_output" and "revision" in after[-1]["output"]
    source_hash = store.one("SELECT source_hash FROM memory_revisions")["source_hash"]
    assert source_hash in Archive(store).pins()


async def test_conflicts_and_out_of_scope_sources_preserve_accepted_memory(store):
    runtime = Runtime(store)
    task = store.new_task("Remember my preference.")
    store.status(task, "running")
    first = await invoke(runtime, store, task, "memory_update", update(), "save")
    assert first["status"] == "completed"
    conflicting = update()
    conflicting["summary"] = "A stale overwrite"
    conflict = await invoke(runtime, store, task, "memory_update", conflicting, "stale")
    assert conflict["status"] == "failed" and "Stale memory revision" in json.dumps(
        store.get(conflict["result_hash"])
    )
    other = store.new_task("A queued request which Astra has not received")
    private = runtime.recall.owner(other)
    for index, source in enumerate(
        ("item:" + str(private["id"]), "artifact:" + store.put("unobserved"), "item:999999")
    ):
        invalid = update()
        invalid.update(expected_revision=1, sources=[source])
        result = await invoke(
            runtime, store, task, "memory_update", invalid, "invalid-" + str(index)
        )
        assert result["status"] == "failed"
    assert runtime.context.memory_get("global")["summary"] == update()["summary"]
    assert store.one("SELECT count(*) n FROM memory_revisions")["n"] == 1
    with pytest.raises(PermissionError, match="Astra owns"):
        await runtime.request("luna", task, "forbidden", {"operation": "memory_update", **update()})


@pytest.mark.parametrize("after_commit", [False, True])
async def test_memory_restart_never_double_applies_or_publishes_uncommitted_mirror(
    store, after_commit
):
    runtime = Runtime(store)
    task = store.new_task("Durable preference update")
    store.status(task, "running")
    if after_commit:

        def interrupt(*args, **kwargs):
            raise asyncio.CancelledError()

        runtime.memory_mirror = interrupt
    else:
        original = runtime.context.memory

        def interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise asyncio.CancelledError()

        runtime.context.memory = interrupt
    with pytest.raises(asyncio.CancelledError):
        await invoke(runtime, store, task, "memory_update", update(), "save")
    assert len(store.rows("SELECT * FROM memories")) == int(after_commit)
    assert not (store.config.root / "memories/global.md").exists()
    # Exercise recovery from process death, before any native result was acknowledged.
    store.db.execute("UPDATE operations SET status='running' WHERE name='memory_update'")
    resumed = Runtime(store)
    resumed.recover()
    tool = store.one("SELECT * FROM tool_calls WHERE task_id=?", (task,))
    await resumed.execute(task, tool)
    await resumed.execute(task, tool)
    assert resumed.context.memory_get("global")["revision"] == 1
    assert store.one("SELECT count(*) n FROM memory_revisions")["n"] == 1
    assert store.one("SELECT count(*) n FROM tool_results")["n"] == 1
    assert "Accepted revision 1" in (store.config.root / "memories/global.md").read_text()


async def test_owner_interruption_keeps_acknowledged_memory_receipt(store):
    runtime = Runtime(store)
    task = store.new_task("Remember this preference.")
    original = runtime.tools.run

    async def interrupt(*args, **kwargs):
        await original(*args, **kwargs)
        raise InterruptedError("Owner steering after the commit")

    runtime.tools.run = interrupt
    result = await invoke(runtime, store, task, "memory_update", update(), "save")
    assert result["status"] == "completed"
    assert store.get(result["result_hash"])["revision"] == 1
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] != "blocked"
