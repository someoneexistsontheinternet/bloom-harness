import asyncio
import json
import time

import jsonschema
import pytest

from bloom.progress import execution_arguments
from bloom.results import question_context
from bloom.runtime import Runtime
from bloom.store import now
from test_control_provenance import invoke
from test_runtime_delivery import message, saved


UPDATE = {
    "observed": "The source files are available; their contents are not checked yet.",
    "next": "Read both files together.",
    "why": "Compare the actual evidence before answering.",
}


def native(name, arguments, identity="one"):
    return {
        "type": "function_call",
        "name": name,
        "call_id": identity,
        "arguments": json.dumps(arguments),
    }


def save_strict(runtime, task, output, item_ids=None):
    call = saved(runtime.store, task, output, item_ids)
    request = {"tools": runtime.tools.schemas("astra")}
    runtime.store.db.execute(
        "UPDATE model_calls SET request_hash=? WHERE id=?", (runtime.store.put(request), call)
    )
    return call


def test_public_schemas_are_strict_but_execution_optionals_remain_optional(store):
    runtime = Runtime(store)
    schemas = {s["name"]: s for s in runtime.tools.schemas("astra")}

    def check(schema):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for child in schema["properties"].values():
                check(child)
        if "items" in schema:
            check(schema["items"])
        for child in schema.get("anyOf", []):
            check(child)

    for schema in schemas.values():
        assert schema["strict"]
        check(schema["parameters"])
        jsonschema.Draft202012Validator.check_schema(schema["parameters"])
    args = {
        "path": "one.txt",
        "start": None,
        "end": None,
        "detail": None,
        "target": None,
        "exact": None,
        "progress": UPDATE,
    }
    jsonschema.validate(args, schemas["read_file"]["parameters"])
    base = next(
        s for s in runtime.tools.schemas("astra", execution=True) if s["name"] == "read_file"
    )
    assert execution_arguments({"path": "one.txt", "start": None}, base["parameters"]) == {
        "path": "one.txt"
    }
    assert execution_arguments({"path": None}, base["parameters"]) == {"path": None}


async def test_progress_is_visible_once_preserves_native_phases_and_feeds_curators(store):
    runtime = Runtime(store)
    task = store.new_task("Compare two sources")
    for name in ("one.txt", "two.txt"):
        (store.config.root / name).write_text(name)
    output = [
        {"type": "reasoning", "encrypted_content": "private-fixture"},
        native("read_file", {"path": "one.txt", "start": None, "progress": UPDATE}),
        native("read_file", {"path": "two.txt", "progress": UPDATE}, "two"),
    ]
    call = save_strict(runtime, task, output)
    runtime.ingest_response(task, call, {"output": output})
    runtime.ingest_response(task, call, {"output": output})
    rows = store.rows("SELECT body FROM deliveries")
    assert len(rows) == 1 and rows[0]["body"].count(UPDATE["why"]) == 1
    assert rows[0]["body"].startswith("Tools: `read_file`, `read_file`\n\n**Observed:**")
    assert "private-fixture" not in rows[0]["body"]
    assert (
        store.get(store.one("SELECT response_hash FROM model_calls")["response_hash"])["output"]
        == output
    )
    for tool in store.rows("SELECT * FROM tool_calls"):
        await runtime.execute(task, tool)
    assert store.one("SELECT count(*) n FROM tool_results WHERE is_error=0")["n"] == 2
    updates = question_context(store, task)["intermediate_summaries"]
    assert len(updates) == 1 and updates[0]["source"] == "function_call"
    assert UPDATE["observed"] in updates[0]["text"]
    assert store.one("SELECT count(*) n FROM model_calls")["n"] == 1


@pytest.mark.parametrize("progress", [None, {"observed": " ", "next": "Read", "why": "Check"}])
async def test_new_missing_or_empty_progress_rejects_before_effect(store, progress):
    runtime = Runtime(store)
    task = store.new_task("Write safely")
    arguments = {"path": "must-not-exist", "text": "data", "expected_sha256": "absent"}
    if progress is not None:
        arguments["progress"] = progress
    output = [native("write_file", arguments)]
    call = save_strict(runtime, task, output)
    runtime.ingest_response(task, call, {"output": output})
    assert not (store.config.root / "must-not-exist").exists()
    assert store.one("SELECT is_error FROM tool_results")["is_error"]
    receipt = store.get(store.one("SELECT result_hash FROM operations")["result_hash"])
    assert receipt["not_executed"] is True
    assert not store.rows("SELECT * FROM operations WHERE status='running'")


async def test_legacy_calls_remain_executable_and_luna_needs_no_progress(store):
    runtime = Runtime(store)
    task = store.new_task("Legacy accepted work")
    (store.config.root / "one.txt").write_text("exact")
    await invoke(runtime, store, task, "read_file", {"path": "one.txt"}, "legacy")
    assert not store.one("SELECT is_error FROM tool_results")["is_error"]
    child = await runtime.request(
        "astra",
        task,
        "luna",
        {
            "operation": "delegate",
            "role": "luna",
            "purpose": "evidence",
            "mode": "read",
            "objective": "Inspect",
        },
    )
    assert all(
        "progress" not in s["parameters"]["properties"]
        for s in runtime.tools.schemas(child["agent_id"])
    )


