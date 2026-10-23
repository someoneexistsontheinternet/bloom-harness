from test_memory import approve_fixture
import json

import httpx

from bloom.context import Context
from bloom.models import Models
from bloom.runtime import Runtime
from bloom.store import Store
from test_memory import Curator
from test_runtime_delivery import message, saved


async def test_context_is_frozen_until_compaction_and_restart(store):
    task = store.new_task("Research the orchid project")
    store.status(task, "running")
    context = Context(store, Curator(store))
    context.memory("global", 0, "Original preference", "original details", "fixture", [])
    fixed, first = await context.compose("astra", task, [])
    original_packet = store.one("SELECT * FROM memory_packets")
    context.memory(
        "global", 1, "New preference takes effect at compaction", "new details", "fixture", []
    )
    store.item("astra", message("An observation that extends the existing history."), task)
    same_fixed, second = await context.compose("astra", task, [])
    assert fixed == same_fixed and second[: len(first)] == first
    assert "New preference" not in json.dumps(second)
    assert len(store.rows("SELECT * FROM memory_packets")) == 1
    assert store.one("SELECT * FROM memory_packets") == original_packet
    reopened = Store(store.config)
    try:
        after_restart = Context(reopened, Curator(reopened))
        assert await after_restart.compose("astra", task, []) == (same_fixed, second)
        await after_restart.compact("astra", task)
        _, compacted = await after_restart.compose("astra", task, [])
        assert "New preference takes effect" in json.dumps(compacted)
        assert len(after_restart.selected("astra", task)) == 1
        assert all(
            x.get("type") not in ("function_call", "function_call_output", "reasoning")
            for x in compacted
        )
    finally:
        reopened.close()


async def test_native_tools_extend_exact_prefix_and_queued_input_is_appended(store):
    task = store.new_task("First owner task")
    store.status(task, "running")
    queued = store.new_task("Queued private task")
    context = Context(store, Curator(store))
    runtime = Runtime(store)
    store.config.data["providers"]["fixture"] = {
        "kind": "responses",
        "auth": "none",
        "base_url": "http://localhost",
        "stream": False,
    }
    store.config.data["profiles"]["astra"]["provider"] = "fixture"
    sent = []

    def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"status": "completed", "output": [message("continuing", "commentary")]}
        )

    model = Models(store, httpx.MockTransport(transport))
    fixed, first = await context.compose("astra", task, runtime.tools.schemas("astra"))
    await model.infer(
        "astra",
        task,
        fixed,
        first,
        runtime.tools.schemas("astra"),
        item_ids=context.members["astra"],
    )
    output = [
        {
            "type": "function_call",
            "call_id": "prefix-read",
            "name": "read_file",
            "arguments": '{"path":"fixture.txt"}',
        }
    ]
    call = saved(store, task, output, item_ids=context.members["astra"])
    runtime.ingest_response(task, call, {"output": output})
    runtime.result_item(
        store.one("SELECT * FROM tool_calls WHERE task_id=?", (task,)),
        {"text": "orchid result"},
        False,
    )
    fixed2, second = await context.compose("astra", task, runtime.tools.schemas("astra"))
    await model.infer(
        "astra",
        task,
        fixed2,
        second,
        runtime.tools.schemas("astra"),
        item_ids=context.members["astra"],
    )
    assert sent[1]["instructions"] == sent[0]["instructions"]
    assert sent[1]["tools"] == sent[0]["tools"]
    assert sent[1]["input"][: len(sent[0]["input"])] == sent[0]["input"]
    assert [x.get("type") for x in second[len(first) :]] == [
        "function_call",
        "function_call_output",
    ]
    assert "Queued private" not in json.dumps(second)
    store.status(task, "completed")
    store.status(queued, "running")
    fixed3, third = await context.compose("astra", queued, runtime.tools.schemas("astra"))
    assert fixed3 == fixed2 and third[: len(second)] == second
    assert third[-1]["content"] == "Queued private task"


async def test_consolidation_alone_does_not_replace_active_input(store):
    task = store.new_task("Keep one continuous Astra.")
    store.status(task, "running")
    context = Context(store, Curator(store))
    first = await context.compose("astra", task, [])
    await approve_fixture(context.history, task=task)
    assert context.history.version("astra") == 1
    assert await context.compose("astra", task, []) == first
    assert len(store.rows("SELECT * FROM memory_packets")) == 1
