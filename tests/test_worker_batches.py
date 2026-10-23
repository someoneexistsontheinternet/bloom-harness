import asyncio
import json

import pytest

from bloom.runtime import Runtime
from test_control_provenance import invoke
from test_runtime_delivery import message, saved


def assignments(store):
    result = []
    for name in ("left", "right"):
        directory = store.config.root / name
        directory.mkdir()
        (directory / "source.txt").write_text(name + "-marker")
        result.append(
            {
                "path": "config/agent-presets/inspect-files.toml",
                "bindings": {
                    "question": "Read the exact marker.",
                    "cwd": str(directory),
                    "sources": ["source.txt"],
                },
            }
        )
    return result


async def test_single_native_preset_batch_collects_reports_without_extra_astra_calls(store):
    runtime = Runtime(store)
    task = store.new_task("Compare the independent markers.")
    selected = assignments(store)
    counts = {}

    async def infer(agent, current, instructions, items, tools, **kwargs):
        counts[agent] = counts.get(agent, 0) + 1
        if agent == "astra" and counts[agent] == 1:
            output = [
                {
                    "type": "function_call",
                    "call_id": "batch",
                    "name": "gather_evidence",
                    "arguments": json.dumps(
                        {"plan": "Inspect both exact sources.", "assignments": selected}
                    ),
                }
            ]
        elif agent == "astra":
            assert counts[agent] == 2
            result = next(i for i in items if i.get("type") == "function_call_output")
            assert all(marker in result["output"] for marker in ("left-marker", "right-marker"))
            assert not store.rows("SELECT * FROM item_origins WHERE kind='worker_evidence'")
            output = [message("Both markers checked.")]
        elif counts[agent] == 1:
            output = [
                {
                    "type": "function_call",
                    "call_id": "read",
                    "name": "read_file",
                    "arguments": '{"path":"source.txt"}',
                }
            ]
        else:
            receipt = store.one(
                "SELECT id,result_hash FROM operations WHERE task_id=? AND name='read_file'",
                (current,),
            )
            output = [
                message(
                    json.dumps(
                        {
                            "status": "found",
                            "summary": "Verified the requested source.",
                            "evidence": [
                                {
                                    "operation_id": receipt["id"],
                                    "quote": store.get(receipt["result_hash"])["text"],
                                }
                            ],
                        }
                    )
                )
            ]
        return saved(store, current, output, kwargs["item_ids"]), {"output": output}

    runtime.models.infer = infer
    await asyncio.wait_for(runtime.run(until_idle=True), 8)
    assert counts["astra"] == 2 and len(counts) == 3
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert len(store.rows("SELECT * FROM worker_batches")) == 1
    assert len(store.rows("SELECT * FROM worker_launches")) == 2
    items = {r["report_item"] for r in store.rows("SELECT * FROM worker_launches")}
    assert len(items) == 1 and None not in items
    assert (
        store.one("SELECT kind FROM items WHERE id=?", (items.pop(),))["kind"]
        == "function_call_output"
    )


async def test_captured_batch_recovers_without_reread_or_partial_publication(store):
    runtime = Runtime(store)
    task = store.new_task("Durable batch")
    args = {"plan": "Read both sources.", "assignments": assignments(store)}
    original = runtime.workers.launch
    count = 0

    def interrupted(*a, **k):
        nonlocal count
        count += 1
        if count == 2:
            raise asyncio.CancelledError()
        return original(*a, **k)

    runtime.workers.launch = interrupted
    with pytest.raises(asyncio.CancelledError):
        await invoke(runtime, store, task, "gather_evidence", args, "batch")
    assert len(store.rows("SELECT * FROM preset_invocations")) == 2
    assert not store.rows("SELECT * FROM worker_launches")
    assert not store.rows("SELECT id FROM tasks WHERE parent_id=?", (task,))
    (store.config.source / "config/agent-presets/inspect-files.toml").write_text(
        "no longer valid TOML"
    )
    resumed = Runtime(store)
    resumed.recover()

    async def forbidden(*a, **k):
        raise AssertionError("Batch recovery must reuse the original native call")

    resumed.models.infer = forbidden
    await resumed.step(task)
    children = store.rows("SELECT id FROM tasks WHERE parent_id=?", (task,))
    assert len(children) == 2
    for child in children:
        resumed.block(child["id"], "Fixture source unavailable")
    resumed.workers.poll()
    await resumed.step(task)
    assert len(store.rows("SELECT * FROM worker_launches")) == 2
    assert len(store.rows("SELECT * FROM model_calls")) == 1
    root = store.one("SELECT operation_id FROM worker_batches")["operation_id"]
    assert store.one("SELECT status FROM operations WHERE id=?", (root,))["status"] == "completed"
    with pytest.raises(ValueError, match="identity changed"):
        resumed.workers.gather("astra", task, root, "Changed plan", args["assignments"])


async def test_invalid_later_preset_launches_nothing(store):
    runtime = Runtime(store)
    task = store.new_task("Validate whole batch first")
    selected = assignments(store)
    selected[1]["bindings"]["unsupported"] = "invalid"
    result = await invoke(
        runtime, store, task, "gather_evidence", {"plan": "Inspect", "assignments": selected}, "bad"
    )
    assert result["status"] == "failed"
    assert not store.rows("SELECT * FROM worker_batches")
    assert not store.rows("SELECT * FROM preset_invocations")
    assert not store.rows("SELECT id FROM tasks WHERE parent_id=?", (task,))


async def test_background_batch_returns_handles_and_retains_supplementary_policy(store):
    runtime = Runtime(store)
    task = store.new_task("Background supplemental evidence")
    selected = [a | {"required": False} for a in assignments(store)]
    result = await invoke(
        runtime,
        store,
        task,
        "gather_evidence",
        {"plan": "Independent optional inspection", "assignments": selected, "background": True},
        "background",
    )
    assert result["status"] == "completed"
    assert all(
        not runtime.workers.required(r["id"])
        for r in store.rows("SELECT id FROM tasks WHERE parent_id=?", (task,))
    )
    assert not store.rows("SELECT * FROM worker_awaits")
