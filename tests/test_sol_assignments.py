import asyncio
import json
import time

import pytest

from bloom import reporting
from bloom.runtime import Runtime
from test_control_provenance import invoke
from test_runtime_delivery import message, saved


async def test_astra_sol_luna_research_finishes_with_two_astra_decisions(store):
    runtime = Runtime(store)
    task = store.new_task("Investigate the retained marker")
    source = store.config.root / "source.txt"
    source.write_text("verified marker")
    counts = {}

    async def infer(agent, current, instructions, items, tools, **kwargs):
        counts[agent] = counts.get(agent, 0) + 1
        role = store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"]
        if role == "astra" and counts[agent] == 1:
            output = [
                {
                    "type": "function_call",
                    "call_id": "assign",
                    "name": "delegate_task",
                    "arguments": json.dumps(
                        {
                            "objective": "Verify the retained marker",
                            "criteria": "Return exact source evidence",
                            "cwd": str(store.config.root),
                        }
                    ),
                }
            ]
        elif role == "astra":
            assert counts[agent] == 2
            assert any("verified marker" in str(i.get("output", "")) for i in items)
            assert not store.rows("SELECT * FROM item_origins WHERE kind='child_report'")
            output = [message("Accepted: verified marker.")]
        elif role == "sol" and counts[agent] == 1:
            assert "gather_evidence" in {t["name"] for t in tools}
            output = [
                {
                    "type": "function_call",
                    "call_id": "readers",
                    "name": "gather_evidence",
                    "arguments": json.dumps(
                        {
                            "plan": "Read the specified marker",
                            "assignments": [
                                {
                                    "path": "config/agent-presets/inspect-files.toml",
                                    "bindings": {
                                        "question": "Read source.txt exactly",
                                        "cwd": str(store.config.root),
                                        "sources": ["source.txt"],
                                    },
                                }
                            ],
                        }
                    ),
                }
            ]
        elif role == "sol":
            assert "verified marker" in str(items)
            output = [message("verified marker, read from source.txt; no missing evidence.")]
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
            op = store.one(
                "SELECT id FROM operations WHERE task_id=? AND name='read_file'", (current,)
            )
            output = [
                message(
                    json.dumps(
                        {
                            "status": "found",
                            "summary": "verified marker",
                            "evidence": [{"operation_id": op["id"], "quote": "verified marker"}],
                        }
                    )
                )
            ]
        return saved(store, current, output, kwargs["item_ids"]), {"output": output}

    runtime.models.infer = infer
    await asyncio.wait_for(runtime.run(until_idle=True), 12)
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert counts["astra"] == 2 and len(counts) == 3
    sol = store.one("SELECT * FROM agents WHERE role='sol'")
    assert store.one("SELECT parent_id FROM agents WHERE role='luna'")["parent_id"] == sol["id"]
    assert sol["mode"] == "full" and sol["network"] == 1


async def test_sol_assignment_restart_reuses_child_and_retained_report(store):
    runtime = Runtime(store)
    task = store.new_task("Delegate once")
    op = await invoke(
        runtime,
        store,
        task,
        "delegate_task",
        {"objective": "Inspect", "criteria": "Report exact facts"},
        "delegation",
    )
    child = store.one("SELECT * FROM tasks WHERE parent_id=?", (task,))
    assert (
        child and store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "waiting"
    )
    resumed = Runtime(store)
    resumed.recover()

    async def forbidden(*a, **k):
        raise AssertionError("Resuming tool must not invoke Astra")

    resumed.models.infer = forbidden
    await resumed.step(task)
    assert len(store.rows("SELECT * FROM tasks WHERE parent_id=?", (task,))) == 1
    item = store.item(child["agent_id"], message("Retained concise report"), child["id"])
    store.status(child["id"], "completed")
    store.db.execute("UPDATE tasks SET final_item=? WHERE id=?", (item, child["id"]))
    resumed.notify_parent(child["id"])
    resumed.workers.poll()
    await resumed.step(task)
    result = store.get(
        store.one("SELECT result_hash FROM operations WHERE id=?", (op["id"],))["result_hash"]
    )
    assert result["summary"] == "Retained concise report"
    assert not store.rows("SELECT * FROM item_origins WHERE kind='child_report'")


async def test_expired_exploration_enters_reporting_without_losing_the_completed_inference(store):
    from test_evidence import pending_batch

    runtime, parent, operation = await pending_batch(store)
    child = runtime.evidence.children(operation["id"])[0]
    store.db.execute(
        "UPDATE evidence_batches SET deadline=? WHERE operation_id=?",
        (time.time() + 0.03, operation["id"]),
    )
    store.db.execute("UPDATE tasks SET turns=0 WHERE id=?", (child["id"],))

    async def work():
        await asyncio.sleep(0.05)
        store.db.execute("UPDATE tasks SET turns=1 WHERE id=?", (child["id"],))

    await runtime.evidence.step(child["id"], work())
    assert store.one("SELECT status FROM tasks WHERE id=?", (child["id"],))["status"] != "blocked"
    phase = reporting.phase(store, child["id"])
    assert phase and phase["deadline"] > time.time()
    assert runtime.tools.schemas(child["agent_id"]) == []


async def test_rollback_cannot_forget_accepted_sol_assignments(store, monkeypatch):
    from bloom import service

    runtime = Runtime(store)
    task = store.new_task("Retain accepted assignment")
    await invoke(
        runtime,
        store,
        task,
        "delegate_task",
        {"objective": "Inspect", "criteria": "Report"},
        "delegation",
    )
    root = store.config.root
    source = root / "artifacts/releases/incompatible"
    source.mkdir(parents=True)
    (source / "old.txt").write_text("No typed assignment reader")
    plan = {"root": str(root), "source": str(source), "manifest": service.source_manifest(source)}

    def forbidden(*args, **kwargs):
        raise AssertionError("Compatibility must be checked before stopping the service")

    monkeypatch.setattr(service, "unload", forbidden)
    with pytest.raises(ValueError, match="task_assignments"):
        service.launch(root, plan)
