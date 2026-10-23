import asyncio
import collections
import json
import time

import httpx
import pytest

from bloom import turns
from bloom.evidence import EvidencePending
from bloom.models import Models
from bloom.runtime import Runtime
from test_control_provenance import invoke
from test_runtime_delivery import message, saved


def batch_arguments(store):
    return {
        "plan": "Compare independent source facts; inspect evidence before deciding.",
        "assignments": [
            {"question": "Read the left marker", "cwd": str(store.config.root / "left")},
            {"question": "Read the right marker", "cwd": str(store.config.root / "right")},
        ],
    }


def sources(store):
    for name in ("left", "right"):
        directory = store.config.root / name
        directory.mkdir()
        (directory / "source.txt").write_text(name + "-verified-marker")


async def test_parallel_evidence_needs_only_plan_and_synthesis_from_astra(store):
    sources(store)
    store.config.data["runtime"]["curation_threshold_chars"] = 1
    runtime = Runtime(store)
    task = store.new_task("Compare the two source facts")
    counts = collections.Counter()
    entered = set()
    overlap = asyncio.Event()

    async def infer(agent, current, instructions, items, tools, **kwargs):
        counts[agent] += 1
        if agent == "astra" and counts[agent] == 1:
            output = [
                {
                    "type": "function_call",
                    "call_id": "parallel",
                    "name": "gather_evidence",
                    "arguments": json.dumps(batch_arguments(store)),
                }
            ]
        elif agent == "astra":
            assert counts[agent] == 2
            reports = [
                json.loads(item["output"])["result"]["reports"]
                for item in items
                if item.get("type") == "function_call_output"
            ]
            assert len(reports) == 1 and len(reports[0]) == 2
            assert {r["evidence"][0]["quote"] for r in reports[0]} == {
                "left-verified-marker",
                "right-verified-marker",
            }
            assert all(r["status"] == "found" for r in reports[0])
            output = [message("Both independent facts are verified; here is my comparison.")]
        elif counts[agent] == 1:
            entered.add(agent)
            if len(entered) == 2:
                overlap.set()
            await asyncio.wait_for(overlap.wait(), 3)
            assert "Astra owns the reasoning plan" in instructions
            assert {t["name"] for t in tools} == {
                "read_file",
                "read_artifact",
                "view_image",
                "start_process",
                "observe_process",
            }
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
                "SELECT * FROM operations WHERE task_id=? AND name='read_file'", (current,)
            )
            quote = store.get(receipt["result_hash"])["text"]
            output = [
                message(
                    json.dumps(
                        {
                            "status": "found",
                            "summary": "Exact source marker.",
                            "evidence": [{"operation_id": receipt["id"], "quote": quote}],
                        }
                    )
                )
            ]
        call = saved(store, current, output, kwargs["item_ids"])
        role = store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"]
        store.db.execute(
            "UPDATE model_calls SET model_requested=?,input_tokens=20,cached_tokens=10,output_tokens=3 WHERE id=?",
            (store.config.profile(role)["model"], call),
        )
        return call, {"output": output}

    runtime.models.infer = infer
    await asyncio.wait_for(runtime.run(until_idle=True), 10)
    assert counts["astra"] == 2 and len(entered) == 2
    assert len(store.rows("SELECT * FROM evidence_workers")) == 2
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert not store.rows("SELECT * FROM item_origins WHERE kind='child_report'")
    assert not store.rows("SELECT * FROM channel_routes")
    assert not store.rows("SELECT * FROM deliveries WHERE task_id!=?", (task,))
    turn = store.one("SELECT id FROM owner_turns WHERE task_id=?", (task,))["id"]
    assert turns.usage(store, turn) == {
        "calls": 6,
        "input": 120,
        "cached": 60,
        "output": 18,
        "unknown_calls": 0,
    }


async def pending_batch(store):
    sources(store)
    runtime = Runtime(store)
    task = store.new_task("Inspect two independent sources")
    operation = await invoke(
        runtime, store, task, "gather_evidence", batch_arguments(store), "batch"
    )
    assert operation["status"] == "running"
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "waiting"
    return runtime, task, operation


async def test_batch_recovery_reuses_workers_and_joins_without_astra_inference(store):
    runtime, task, operation = await pending_batch(store)
    children = runtime.evidence.children(operation["id"])
    first = children[0]
    output = [
        message(
            json.dumps(
                {
                    "status": "not_found",
                    "summary": "The source had no matching record.",
                    "evidence": [],
                }
            )
        )
    ]
    call = saved(store, first["id"], output)
    runtime.ingest_response(first["id"], call, {"output": output})
    await runtime.finish(first["id"])
    runtime.block(children[1]["id"], "Unavailable source")
    recovered = Runtime(store)
    recovered.recover()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Saved workers and native tool must not require inference to collect")

    recovered.models.infer = forbidden
    await recovered.evidence.poll()
    await recovered.step(task)
    receipt = store.one("SELECT * FROM operations WHERE id=?", (operation["id"],))
    assert receipt["status"] == "completed"
    assert [r["status"] for r in store.get(receipt["result_hash"])["reports"]] == [
        "not_found",
        "unavailable",
    ]
    assert len(store.rows("SELECT * FROM evidence_workers")) == 2
    assert store.one("SELECT count(*) n FROM model_calls")["n"] == 2