async def test_short_command_completes_with_two_astra_decisions_and_public_progress(store):
    runtime = Runtime(store)
    task = store.new_task("Read a completed short command")
    calls = []

    async def infer(agent, task, instructions, items, schemas, **kwargs):
        calls.append(items)
        if len(calls) == 1:
            output = [
                native(
                    "start_process",
                    {
                        "command": "python -c 'import time; time.sleep(1.2); print(\"checked output\")'",
                        "cwd": None,
                        "target": None,
                        "background": None,
                        "progress": {
                            "observed": "The command has not run yet.",
                            "next": "Run the short check.",
                            "why": "Wait for its actual result before answering.",
                        },
                    },
                )
            ]
        else:
            assert len(calls) == 2
            result = store.get(
                store.one("SELECT result_hash FROM operations WHERE name='start_process'")[
                    "result_hash"
                ]
            )
            assert result["result"]["exit_code"] == 0 and "checked output" in result["output"]
            output = [message("The command completed and returned checked output.")]
        ident = save_strict(runtime, task, output, kwargs["item_ids"])
        return ident, {"output": output}

    runtime.models.infer = infer
    await asyncio.wait_for(runtime.run(until_idle=True), 8)
    assert len(calls) == 2 and store.one("SELECT status FROM tasks")["status"] == "completed"
    assert [r["name"] for r in store.rows("SELECT name FROM tool_calls")] == ["start_process"]
    assert any("**Why:**" in r["body"] for r in store.rows("SELECT body FROM deliveries"))


async def test_restart_resumes_process_wait_and_output_without_relaunch(store):
    runtime = Runtime(store)
    task = store.new_task("Recover the same command")
    command = 'python -c \'import time; from pathlib import Path; p=Path("launches"); p.write_text(p.read_text()+"x" if p.exists() else "x"); time.sleep(1.6); print("done")\''
    output = [native("start_process", {"command": command, "progress": UPDATE})]
    call = save_strict(runtime, task, output)
    runtime.ingest_response(task, call, {"output": output})
    tool = store.one("SELECT * FROM tool_calls")
    await runtime.execute(task, tool)
    first = {"process_id": store.one("SELECT id FROM processes")["id"]}
    assert store.one("SELECT process_id FROM process_awaits")["process_id"] == first["process_id"]
    other = Runtime(store)
    other.recover()

    async def forbidden(*a, **k):
        raise AssertionError("Resuming the accepted native call needs no inference")

    other.models.infer = forbidden
    await other.step(task)
    receipt = store.get(store.one("SELECT result_hash FROM operations")["result_hash"])
    assert receipt["process_id"] == first["process_id"]
    assert receipt["result"]["exit_code"] == 0 and "done" in receipt["output"]
    assert (store.config.root / "launches").read_text() == "x"
    assert store.one("SELECT count(*) n FROM processes")["n"] == 1


async def test_steering_interrupts_durable_wait_without_ambiguous_relaunch(store):
    runtime = Runtime(store)
    task = store.new_task("Wait until steered")
    output = [
        native(
            "start_process",
            {"command": "python -c 'import time; time.sleep(2)'", "progress": UPDATE},
        )
    ]
    call = save_strict(runtime, task, output)
    runtime.ingest_response(task, call, {"output": output})
    tool = store.one("SELECT * FROM tool_calls")
    await runtime.execute(task, tool)
    assert store.one("SELECT * FROM process_awaits")
    runtime.steer(task, "Retain the command and reassess when it settles.")
    receipt = store.get(store.one("SELECT result_hash FROM operations")["result_hash"])
    assert receipt["process_id"] and receipt["waiting_interrupted_by_owner"]
    assert receipt["completion_observed"] is False
    assert not store.rows("SELECT 1 FROM operations WHERE status='uncertain'")
    result = await runtime.tools.processes.observe("astra", receipt["process_id"], wait_seconds=3)
    assert result["result"]["exit_code"] == 0


