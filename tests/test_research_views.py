import json

import httpx
import pytest

from bloom.runtime import Runtime
from bloom.web import saved_file
from test_control_provenance import invoke
from test_web_search import Pages, reviewer


async def test_research_budget_prevents_more_capture_preserves_sources_and_allows_explicit_expansion(
    store, monkeypatch
):
    runtime = Runtime(store)
    task = store.new_task("Compare sources; keep evidence concise")
    calls = reviewer(store, runtime, monkeypatch)
    store.config.data["research"]["astra_tokens"] = 512
    first = await invoke(
        runtime, store, task, "web_open", {"url": "https://evidence.test/1"}, "first"
    )
    raw = store.get(first["result_hash"])
    source = raw["page"]["text_path"]
    view = store.one("SELECT * FROM research_views WHERE operation_id=?", (first["id"],))
    assert view and view["text_tokens"] >= 512
    envelope = store.get(
        store.one(
            "SELECT envelope_hash FROM items WHERE kind='function_call_output' ORDER BY id DESC"
        )["envelope_hash"]
    )
    assert not any(p["type"] == "input_image" for p in envelope["output"])
    assert raw["page"]["text"].startswith("Full evidence from source 1")
    count = len(Pages.captures)
    blocked = await invoke(
        runtime, store, task, "web_open", {"url": "https://evidence.test/2"}, "budget"
    )
    assert store.get(blocked["result_hash"])["status"] == "evidence_budget_reached"
    assert len(Pages.captures) == count
    ranged = await invoke(
        runtime, store, task, "read_file", {"path": source, "start": 1, "end": 1}, "range"
    )
    assert store.get(ranged["result_hash"])["text"] == "Full evidence from source 1.\n"
    expanded = await invoke(
        runtime,
        store,
        task,
        "web_open",
        {
            "url": "https://evidence.test/2",
            "include_image": True,
            "evidence_expansion": "Need the second source figure to resolve the discrepancy",
        },
        "expand",
    )
    assert expanded["status"] == "completed" and len(Pages.captures) == count + 1
    payload = store.get(
        store.one(
            "SELECT envelope_hash FROM items WHERE kind='function_call_output' ORDER BY id DESC"
        )["envelope_hash"]
    )
    assert any(p["type"] == "input_image" for p in payload["output"])
    call = store.one("SELECT * FROM tool_calls WHERE operation_id=?", (first["id"],))
    before = len(calls)
    assert await runtime.results.present(
        "astra", task, call, raw, first["result_hash"]
    ) == store.get(view["view_hash"])
    assert len(calls) == before


async def test_explicit_sol_assignment_and_scoped_luna_can_read_granted_parent_capture(
    store, monkeypatch
):
    runtime = Runtime(store)
    task = store.new_task("Use an existing captured source")
    reviewer(store, runtime, monkeypatch)
    opened = await invoke(
        runtime, store, task, "web_open", {"url": "https://evidence.test/1"}, "capture"
    )
    path = store.get(opened["result_hash"])["page"]["text_path"]
    assignment = await invoke(
        runtime,
        store,
        task,
        "delegate_task",
        {
            "objective": "Investigate saved evidence",
            "criteria": "Quote the source",
            "background": True,
        },
        "sol",
    )
    child = store.get(assignment["result_hash"])["task_id"]
    agent = store.one("SELECT agent_id FROM tasks WHERE id=?", (child,))["agent_id"]
    assert saved_file(store, agent, path)
    read = await invoke(
        runtime, store, child, "read_file", {"path": path, "start": 1, "end": 1}, "read-granted"
    )
    assert read["status"] == "completed"
    launch = await invoke(
        runtime,
        store,
        child,
        "apply_request",
        {
            "path": "config/agent-presets/inspect-files.toml",
            "bindings": {
                "question": "Read the first line",
                "cwd": str(store.config.root),
                "sources": [path],
            },
        },
        "luna",
    )
    luna = store.get(launch["result_hash"])["launched"][0]
    result = await invoke(
        runtime, store, luna, "read_file", {"path": path, "start": 1, "end": 1}, "read-child-grant"
    )
    assert result["status"] == "completed"
    other = store.new_task("Unrelated private evidence")
    ungranted = await runtime.tools.web.open(
        "astra", other, "other-capture", "https://evidence.test/3"
    )
    with pytest.raises(PermissionError, match="agent tree"):
        saved_file(store, agent, ungranted["page"]["text_path"])


async def test_completed_invalid_web_review_has_one_receipted_correction_and_no_recapture(
    store, monkeypatch
):
    runtime = Runtime(store)
    task = store.new_task("Inspect all candidates")
    calls = reviewer(store, runtime, monkeypatch)
    handler = runtime.models.transport.handler

    def incomplete_once(request):
        response = handler(request)
        if len(calls) == 1:
            value = json.loads(response.content)
            text = json.loads(value["output"][0]["content"][0]["text"])
            text["sources"].pop()
            value["output"][0]["content"][0]["text"] = json.dumps(text)
            return httpx.Response(200, json=value)
        return response

    runtime.models.transport = httpx.MockTransport(incomplete_once)
    result = await runtime.tools.web.search("astra", task, "review-repair", "sources")
    assert result["status"] == "ok" and len(calls) == 2 and len(Pages.captures) == 20
    receipts = store.rows(
        "SELECT id,status FROM model_calls WHERE purpose='web_review' ORDER BY rowid"
    )
    assert receipts[-1]["id"].endswith("_correction") and all(
        r["status"] == "completed" for r in receipts
    )
    assert await runtime.tools.web.search("astra", task, "review-repair", "sources") == result
    assert len(calls) == 2 and len(Pages.captures) == 20


async def test_review_upgrade_reuses_completed_inference_without_changing_original_request(
    store, monkeypatch
):
    runtime = Runtime(store)
    task = store.new_task("Review recovery across a representation change")
    calls = reviewer(store, runtime, monkeypatch)
    web = runtime.tools.web
    result = await web.search("astra", task, "review-upgrade", "sources")
    receipt = store.one("SELECT * FROM model_calls WHERE purpose='web_review'")
    original = store.bytes(receipt["request_hash"])
    store.db.execute(
        "UPDATE web_runs SET review_hash=NULL,result_hash=NULL WHERE operation_id=?",
        ("review-upgrade",),
    )
    # A new serialization differs, but the completed response is authoritative.
    from bloom import web as module

    monkeypatch.setattr(
        module,
        "review_input",
        lambda *args: [{"role": "user", "content": "changed document format"}],
    )
    recovered = await web.search("astra", task, "review-upgrade", "sources")
    assert recovered["review"] == result["review"] and len(calls) == 1 and len(Pages.captures) == 20
    assert store.bytes(receipt["request_hash"]) == original