async def test_pending_batch_identity_and_read_only_scope(store):
    runtime, task, operation = await pending_batch(store)
    arguments = batch_arguments(store)
    with pytest.raises(EvidencePending):
        runtime.evidence.gather("astra", task, operation["id"], **arguments)
    assert len(store.rows("SELECT * FROM agents")) == 3
    with pytest.raises(ValueError, match="different work"):
        runtime.evidence.gather(
            "astra", task, operation["id"], "Changed objective", arguments["assignments"]
        )
    child = runtime.evidence.children(operation["id"])[0]
    with pytest.raises(PermissionError):
        await runtime.tools.run(
            child["agent_id"],
            child["id"],
            "invalid-write",
            "write_file",
            {"path": "x", "text": "x", "expected_sha256": "absent"},
        )
    with pytest.raises(PermissionError):
        await runtime.tools.run(
            child["agent_id"],
            child["id"],
            "outside",
            "read_file",
            {"path": str(store.config.root / "right/source.txt")},
        )


@pytest.mark.parametrize("control", ["deadline", "steer"])
async def test_pending_batch_interrupts_children_and_settles_native_result(store, control):
    runtime, task, operation = await pending_batch(store)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Settling accepted evidence must not purchase another decision")

    runtime.models.infer = forbidden
    if control == "deadline":
        store.db.execute("UPDATE evidence_batches SET deadline=0")
    else:
        runtime.steer(task, "Stop that investigation and answer the new question")
    await runtime.evidence.poll()
    if control == "deadline":
        from bloom import reporting

        children = runtime.evidence.children(operation["id"])
        assert all(c["status"] == "ready" and reporting.phase(store, c["id"]) for c in children)
        # Exploration is over, but retained evidence gets a report-only phase;
        # Astra is not invoked to poll. Complete those native reports directly.
        for child in children:
            output = [
                message(
                    json.dumps(
                        {
                            "status": "unavailable",
                            "summary": "No evidence collected before exploration ended.",
                            "evidence": [],
                        }
                    )
                )
            ]
            call = saved(store, child["id"], output)
            runtime.ingest_response(child["id"], call, {"output": output})
            await runtime.finish(child["id"])
        await runtime.evidence.poll()
    else:
        assert all(c["status"] == "blocked" for c in runtime.evidence.children(operation["id"]))
    await runtime.step(task)
    receipt = store.one("SELECT * FROM operations WHERE id=?", (operation["id"],))
    assert receipt["status"] == "completed"
    assert all(r["status"] == "unavailable" for r in store.get(receipt["result_hash"])["reports"])
    assert store.one("SELECT count(*) n FROM model_calls WHERE agent_id='astra'")["n"] == 1


async def test_paused_batch_keeps_native_pair_pending_until_resume(store):
    runtime, task, operation = await pending_batch(store)
    runtime.pause(task)
    await runtime.evidence.poll()
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "paused"
    assert not store.rows("SELECT * FROM tool_results")
    runtime.resume(task)
    for child in runtime.evidence.children(operation["id"]):
        runtime.block(child["id"], "Scoped evidence unavailable")
    await runtime.evidence.poll()
    await runtime.step(task)
    assert (
        store.one("SELECT status FROM operations WHERE id=?", (operation["id"],))["status"]
        == "completed"
    )