async def test_no_inference_until_every_foreground_result_settles(store, monkeypatch):
    runtime = Runtime(store)
    task = store.new_task("Join two foreground commands")
    decisions = []

    async def infer(agent, current, instructions, items, tools, **kwargs):
        decisions.append(items)
        if len(decisions) == 1:
            output = [
                native("start_process", {"command": label, "progress": UPDATE}, label)
                for label in ("first", "second")
            ]
        else:
            assert len(decisions) == 2
            assert store.one("SELECT count(*) n FROM tool_results")["n"] == 2
            output = [message("Both results are ready.")]
        return save_strict(runtime, task, output, kwargs["item_ids"]), {"output": output}

    async def start(agent, operation, command, **kwargs):
        row = store.one("SELECT * FROM processes WHERE operation_id=?", (operation,))
        if not row:
            store.db.execute(
                "INSERT INTO processes(id,operation_id,agent_id,target,cwd,command_hash,status,directory,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    command,
                    operation,
                    agent,
                    "local",
                    ".",
                    store.put(command),
                    "running",
                    ".",
                    now(),
                ),
            )
        row = store.one("SELECT * FROM processes WHERE operation_id=?", (operation,))
        return {
            "process_id": row["id"],
            "result": {"exit_code": row["exit_code"]} if row["status"] == "completed" else None,
        }

    monkeypatch.setattr(runtime.models, "infer", infer)
    monkeypatch.setattr(runtime.tools.processes, "start", start)
    await runtime.step(task)
    assert len(decisions) == 1 and store.one("SELECT count(*) n FROM process_awaits")["n"] == 2
    for _ in range(5):
        runtime.wake_processes()
        assert store.one("SELECT status FROM tasks")["status"] == "waiting"
    store.db.execute("UPDATE processes SET status='completed',exit_code=0 WHERE id='first'")
    runtime.wake_processes()
    await runtime.step(task)
    assert len(decisions) == 1 and store.one("SELECT status FROM tasks")["status"] == "waiting"
    store.db.execute("UPDATE processes SET status='completed',exit_code=0 WHERE id='second'")
    runtime.wake_processes()
    await runtime.step(task)
    assert len(decisions) == 1 and not store.rows("SELECT * FROM process_awaits")
    await runtime.step(task)
    assert len(decisions) == 2 and store.one("SELECT status FROM tasks")["status"] == "completed"


async def test_capacity_wait_is_durable_and_explicit_background_returns_early(store):
    runtime = Runtime(store)
    task = store.new_task("Capacity is not a model decision")
    store.config.data["runtime"]["max_local_processes"] = 1
    output = [
        native(
            "start_process",
            {
                "command": "python -c 'import time; time.sleep(2)'",
                "background": True,
                "progress": UPDATE,
            },
            "background",
        ),
        native(
            "start_process",
            {"command": "python -c 'print(\"after capacity\")'", "progress": UPDATE},
            "foreground",
        ),
    ]
    call = save_strict(runtime, task, output)
    runtime.ingest_response(task, call, {"output": output})
    calls = store.rows("SELECT * FROM tool_calls ORDER BY rowid")
    await runtime.execute(task, calls[0])
    assert store.one("SELECT count(*) n FROM tool_results")["n"] == 1
    await runtime.execute(task, calls[1])
    assert store.one("SELECT process_id FROM process_awaits")["process_id"] is None
    assert store.one("SELECT count(*) n FROM processes")["n"] == 1
    runtime.wake_processes()
    assert store.one("SELECT status FROM tasks")["status"] == "waiting"
    process = store.one("SELECT id FROM processes")["id"]
    await runtime.tools.processes.observe("astra", process, wait_seconds=3)
    runtime.wake_processes()
    await runtime.execute(task, calls[1])
    result = store.get(
        store.one("SELECT result_hash FROM operations WHERE id=?", (calls[1]["operation_id"],))[
            "result_hash"
        ]
    )
    assert result["result"]["exit_code"] == 0 and "after capacity" in result["output"]
    assert not store.rows("SELECT * FROM process_awaits")
    assert store.one("SELECT count(*) n FROM model_calls")["n"] == 1


def test_old_reader_is_rejected_before_service_replacement(store, monkeypatch):
    from bloom import service

    source = store.config.artifacts / "releases/old"
    source.mkdir(parents=True)
    plan = {"root": str(store.config.root), "source": str(source), "manifest": {}}
    monkeypatch.setattr(service, "source_manifest", lambda path: {})
    store.event("model.tool_contract", {"contract": "public-progress-v1"})
    with pytest.raises(ValueError, match="public progress"):
        service.launch(store.config.root, plan)


async def test_worker_reporting_phase_removes_discovery_without_revoking_accepted_tools(store):
    from bloom.evidence import EvidencePending

    runtime = Runtime(store)
    task = store.new_task("Inspect facts")
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
        ("batch", task, "tool", "gather_evidence", "running", store.put({}), now()),
    )
    with pytest.raises(EvidencePending):
        runtime.evidence.gather(
            "astra", task, "batch", "Find the facts", [{"question": "Read a source"}]
        )
    child = runtime.evidence.children("batch")[0]
    store.db.execute("UPDATE evidence_batches SET deadline=?", (time.time() + 80,))
    store.db.execute("UPDATE tasks SET turns=1 WHERE id=?", (child["id"],))
    runtime.evidence.refresh(child["id"])
    assert runtime.tools.schemas(child["agent_id"]) == []
    assert "read_file" in {
        s["name"] for s in runtime.tools.schemas(child["agent_id"], execution=True)
    }
    assert store.one("SELECT count(*) n FROM item_origins WHERE kind='worker_reporting'")["n"] == 1
    output = [native("start_process", {"command": "echo must-not-run"})]
    call = saved(store, child["id"], output)
    store.db.execute(
        "UPDATE model_calls SET request_hash=? WHERE id=?", (store.put({"tools": []}), call)
    )
    runtime.ingest_response(child["id"], call, {"output": output})
    assert store.one("SELECT is_error FROM tool_results")["is_error"]
    assert not store.rows("SELECT * FROM processes")
