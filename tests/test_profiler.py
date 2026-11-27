from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from bloom41.store import Store, digest, encoded

spec = importlib.util.spec_from_file_location(
    "profile_run", Path(__file__).resolve().parents[1] / "scripts/profile_run.py"
)
profiler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profiler)


def fixture_trace(tmp_path, monkeypatch):
    store = Store(tmp_path / "workspace")
    base = datetime(2026, 11, 26, tzinfo=timezone.utc)

    def iso(seconds):
        return (base + timedelta(seconds=seconds)).isoformat()

    def event(seconds, kind, subject, data, key):
        monkeypatch.setattr("bloom41.store.now", lambda: iso(seconds))
        return store.event(kind, subject, data, key=key)

    def message(seconds, ident, sender, recipient, kind="message", metadata=None):
        event(
            seconds,
            "message.prepared",
            ident,
            {
                "id": ident,
                "sender": sender,
                "recipient": recipient,
                "kind": kind,
                "metadata": metadata or {},
                "body": store.put("Visible text", "text/plain"),
            },
            "message-prepare:" + ident,
        )
        event(seconds, "message.delivered", ident, {}, "message-delivered:" + ident)

    message(0, "fixture-question", "user", "astra")
    event(
        0.1,
        "context.requested",
        "astra",
        {"request_id": "ctx", "original_ids": ["fixture-question"]},
        "ctx",
    )
    message(0.5, "context-return", "terra-astra", "astra", "context", {"request_id": "ctx"})
    event(
        1,
        "turn.received",
        "astra",
        {"turn": "t1", "inputs": ["fixture-question", "context-return"]},
        "received",
    )
    event(
        1, "turn.state", "t1", {"id": "t1", "scope": "astra", "status": "prepared"}, "turn-prepared"
    )
    request = store.put({"input": [], "api_key": "fixture-key", "reasoning": {"effort": "high"}})

    def model(seconds, ident, purpose):
        event(
            seconds,
            "model.started",
            "astra",
            {
                "id": ident,
                "profile": "luna" if purpose == "tool_reader" else "astra",
                "purpose": purpose,
                "input_estimate": 100,
                "request": request,
            },
            "model-started:" + ident,
        )

    model(1.2, "t1", "decision")
    event(
        3,
        "model.completed",
        "astra",
        {
            "id": "t1",
            "response": store.put(
                {
                    "output": [
                        {
                            "type": "reasoning",
                            "summary": "private fixture",
                            "encrypted_content": "private fixture",
                        },
                        {
                            "type": "message",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "Visible answer"}],
                        },
                    ]
                }
            ),
        },
        "model-completed:t1",
    )
    event(
        3.01,
        "turn.state",
        "t1",
        {"id": "t1", "scope": "astra", "status": "completed"},
        "turn-completed",
    )
    for ident in ("fixture-question", "context-return"):
        event(3.02, "message.consumed", ident, {"turn": "t1"}, "message-consumed:" + ident)
    job = {
        "id": "j1",
        "agent": "astra",
        "branch": None,
        "turn": "t1",
        "name": "search",
        "arguments": {"query": "fixture"},
        "parent": None,
    }
    event(3.1, "job.state", "j1", {**job, "status": "prepared"}, "j-prepared")
    event(3.3, "job.state", "j1", {**job, "status": "launching"}, "j-launch")
    event(
        3.35,
        "web.started",
        "astra",
        {"operation": "w1", "job": "j1", "query": "fixture"},
        "web-started:w1",
    )
    reader = "reader_" + digest(encoded(["j1", 1]))[:24]
    model(3.4, reader, "tool_reader")
    source = store.put(
        {
            "title": "</script><script>alert(1)</script>",
            "url": "https://example.com",
            "status": "ok",
            "capture_started_at": iso(3.5),
            "captured_at": iso(3.8),
        }
    )
    event(
        3.8,
        "web.source",
        "astra",
        {"operation": "w1", "source_id": "s1", "source": source},
        "source:s1",
    )
    event(
        3.9,
        "model.failed",
        "astra",
        {"id": reader, "error": "fixture failure"},
        "model-failed:" + reader,
    )
    event(4, "web.completed", "astra", {}, "web-completed:w1")
    event(
        4.1,
        "job.state",
        "j1",
        {**job, "status": "completed", "result": store.put({"text": "result"})},
        "j-completed",
    )
    event(4.2, "job.notified", "j1", {"woke": True}, "job-notified:j1")
    message(5, "answer", "astra", "user", metadata={"origin": "fixture-question"})
    run = tmp_path / "run"
    run.mkdir()
    (run / "fixture-process.json").write_text(
        json.dumps({"started_at": iso(0), "ended_at": iso(5.1)})
    )
    store.close()
    return tmp_path / "workspace", run, reader


