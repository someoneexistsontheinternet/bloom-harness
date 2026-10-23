import asyncio
import json
import time

import pytest

from bloom import reporting
from bloom.runtime import Runtime
from test_control_provenance import invoke
from test_evidence import pending_batch
from test_runtime_delivery import message, saved


async def test_empty_recall_is_durable_and_costs_no_luna_inference(store):
    store.config.data["memory"]["retrieval_enabled"] = True
    runtime = Runtime(store)
    store.new_task("An unrelated queued request is not visible history.")
    task = store.new_task("Search my earlier history.")
    args = {
        "plan": "Recall only if prior evidence exists.",
        "assignments": [
            {
                "path": "config/agent-presets/recall.toml",
                "bindings": {"question": "Find earlier decisions."},
            }
        ],
    }
    op = await invoke(runtime, store, task, "gather_evidence", args, "recall")
    assert op["status"] == "completed"
    report = store.get(op["result_hash"])["reports"][0]
    assert report["status"] == "not_needed" and report["sources"] == []
    assert "No Luna inference" in report["summary"]
    assert store.one("SELECT turns,status FROM tasks WHERE parent_id=?", (task,)) == {
        "turns": 0,
        "status": "completed",
    }
    assert not store.rows("SELECT * FROM model_calls WHERE agent_id!='astra'")
    resumed = Runtime(store)
    resumed.recover()
    again = await resumed.tools.run("astra", task, op["id"], "gather_evidence", args)
    assert (
        again == store.get(op["result_hash"])
        and len(store.rows("SELECT * FROM memory_retrievals")) == 1
    )
    assert not store.rows("SELECT * FROM item_origins WHERE kind='memory_retrieval'")


@pytest.mark.parametrize("prior", ["original", "accepted"])
async def test_lexical_miss_is_not_empty_history(store, prior):
    store.config.data["memory"]["retrieval_enabled"] = True
    runtime = Runtime(store)
    if prior == "original":
        old = store.new_task("Unrelated historical wording")
        store.status(old, "completed")
    else:
        runtime.context.memory("global", 0, "Earlier preference", "Detail", "fixture", [])
    task = store.new_task("Look for zzzz-no-matching-text.")
    child = runtime.recall.start(
        task, "recall", "zzzz-no-matching-text", "Look up the earlier answer."
    )
    assert store.one("SELECT status FROM tasks WHERE id=?", (child,))["status"] == "ready"
    assert not store.rows("SELECT * FROM events WHERE kind='recall.skipped_empty'")


async def test_report_completes_after_exploration_deadline_and_survives_restart(store):
    runtime, parent, operation = await pending_batch(store)
    child = runtime.evidence.children(operation["id"])[0]
    receipt = await invoke(runtime, store, child["id"], "read_file", {"path": "source.txt"}, "read")
    store.db.execute("UPDATE tasks SET turns=1 WHERE id=?", (child["id"],))
    store.db.execute(
        "UPDATE evidence_batches SET deadline=? WHERE operation_id=?",
        (time.time() + 0.1, operation["id"]),
    )
    runtime.evidence.refresh(child["id"])
    original_phase = reporting.phase(store, child["id"])
    resumed = Runtime(store)
    resumed.recover()
    resumed.evidence.refresh(child["id"])
    assert reporting.phase(store, child["id"]) == original_phase

    async def infer(agent, task, instructions, items, tools, **kwargs):
        assert tools == []
        await asyncio.sleep(0.15)
        await resumed.evidence.poll()
        assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "running"
        output = [
            message(
                json.dumps(
                    {
                        "status": "found",
                        "summary": "Verified source.",
                        "evidence": [
                            {"operation_id": receipt["id"], "quote": "left-verified-marker"}
                        ],
                    }
                )
            )
        ]
        return saved(store, task, output, kwargs["item_ids"]), {"output": output}

    resumed.models.infer = infer
    await resumed.step(child["id"])
    row = store.one("SELECT * FROM tasks WHERE id=?", (child["id"],))
    assert row["status"] == "completed" and resumed.evidence.report(row)["status"] == "found"


async def test_reporting_expiry_preserves_exact_partial_receipts(store):
    runtime, parent, operation = await pending_batch(store)
    child = runtime.evidence.children(operation["id"])[0]
    receipt = await invoke(runtime, store, child["id"], "read_file", {"path": "source.txt"}, "read")
    store.db.execute("UPDATE tasks SET turns=16 WHERE id=?", (child["id"],))
    runtime.evidence.refresh(child["id"])
    store.db.execute("UPDATE worker_reporting SET deadline=0 WHERE task_id=?", (child["id"],))
    await runtime.evidence.poll()
    row = store.one("SELECT * FROM tasks WHERE id=?", (child["id"],))
    assert row["status"] == "blocked"
    report = runtime.evidence.report(row)
    assert report["status"] == "partial" and report["evidence"][0]["id"] == receipt["id"]
    assert report["evidence"][0]["quote"] == "left-verified-marker"


async def test_report_can_correct_exact_source_once_without_renewing_budget(store):
    runtime, parent, operation = await pending_batch(store)
    child = runtime.evidence.children(operation["id"])[0]
    receipt = await invoke(runtime, store, child["id"], "read_file", {"path": "source.txt"}, "read")
    store.db.execute("UPDATE tasks SET turns=max_turns WHERE id=?", (child["id"],))
    count = 0

    async def infer(agent, task, instructions, items, tools, **kwargs):
        nonlocal count
        count += 1
        assert tools == []
        quote = "fabricated quote" if count == 1 else "left-verified-marker"
        output = [
            message(
                json.dumps(
                    {
                        "status": "found",
                        "summary": "Source result",
                        "evidence": [{"operation_id": receipt["id"], "quote": quote}],
                    }
                )
            )
        ]
        return saved(store, task, output, kwargs["item_ids"]), {"output": output}

    runtime.models.infer = infer
    await runtime.step(child["id"])
    phase = reporting.phase(store, child["id"])
    assert store.one("SELECT status FROM tasks WHERE id=?", (child["id"],))["status"] == "ready"
    await runtime.step(child["id"])
    assert count == 2 and reporting.phase(store, child["id"]) == phase
    assert store.one("SELECT status FROM tasks WHERE id=?", (child["id"],))["status"] == "completed"
