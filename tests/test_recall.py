import json
import time

import httpx
import pytest

from bloom.models import Models
from bloom.runtime import Runtime
from bloom.store import Store
from bloom import turns
from test_runtime_delivery import message
from test_worker_presets import wire


def fixture_runtime(store):
    store.config.data["memory"]["retrieval_enabled"] = True
    store.config.data["providers"]["fixture"] = {
        "kind": "responses",
        "auth": "none",
        "base_url": "http://localhost",
        "stream": False,
    }
    for role in ("astra", "luna"):
        store.config.data["profiles"][role]["provider"] = "fixture"
    old = store.new_task("The orchid calibration marker is iris-739.")
    store.status(old, "completed")
    source = store.one("SELECT id FROM items WHERE task_id=?", (old,))["id"]
    task = store.new_task("Retrieve the orchid calibration marker.")
    store.status(task, "running")
    seen = []

    def transport(request):
        body = json.loads(request.content)
        seen.append(body)
        if body["model"] == store.config.profile("luna")["model"]:
            results = [x for x in body["input"] if x.get("type") == "function_call_output"]
            if not results:
                output = [
                    {
                        "type": "function_call",
                        "call_id": "search",
                        "name": "memory_search",
                        "arguments": json.dumps({"query": "orchid calibration"}),
                    }
                ]
            elif len(results) == 1:
                output = [
                    {
                        "type": "function_call",
                        "call_id": "read",
                        "name": "memory_read",
                        "arguments": json.dumps({"node_id": "event:" + str(source)}),
                    }
                ]
            else:
                output = [
                    message(
                        json.dumps(
                            {
                                "status": "found",
                                "summary": "The prior calibration used iris-739.",
                                "sources": [
                                    {"node_id": "event:" + str(source), "quote": "iris-739"}
                                ],
                            }
                        )
                    )
                ]
        else:
            found = any("memory_retrieval_report" in json.dumps(x) for x in body["input"])
            if sum(r["model"] == store.config.profile("astra")["model"] for r in seen) == 1:
                output = [
                    wire(
                        runtime,
                        "apply_request",
                        {
                            "path": "config/agent-presets/recall.toml",
                            "bindings": {"question": "Retrieve the orchid calibration marker."},
                        },
                        "launch",
                    ),
                    wire(runtime, "await_workers", {}, "join"),
                ]
            else:
                output = [
                    message(
                        "Confirmed iris-739." if found else "Premature answer must be withheld."
                    )
                ]
        return httpx.Response(
            200,
            json={
                "id": "response-" + str(len(seen)),
                "status": "completed",
                "output": output,
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 50},
                    "output_tokens": 10,
                },
            },
        )

    runtime = Runtime(store, Models(store, httpx.MockTransport(transport)))
    return runtime, task, seen


async def test_astra_starts_recall_then_background_reads_join_final_and_account_usage(store):
    runtime, task, seen = fixture_runtime(store)
    await runtime.step(task)
    job = store.one("SELECT * FROM memory_retrievals")
    assert [r["model"] for r in seen] == [store.config.profile("astra")["model"]]
    assert (
        job["triaged"] == 0
        and store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "waiting"
    )
    assert store.one("SELECT final_item FROM tasks WHERE id=?", (task,))["final_item"] is None
    assert {t["name"] for t in runtime.tools.schemas(job["agent_id"])} == {
        "memory_search",
        "memory_read",
    }
    await runtime.step(job["task_id"])
    await runtime.step(job["task_id"])
    await runtime.step(job["task_id"])
    assert any("parent observation" in json.dumps(x) for x in seen[2]["input"])
    assert store.one("SELECT report_item FROM memory_retrievals")["report_item"] is None
    assert store.one("SELECT report_hash FROM memory_retrievals")["report_hash"]
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "waiting"
    runtime.workers.poll()
    await runtime.step(task)
    await runtime.step(task)
    report = store.one(
        "SELECT * FROM items WHERE task_id=? AND id IN (SELECT report_item FROM memory_retrievals)",
        (task,),
    )
    payload = json.loads(report["text"])
    assert payload["sources"][0]["quote"] == "iris-739" and payload["sources"][0]["result_hash"]
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert [
        r["body"]
        for r in store.rows(
            "SELECT body FROM deliveries WHERE task_id=? AND item_id=(SELECT final_item FROM tasks WHERE id=?)",
            (task, task),
        )
    ] == ["Confirmed iris-739."]
    assert not store.rows("SELECT * FROM deliveries WHERE task_id=?", (job["task_id"],))
    assert turns.usage(store, job["turn_id"])["calls"] == 5
    astra = [r for r in seen if r["model"] == store.config.profile("astra")["model"]]
    assert astra[1]["input"][: len(astra[0]["input"])] == astra[0]["input"]
    # Derived retrieval reports must not feed themselves back into consolidation.
    runtime.context.history.index()
    assert (
        store.one("SELECT node_id FROM memory_indexed WHERE item_id=?", (report["id"],))["node_id"]
        is None
    )