def test_receipt_spans_link_failures_and_do_not_count_consumption_after_inference(
    tmp_path, monkeypatch
):
    workspace, run, reader = fixture_trace(tmp_path, monkeypatch)
    before = hashlib.sha256((workspace / "index.sqlite3").read_bytes()).hexdigest()
    trace = profiler.Trace(workspace, run).build()
    spans = trace.span_by_id
    assert not trace.unlinked_readers
    assert spans["model:" + reader]["parent"] == "job:j1"
    assert spans["model:" + reader]["status"] == "error"
    assert (
        spans["message:fixture-question"]["end_us"] - spans["message:fixture-question"]["start_us"]
        == 1_000_000
    )
    assert spans["queue:j1"]["end_us"] - spans["queue:j1"]["start_us"] == 200_000
    assert spans["context:ctx"]["end_us"] - spans["context:ctx"]["start_us"] == 400_000
    result = trace.write(tmp_path / "output", details=True)
    assert result["model_requests"] == 2
    assert result["model_failed"] == 1
    case = result["cases"][0]
    assert case["wall_seconds"] == 5
    assert sum(case["wall_states"].values()) == pytest.approx(5)
    assert case["categories"]["Model decision"]["union_seconds"] == pytest.approx(1.8)
    assert hashlib.sha256((workspace / "index.sqlite3").read_bytes()).hexdigest() == before
    html = (tmp_path / "output/operation-timeline.html").read_text()
    assert "</script><script>alert(1)</script>" not in html
    assert "\\u003c/script\\u003e" in html
    details = "".join(p.read_text() for p in (tmp_path / "output/details").glob("*.json"))
    assert "private fixture" not in details and "fixture-key" not in details
    assert "Visible answer" in details and "high" in details
    with pytest.raises(ValueError, match="outside the original"):
        trace.write(workspace / "output")


def test_missing_process_receipts_do_not_invent_operator_downtime(tmp_path, monkeypatch):
    workspace, run, _ = fixture_trace(tmp_path, monkeypatch)
    (run / "fixture-process.json").unlink()
    trace = profiler.Trace(workspace, run).build()
    assert not any(s["category"] == "Operator pause" for s in trace.spans)
    assert not trace.metrics(trace.cases[0])["wall_states"].get("Operator pause")


def test_profiler_rejects_missing_or_modified_originals(tmp_path, monkeypatch):
    workspace, run, _ = fixture_trace(tmp_path, monkeypatch)
    path = next((workspace / "control/events").glob("*.json"))
    original = json.loads(path.read_bytes())
    original["subject"] = "modified"
    path.write_text(json.dumps(original))
    with pytest.raises(ValueError, match="checksum mismatch"):
        profiler.Trace(workspace, run)
    path.unlink()
    with pytest.raises(ValueError, match="Journal and index disagree"):
        profiler.Trace(workspace, run)


def test_overlap_math_and_nested_private_projection():
    assert profiler.union([(0, 3), (1, 4), (6, 8), (8, 9)]) == [(0, 4), (6, 9)]
    assert profiler.subtract([(0, 10)], [(1, 2), (5, 8)]) == [(0, 1), (2, 5), (8, 10)]
    hidden = {"type": "message", "phase": "analysis", "content": "private"}
    value = {"output": json.dumps([hidden]), "api_key": "credential", "text": "visible"}
    projected = profiler.public(value)
    assert 'private"' not in json.dumps(projected)
    assert "credential" not in json.dumps(projected)
    assert projected["text"] == "visible"