async def test_worker_report_rejects_fabricated_or_sibling_evidence(store):
    runtime, task, operation = await pending_batch(store)
    first, second = runtime.evidence.children(operation["id"])
    receipt = await invoke(
        runtime, store, first["id"], "read_file", {"path": "source.txt"}, "source"
    )
    for child, quote in ((first, "invented quotation"), (second, "left-verified-marker")):
        output = [
            message(
                json.dumps(
                    {
                        "status": "found",
                        "summary": "Claimed finding",
                        "evidence": [{"operation_id": receipt["id"], "quote": quote}],
                    }
                )
            )
        ]
        call = saved(store, child["id"], output)
        runtime.ingest_response(child["id"], call, {"output": output})
        await runtime.finish(child["id"])
        report = runtime.evidence.report(
            store.one("SELECT * FROM tasks WHERE id=?", (child["id"],))
        )
        assert report["status"] in ("partial", "unavailable")
        assert all(source["quote"] != "invented quotation" for source in report["evidence"])
        assert all(
            store.one("SELECT task_id FROM operations WHERE id=?", (source["id"],))["task_id"]
            == child["id"]
            for source in report["evidence"]
        )
        assert store.one("SELECT status FROM tasks WHERE id=?", (child["id"],))["status"] == "ready"
        assert (
            store.one(
                "SELECT count(*) n FROM items i JOIN item_origins o ON o.item_id=i.id WHERE i.task_id=? AND o.kind='evidence_feedback'",
                (child["id"],),
            )["n"]
            == 1
        )
    # Correction stays on Luna; Astra's native batch is still pending.
    assert store.one("SELECT turns FROM tasks WHERE id=?", (task,))["turns"] == 0
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "waiting"


async def test_quick_process_returns_terminal_result_without_paid_poll(store):
    runtime = Runtime(store)
    task = store.new_task("Small read-only command")
    op = await invoke(
        runtime,
        store,
        task,
        "start_process",
        {"command": "sleep 0.12; printf verified-fast-result"},
        "quick",
    )
    result = store.get(op["result_hash"])
    assert result["result"]["exit_code"] == 0
    assert result["output"] == "verified-fast-result"


async def test_exploration_deadline_keeps_purchased_report_and_returns_explicit_missing_evidence(
    store, monkeypatch
):
    runtime, task, operation = await pending_batch(store)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    for profile in store.config.data["profiles"].values():
        profile["provider"] = "openai"
    store.config.data["providers"]["openai"]["stream"] = False
    entered = []

    async def handler(request):
        body = json.loads(request.content)
        if body["model"] == store.config.profile("luna")["model"]:
            entered.append(body["model"])
            await asyncio.sleep(0.8)  # Cross the exploration deadline in-flight.
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        message(
                            json.dumps(
                                {
                                    "status": "unavailable",
                                    "summary": "No finding is claimed; the source was not inspected.",
                                    "evidence": [],
                                }
                            )
                        )
                    ],
                    "usage": {
                        "input_tokens": 20,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": 5,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [message("The evidence workers timed out; no finding is claimed.")],
                "usage": {
                    "input_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 5,
                },
            },
        )

    runtime.models = Models(store, httpx.MockTransport(handler))
    store.db.execute("UPDATE evidence_batches SET deadline=?", (time.time() + 0.5,))
    await asyncio.wait_for(runtime.run(until_idle=True), 5)
    assert len(entered) == 2
    children = runtime.evidence.children(operation["id"])
    assert all(c["status"] == "completed" for c in children)
    calls = store.rows("SELECT status,input_tokens FROM model_calls WHERE agent_id!='astra'")
    assert len(calls) == 2 and all(
        c["status"] == "completed" and c["input_tokens"] == 20 for c in calls
    )
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert store.one("SELECT count(*) n FROM evidence_workers")["n"] == 2


async def test_reset_clears_batches_after_verified_backup(store):
    from bloom import reset

    runtime, task, operation = await pending_batch(store)
    for child in runtime.evidence.children(operation["id"]):
        store.status(child["id"], "cancelled", "Fixture inspection stopped")
    await runtime.evidence.poll()
    await runtime.step(task)
    store.status(task, "completed")
    await runtime.delivery.flush()
    config = store.config
    store.close()
    result = reset.apply(config)
    assert result["backup"]["verified"]
    assert result["after"]["evidence_batches"] == result["after"]["evidence_workers"] == 0


async def test_old_release_cannot_replace_active_batch_runtime(store, monkeypatch):
    from bloom import service

    runtime, task, operation = await pending_batch(store)
    root = store.config.root
    source = root / "artifacts/releases/old"
    source.mkdir(parents=True)
    (source / "old.txt").write_text("old release lacks evidence coordinator")
    plan = {"root": str(root), "source": str(source), "manifest": service.source_manifest(source)}

    def forbidden(*args, **kwargs):
        raise AssertionError("Compatibility must be checked before stopping the service")

    monkeypatch.setattr(service, "unload", forbidden)
    with pytest.raises(ValueError, match="parallel evidence"):
        service.launch(root, plan)


async def test_default_http_client_cannot_reach_a_real_service():
    async with httpx.AsyncClient() as client:
        with pytest.raises(AssertionError, match="unmocked HTTP"):
            await client.get("https://example.invalid/never-requested")


