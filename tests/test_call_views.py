import asyncio
import hashlib
import json
import os
import time

import pytest

from bloom.call_views import export_calls
from bloom.runtime import Runtime
from bloom.storage import Archive
from bloom.store import encoded, now
from test_storage import archive, aged


def call(store, ident, purpose="turn", task=None, response=True, status="completed"):
    request = store.put({"input": [{"role": "user", "content": ident}], "tools": []})
    result = (
        store.put({"output": [{"role": "assistant", "phase": "final_answer", "content": ident}]})
        if response
        else None
    )
    store.db.execute(
        "INSERT INTO model_calls(id,task_id,agent_id,purpose,model_requested,provider,status,request_hash,response_hash,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ident, task, "astra", purpose, "fixture-model", "fixture", status, request, result, now()),
    )
    return request, result


def index(store):
    return json.loads((store.config.artifacts / "runtime/calls/index.json").read_text())


def receipt_path(store, row):
    return store.config.artifacts / "runtime/calls" / row["path"] / "receipt.json"


def test_separate_pools_named_calls_native_phases_and_verified_pruning(store):
    store.config.data["runtime"].update(recent_main_calls=2, recent_worker_calls=1)
    t1 = store.new_task("EROC research")
    t2 = store.new_task("EROC research")
    originals = [
        call(store, "call_main_1", task=t1),
        call(store, "call_main_2", task=t2),
        call(store, "call_helper_1", "curation", t1),
    ]
    assert export_calls(store)["pools"] == {"main": 2, "workers": 1}
    rows = index(store)["calls"]
    assert len({r["path"] for r in rows}) == 3
    assert all("eroc-research--" in r["path"] for r in rows)
    for row in rows:
        path = receipt_path(store, row)
        data = json.loads(path.read_text())
        request = json.loads((path.parent / "request.json").read_text())
        response = json.loads((path.parent / "response.json").read_text())
        assert hashlib.sha256(encoded(request)).hexdigest() == data["request_hash"]
        assert response["output"][0]["phase"] == "final_answer"
        assert data["input_tokens"] is None
    call(store, "call_main_3")
    call(store, "call_helper_2", "search")
    assert export_calls(store)["pools"] == {"main": 2, "workers": 1}
    assert len(list((store.config.artifacts / "runtime/calls").rglob("receipt.json"))) == 3
    assert store.one("SELECT count(*) AS n FROM model_calls")["n"] == 5
    assert all(store.get(h) for pair in originals for h in pair)


def test_failed_attempt_has_no_invented_response_and_retry_remains_separate(store):
    call(store, "call_failed", response=False, status="failed")
    call(store, "call_retry")
    export_calls(store)
    rows = {r["id"]: r for r in index(store)["calls"]}
    failed = receipt_path(store, rows["call_failed"])
    assert not (failed.parent / "response.json").exists()
    data = json.loads(failed.read_text())
    assert data["status"] == "failed" and data["response_hash"] is None
    assert receipt_path(store, rows["call_retry"]).exists()


def test_views_rebuild_after_edits_and_do_not_touch_or_cache_originals(store):
    hashes = call(store, "call_rebuild")
    access = store.rows("SELECT * FROM artifact_access ORDER BY hash")
    export_calls(store)
    receipt = receipt_path(store, index(store)["calls"][0])
    request = receipt.parent / "request.json"
    stat = request.stat()
    export_calls(store)
    assert request.stat().st_mtime_ns == stat.st_mtime_ns
    request.write_text("broken derived JSON")
    export_calls(store)
    assert json.loads(request.read_text()) == store.get(hashes[0])
    assert store.rows("SELECT * FROM artifact_access ORDER BY hash") == access
    receipt.unlink()
    request.unlink()
    export_calls(store)
    assert receipt.exists() and request.exists()


def test_offline_view_is_preserved_and_pruning_is_deferred(store, archive, monkeypatch):
    hashes = call(store, "call_cold")
    export_calls(store)
    old_receipt = receipt_path(store, index(store)["calls"][0])
    for digest in hashes:
        os.utime(store.artifact_path(digest), (time.time() - 8 * 86400,) * 2)
    store.db.execute("DELETE FROM artifact_access")
    assert archive.sweep()["files"] == 2
    monkeypatch.setattr(Archive, "volume_ready", lambda self: False)
    assert export_calls(store)["recent_calls"] == 1
    assert (old_receipt.parent / "request.json").exists()
    store.config.data["runtime"]["recent_main_calls"] = 1
    call(store, "call_new")
    result = export_calls(store)
    assert result["prune_deferred"][0]["id"] == "call_cold"
    assert old_receipt.exists()
    monkeypatch.setattr(Archive, "volume_ready", lambda self: True)
    assert export_calls(store)["prune_deferred"] == []
    assert not old_receipt.exists()
    assert all(not store.artifact_path(h).exists() for h in hashes)


def test_interrupted_projection_resumes_and_does_not_delete_unrelated_files(store, monkeypatch):
    import bloom.call_views as views

    call(store, "call_one")
    call(store, "call_two")
    render = views.render_call
    count = 0

    def fail(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("injected interruption")
        return render(*args)

    monkeypatch.setattr(views, "render_call", fail)
    with pytest.raises(OSError, match="interruption"):
        export_calls(store)
    monkeypatch.setattr(views, "render_call", render)
    assert export_calls(store)["recent_calls"] == 2
    row = next(r for r in index(store)["calls"] if r["id"] == "call_one")
    note = receipt_path(store, row).parent / "my-note.txt"
    note.write_text("keep")
    store.config.data["runtime"]["recent_main_calls"] = 1
    assert export_calls(store)["prune_deferred"][0]["id"] == "call_one"
    assert note.read_text() == "keep"


def test_legacy_projection_retired_only_with_durable_matching_record(store):
    call(store, "call_legacy")
    legacy = store.config.root / "logs/model-calls/main"
    legacy.mkdir(parents=True)
    row = store.one("SELECT * FROM model_calls WHERE id='call_legacy'")
    (legacy / "call_legacy.json").write_text(json.dumps(row))
    (legacy / "call_unknown.json").write_text('{"id":"call_unknown"}')
    assert export_calls(store)["legacy_views_removed"] == 1
    assert not (legacy / "call_legacy.json").exists()
    assert (legacy / "call_unknown.json").exists()


def test_archive_skips_recent_projection_even_when_aged(store, archive):
    path = aged(store.config.artifacts / "runtime/calls/main/receipt.json", b"view")
    assert archive.sweep()["files"] == 0
    assert path.exists()


async def test_running_coordinator_refreshes_views_without_shutdown(store):
    call(store, "call_live_view")
    runtime = Runtime(store)
    task = asyncio.create_task(runtime.run())
    try:
        for _ in range(100):
            await asyncio.sleep(0.02)
            if (store.config.artifacts / "runtime/calls/index.json").exists():
                break
        assert not task.done()
        assert index(store)["calls"][0]["id"] == "call_live_view"
    finally:
        runtime.stopping = True
        await asyncio.wait_for(task, 5)
