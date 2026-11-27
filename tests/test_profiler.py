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


def fixture_trace(tmp_path, monkeypatch, *, pending=False):
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
    if pending:
        event(4.3, "job.state", "queued", {**job, "id": "queued", "status": "prepared"}, "queued")
    message(
        5, "answer", "astra", "user", metadata={"origin": "fixture-question", "sender_job": "j1"}
    )
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


@pytest.mark.parametrize("resolved", [False, True])
def test_explicit_operator_recovery_is_separate_and_requires_a_resolution(
    tmp_path, monkeypatch, resolved
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from build_profiler_site import build

    workspace, run, _ = fixture_trace(tmp_path, monkeypatch)
    store = Store(workspace)
    monkeypatch.setattr("bloom41.store.now", lambda: "2026-11-26T00:00:06+00:00")
    recovery = store.event(
        "operator.recovery",
        "fixture-context",
        {"delay_started_at": "2026-11-26T00:00:03.700000+00:00", "reason": "Fixture repair"},
        key="operator-repair:fixture",
    )
    if resolved:
        monkeypatch.setattr("bloom41.store.now", lambda: "2026-11-26T00:00:06.100000+00:00")
        end = store.event(
            "context.finished",
            "astra",
            {"request": "fixture-context", "operator_resolution": recovery["key"]},
            key="context-finished:fixture-context",
        )
    store.close()
    trace = profiler.Trace(workspace, run).build()
    intervals = [s for s in trace.spans if s["category"] == "Operator recovery"]
    case = trace.metrics(trace.cases[0])
    assert sum(case["wall_states"].values()) == pytest.approx(5)
    assert case["categories"]["Model decision"]["union_seconds"] == pytest.approx(1.8)
    if not resolved:
        assert not intervals
        assert "Operator recovery" not in case["categories"]
        return
    assert len(intervals) == 1
    assert intervals[0]["end_us"] - intervals[0]["start_us"] == 2_400_000
    assert intervals[0]["end_event"] == end["seq"]
    assert intervals[0]["attributes"]["recovery_event"] == recovery["seq"]
    assert case["categories"]["Operator recovery"]["union_seconds"] == pytest.approx(1.3)
    assert any("Operator recovery" in key and "Model call" in key for key in case["wall_states"])
    data, _ = build(trace, tmp_path / "site")
    span = next(s for s in data["spans"] if s["category"] == "Operator recovery")
    assert span["l"] == "runtime" and span["k"] == "wait"
    assert next(e for e in data["events"] if e["n"] == span["a"])["derived"]


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


def test_unstarted_queue_is_censored_at_recording_end_and_clipped_to_case(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from build_profiler_site import build

    workspace, run, _ = fixture_trace(tmp_path, monkeypatch, pending=True)
    store = Store(workspace)
    monkeypatch.setattr("bloom41.store.now", lambda: "2026-11-26T00:00:06+00:00")
    store.event("operator.stop_requested", "fixture", {}, key="stop")
    store.close()
    trace = profiler.Trace(workspace, run).build()
    queue = trace.span_by_id["queue:queued"]
    assert queue["end_us"] - queue["start_us"] == 1_700_000
    assert queue["status"] == "warning" and queue["end_event"] is None
    assert queue["attributes"]["queued_at_recording_end"]
    assert "job:queued" not in trace.span_by_id
    waiting = next(
        q for q in trace.metrics(trace.cases[0])["top_queues"] if q["id"] == "queue:queued"
    )
    assert waiting["seconds"] == pytest.approx(0.7)
    assert waiting["queued_at_recording_end"] and waiting["end_event"] is None
    data, _ = build(trace, tmp_path / "site")
    assert [queue["start_event"], "Longest tool queue"] in data["views"]["fixture"]["landmarks"]


def test_lane_viewer_preserves_native_boundaries_and_lazy_visible_payloads(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from build_profiler_site import build

    workspace, run, reader = fixture_trace(tmp_path, monkeypatch)
    trace = profiler.Trace(workspace, run).build()
    original = hashlib.sha256((workspace / "index.sqlite3").read_bytes()).hexdigest()
    output = tmp_path / "site"
    data, report = build(trace, output)
    assert report["model_requests"] == 2
    assert report["receipts"] == len(trace.events)
    assert hashlib.sha256((workspace / "index.sqlite3").read_bytes()).hexdigest() == original
    events = {event["n"]: event for event in data["events"]}
    spans = {span["id"]: span for span in data["spans"]}
    queue = spans["queue:j1"]
    assert queue["v"] - queue["u"] == 200_000
    capture = spans["capture:s1"]
    assert capture["a"] == capture["b"]
    assert capture["v"] - capture["u"] == 300_000
    call = spans["model:" + reader]
    assert events[call["a"]]["l"] == "luna"
    assert events[call["b"]]["error"]
    assert all(a in events and b in events and a != b for a, b, _ in data["edges"])
    assert [
        spans["job:j1"]["a"],
        trace.keys["message-delivered:answer"]["seq"],
        "delivered message",
    ] in data["edges"]
    assert not any(span["id"].startswith("display:") for span in data["spans"])
    assert all(
        span["k"] == "task" for span in data["spans"] if span["category"] in ("Turn", "Runner")
    )
    for event in events.values():
        record = json.loads((output / event["record"]).read_text())[str(event["n"])]
        if event.get("derived"):
            assert record["record_type"] == "derived timer boundary"
        else:
            assert record["seq"] == event["n"]
        for _, file in event["details"]:
            assert (output / file).is_file()
    text = "".join(file.read_text() for file in (output / "v41-details").glob("*.json"))
    assert "private fixture" not in text and "fixture-key" not in text
    assert "Visible answer" in text
    assert "alert(1)" not in (output / "v41.html").read_text()
    with pytest.raises(ValueError, match="outside the original"):
        build(trace, workspace / "viewer")


def test_viewer_epoch_round_trip_is_exact_at_microsecond_boundaries(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from build_profiler_site import iso

    for value in (1795666525723571, 1795672612934696, 1795672703476259):
        assert profiler.micros(iso(value)) == value