async def test_real_coordinator_runs_retrieval_worker_to_completion(store):
    runtime, task, seen = fixture_runtime(store)
    await runtime.run(until_idle=True)
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert store.one("SELECT report_item FROM memory_retrievals")["report_item"]
    assert not store.rows("SELECT id FROM tasks WHERE status IN ('ready','running','waiting')")


async def test_retrieval_restart_reuses_completed_triage_and_read_receipts(store):
    runtime, task, seen = fixture_runtime(store)
    await runtime.step(task)
    job = store.one("SELECT * FROM memory_retrievals")
    await runtime.step(job["task_id"])
    assert len(seen) == 2
    store.db.execute("UPDATE memory_retrievals SET triaged=0")
    reopened = Store(store.config)
    try:
        models = Models(reopened, runtime.models.transport)
        resumed = Runtime(reopened, models)
        resumed.recover()
        assert len(seen) == 2
        await resumed.step(job["task_id"])
        await resumed.step(job["task_id"])
        resumed.notify_parent(job["task_id"])
        resumed.workers.poll()
        await resumed.step(task)
        resumed.recall.flush(task)
        assert (
            reopened.one("SELECT count(*) AS n FROM item_origins WHERE kind='memory_retrieval'")[
                "n"
            ]
            == 1
        )
    finally:
        reopened.close()


async def test_new_owner_input_supersedes_worker_without_late_report(store):
    runtime, task, seen = fixture_runtime(store)
    await runtime.step(task)
    job = store.one("SELECT * FROM memory_retrievals")
    runtime.steer(task, "Different request; the old lookup is obsolete.")
    runtime.recall.poll()
    assert (
        store.one("SELECT status FROM tasks WHERE id=?", (job["task_id"],))["status"] == "cancelled"
    )
    assert store.one("SELECT report_item FROM memory_retrievals")["report_item"] is None
    assert not store.rows("SELECT item_id FROM item_origins WHERE kind='memory_retrieval'")


async def test_retrieval_deadline_is_explicit_and_cannot_hold_parent_forever(store):
    runtime, task, seen = fixture_runtime(store)
    await runtime.step(task)
    job = store.one("SELECT * FROM memory_retrievals")
    store.db.execute("UPDATE memory_retrievals SET deadline=?", (time.time() - 1,))
    runtime.recall.poll()
    runtime.workers.poll()
    assert (
        store.one("SELECT status FROM tasks WHERE id=?", (job["task_id"],))["status"] == "blocked"
    )
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "ready"
    report = store.get(store.one("SELECT report_hash FROM memory_retrievals")["report_hash"])
    assert report["status"] == "unavailable" and "deadline" in report["summary"]


async def test_retrieval_cannot_read_other_queued_task_or_mutate_files(store):
    runtime, task, seen = fixture_runtime(store)
    private = store.new_task("Unrelated queued owner secret fixture")
    source = store.one("SELECT id FROM items WHERE task_id=?", (private,))["id"]
    await runtime.step(task)
    job = store.one("SELECT * FROM memory_retrievals")
    with pytest.raises(PermissionError):
        await runtime.tools.run(
            job["agent_id"],
            job["task_id"],
            "probe",
            "memory_read",
            {"node_id": "event:" + str(source)},
        )
    with pytest.raises(PermissionError):
        await runtime.tools.run(
            job["agent_id"],
            job["task_id"],
            "probe",
            "write_file",
            {"path": "x", "text": "x", "expected_sha256": "absent"},
        )