async def test_worker_limit_preserves_useful_original_receipts(store):
    runtime, task, operation = await pending_batch(store)
    first, second = runtime.evidence.children(operation["id"])
    receipt = await invoke(
        runtime, store, first["id"], "read_file", {"path": "source.txt"}, "partial-source"
    )
    store.db.execute("UPDATE tasks SET turns=max_turns WHERE id=?", (first["id"],))
    from bloom import reporting

    reporting.prepare(
        store,
        first["id"],
        runtime.evidence.worker(first["id"])["deadline"],
        reserve_turns=4,
        reserve_seconds=90,
    )
    store.db.execute(
        "UPDATE worker_reporting SET start_turn=start_turn-2 WHERE task_id=?", (first["id"],)
    )
    await runtime.step(first["id"])
    await runtime.evidence.stop(second["id"], "Other source unavailable")
    await runtime.evidence.poll()
    await runtime.step(task)
    result = store.get(
        store.one("SELECT result_hash FROM operations WHERE id=?", (operation["id"],))[
            "result_hash"
        ]
    )
    assert result["reports"][0]["status"] == "partial"
    assert result["reports"][0]["evidence"][0]["quote"] == "left-verified-marker"
    assert result["reports"][0]["evidence"][0]["id"] == receipt["id"]
    assert result["reports"][1]["status"] == "unavailable"


async def test_worker_is_prompted_to_report_before_budget_exhaustion(store):
    runtime, task, operation = await pending_batch(store)
    child = runtime.evidence.children(operation["id"])[0]
    store.db.execute("UPDATE tasks SET turns=max_turns-4 WHERE id=?", (child["id"],))
    runtime.evidence.refresh(child["id"])
    runtime.evidence.refresh(child["id"])
    rows = store.rows(
        "SELECT i.text FROM items i JOIN item_origins o ON o.item_id=i.id WHERE o.kind='worker_reporting'"
    )
    assert len(rows) == 1 and "Reporting phase" in rows[0]["text"]
    assert store.one("SELECT turns FROM tasks WHERE id=?", (task,))["turns"] == 0


async def test_evidence_process_timeout_retains_output_and_stops_its_own_command(store):
    runtime, task, operation = await pending_batch(store)
    store.config.data["runtime"]["evidence_process_seconds"] = 1
    child = runtime.evidence.children(operation["id"])[0]
    op = await invoke(
        runtime,
        store,
        child["id"],
        "start_process",
        {"command": "printf partial-evidence; sleep 60"},
        "bounded-inspection",
    )
    process = store.one("SELECT id FROM processes WHERE operation_id=?", (op["id"],))["id"]
    async with asyncio.timeout(5):
        while True:
            result = await runtime.tools.processes.observe(child["agent_id"], process)
            if result.get("result"):
                break
            await asyncio.sleep(0.1)
    assert result["result"]["exit_code"] == 124 and result["result"]["timed_out"]
    assert result["output"] == "partial-evidence"
    assert store.one("SELECT status FROM processes WHERE id=?", (process,))["status"] == "failed"


async def test_derived_worker_compaction_has_separate_source_membership(store, monkeypatch):
    from bloom.context import Context

    runtime, task, operation = await pending_batch(store)
    child = runtime.evidence.children(operation["id"])[0]
    for n in range(8):
        store.item(
            child["agent_id"],
            {"role": "user", "content": f"Observation {n}: " + "original evidence " * 100},
            child["id"],
        )
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    store.config.profile("luna")["provider"] = "openai"
    store.config.data["providers"]["openai"]["stream"] = False
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert len(body["input"]) == 1
        assert len(json.loads(body["input"][0]["content"])["older_context"]) > 1
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [message("Derived summary with original source references.")],
                "usage": {
                    "input_tokens": 300,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 20,
                },
            },
        )

    context = Context(store, Models(store, httpx.MockTransport(handler)))
    identity = await context.compact(child["agent_id"], child["id"], target=800)
    assert identity and len(requests) == 1
    call = store.one("SELECT id,status FROM model_calls WHERE purpose='compaction'")
    assert call["status"] == "completed"
    assert not store.rows("SELECT * FROM model_call_items WHERE call_id=?", (call["id"],))
    sources = store.rows("SELECT item_id FROM model_call_sources WHERE call_id=?", (call["id"],))
    assert len(sources) > 1
    selected = {r["id"] for r in context.selected(child["agent_id"], child["id"])}
    assert not selected.intersection(r["item_id"] for r in sources)
    assert all(store.one("SELECT id FROM items WHERE id=?", (r["item_id"],)) for r in sources)
    from bloom import service

    source = store.config.root / "artifacts/releases/old-compaction-reader"
    source.mkdir(parents=True)
    (source / "old.txt").write_text("No derived-source reader")
    plan = {
        "root": str(store.config.root),
        "source": str(source),
        "manifest": service.source_manifest(source),
    }
    with pytest.raises(ValueError, match="derived compaction sources"):
        service.launch(store.config.root, plan)
