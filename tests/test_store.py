"""Failure boundaries for file/index commits and inbox consumption."""

import json

import pytest

from bloom4.mailbox import Mailbox
from bloom4.store import Store


def test_rebuild_preserves_originals_search_and_idempotency(tmp_path):
    store = Store(tmp_path)
    first = store.append("observed", {"text": "restart evidence"}, dedup="one")
    assert store.append("observed", {"text": "restart evidence"}, dedup="one") == first
    with pytest.raises(ValueError, match="Idempotency"):
        store.append("observed", {"text": "changed"}, dedup="one")
    store.close()
    (tmp_path / "index.sqlite3").unlink()
    recovered = Store(tmp_path)
    assert recovered.search("restart")[0] == first
    assert recovered.append("second", {})["seq"] == first["seq"] + 1


def test_file_committed_before_index_recovers(tmp_path, monkeypatch):
    store = Store(tmp_path)
    original = store._index
    monkeypatch.setattr(store, "_index", lambda *a: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        store.append("committed", {})
    monkeypatch.setattr(store, "_index", original)
    store.sync()
    assert len(store.events("committed")) == 1


def test_mailbox_receipt_is_not_consumption_and_restart_deduplicates(tmp_path):
    store = Store(tmp_path)
    box = Mailbox(store)
    box.post("hello", request_id="request1")
    first = box.ingest()[0]
    assert box.pending("astra") == [first]
    box.post("hello", request_id="request1")
    assert box.ingest() == [first]
    with pytest.raises(ValueError):
        box.consume("sol", [first["id"]])
    box.consume("astra", [first["id"]])
    assert not box.pending("astra")
    box.post("different", request_id="request1")
    assert box.ingest() == []
    assert len(store.events("input.rejected")) == 1


def test_tampered_original_refused(tmp_path):
    store = Store(tmp_path)
    ev = store.append("original", {})
    path = next((tmp_path / "events").glob("*/*.json"))
    data = json.loads(path.read_text()); data["data"] = {"fake": True}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="checksum"):
        store.get(ev["id"])
    with pytest.raises(ValueError, match="checksum"):
        store.sync(rebuild=True)