async def test_retrieval_report_rejects_unread_or_fabricated_quote(store):
    runtime, task, seen = fixture_runtime(store)
    await runtime.step(task)
    job = store.one("SELECT * FROM memory_retrievals")
    store.item(
        job["agent_id"],
        message(
            json.dumps(
                {
                    "status": "found",
                    "summary": "Unsupported conclusion",
                    "sources": [{"node_id": "event:1", "quote": "invented"}],
                }
            )
        ),
        job["task_id"],
    )
    store.status(job["task_id"], "completed")
    runtime.notify_parent(job["task_id"])
    report = store.get(store.one("SELECT report_hash FROM memory_retrievals")["report_hash"])
    assert report["status"] == "unavailable" and not report["sources"]
    assert (
        store.one("SELECT status FROM tasks WHERE id=?", (job["task_id"],))["status"] == "blocked"
    )


async def test_report_waits_until_native_tool_pair_settles_and_is_pinned(store):
    from bloom.storage import Archive
    from test_runtime_delivery import saved

    runtime, task, seen = fixture_runtime(store)
    runtime.recall.start(
        task, "explicit-fixture", "Retrieve the orchid marker.", "Find exact historical evidence."
    )
    job = store.one("SELECT * FROM memory_retrievals")
    output = [
        {
            "type": "function_call",
            "call_id": "parent-read",
            "name": "read_file",
            "arguments": '{"path":"missing.txt"}',
        }
    ]
    call = saved(store, task, output)
    runtime.ingest_response(task, call, {"output": output})
    await runtime.step(job["task_id"])
    await runtime.step(job["task_id"])
    await runtime.step(job["task_id"])
    ready = store.one("SELECT * FROM memory_retrievals")
    assert ready["report_hash"] and ready["report_item"] is None
    assert ready["report_hash"] in Archive(store).pins()
    assert runtime.recall.flush(task) == []
    tool = store.one("SELECT * FROM tool_calls WHERE task_id=?", (task,))
    runtime.result_item(tool, {"error": "fixture not found"}, True)
    result_id = store.one("SELECT item_id FROM tool_results WHERE call_id=?", (tool["id"],))[
        "item_id"
    ]
    applied = runtime.recall.flush(task)
    assert len(applied) == 1 and applied[0] > result_id
    assert runtime.recall.flush(task) == []


async def test_inflight_retrieval_timeout_preserves_unknown_usage_without_retry(store):
    import asyncio

    runtime, task, seen = fixture_runtime(store)
    store.config.data["memory"]["retrieval_seconds"] = 0.05
    entered = asyncio.Event()

    async def slow(request):
        entered.set()
        await asyncio.Event().wait()

    runtime.models.transport = httpx.MockTransport(slow)
    child = runtime.recall.start(
        task, "explicit-timeout", "Retrieve the marker.", "Find exact historical evidence."
    )
    await asyncio.wait_for(runtime.step(child), 1)
    assert entered.is_set()
    job = store.one("SELECT * FROM memory_retrievals")
    assert (
        store.one("SELECT status FROM tasks WHERE id=?", (job["task_id"],))["status"] == "blocked"
    )
    calls = store.rows("SELECT status,input_tokens,cached_tokens,output_tokens FROM model_calls")
    assert calls == [
        {"status": "uncertain", "input_tokens": None, "cached_tokens": None, "output_tokens": None}
    ]
    assert (
        runtime.recall.start(
            task, "explicit-timeout", "Retrieve the marker.", "Find exact historical evidence."
        )
        == child
    )
    assert store.one("SELECT count(*) AS n FROM model_calls")["n"] == 1
    assert (
        store.get(store.one("SELECT report_hash FROM memory_retrievals")["report_hash"])["status"]
        == "unavailable"
    )


async def test_old_release_cannot_resume_managed_retrieval_state(store):
    from bloom.service import launch

    runtime, task, seen = fixture_runtime(store)
    runtime.recall.start(
        task, "explicit-fixture", "Retrieve the marker.", "Find exact historical evidence."
    )
    legacy = store.config.artifacts / "releases/legacy"
    legacy.mkdir(parents=True)
    plan = {"root": str(store.config.root), "source": str(legacy), "manifest": {}}
    with pytest.raises(ValueError, match="cannot resume managed memory retrieval"):
        launch(store.config.root, plan)
