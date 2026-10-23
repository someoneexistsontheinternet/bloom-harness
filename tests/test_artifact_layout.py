import hashlib
import os
import time

import pytest

from bloom.storage import Archive
from bloom.store import Store
from bloom.artifacts import migrate_objects
from test_storage import archive, aged


def test_fresh_objects_are_flat_and_deduplicated(store):
    digest = store.put({"immutable": "original"})
    assert store.put({"immutable": "original"}) == digest
    assert store.artifact_path(digest) == store.config.artifacts / "runtime/objects" / digest
    assert len(list((store.config.artifacts / "runtime/objects").iterdir())) == 1
    assert not (store.config.artifacts / digest[:2]).exists()


def test_existing_store_retains_legacy_writes_until_migrated(store):
    digest = store.put({"original": "legacy"})
    current, old = store.artifact_paths(digest)
    old.parent.mkdir()
    current.rename(old)
    store.db.execute("DELETE FROM artifact_layout")
    another = Store(store.config)
    try:
        assert another.one("SELECT version FROM artifact_layout")["version"] == 1
        assert another.get(digest) == {"original": "legacy"}
        new = another.put({"bridge": "legacy writes remain rollback compatible"})
        assert another.artifact_paths(new)[1].exists()
        another.db.execute("UPDATE artifact_layout SET version=2")
        assert another.get(digest) == {"original": "legacy"}
        assert another.artifact_path(new) == another.artifact_paths(new)[0]
    finally:
        another.close()


def test_corrupt_new_copy_does_not_silently_fall_back(store):
    digest = store.put({"payload": "unchanged"})
    current, old = store.artifact_paths(digest)
    old.parent.mkdir()
    old.write_bytes(current.read_bytes())
    current.write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="integrity"):
        store.bytes(digest)


def test_legacy_archive_receipt_restores_to_current_layout(store, archive, monkeypatch):
    store.db.execute("UPDATE artifact_layout SET version=1")
    digest = store.put({"legacykeyword": "cold original"})
    old = store.artifact_path(digest)
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    store.db.execute("DELETE FROM artifact_access")
    assert archive.sweep()["files"] == 1
    row = archive.record(str(old.relative_to(store.config.root)))
    store.db.execute("UPDATE artifact_layout SET version=2")
    monkeypatch.setattr(Archive, "volume_ready", lambda self: archive.online["value"])
    assert store.bytes(digest, touch=False, cache=False)
    assert not store.artifact_path(digest).exists()
    assert store.one("SELECT * FROM artifact_access") is None
    archive.online["value"] = False
    assert archive.search("legacykeyword")[0]["path"] == row["path"]
    archive.online["value"] = True
    result = archive.restore(row["id"])
    assert result["restored"] == str(store.artifact_path(digest))
    assert hashlib.sha256(store.artifact_path(digest).read_bytes()).hexdigest() == digest
    assert not old.exists()
    assert archive.record(row["path"])["id"] == row["id"]


def test_legacy_cold_read_caches_into_new_layout(store, archive, monkeypatch):
    store.db.execute("UPDATE artifact_layout SET version=1")
    digest = store.put({"legacy read": "exact"})
    old = store.artifact_path(digest)
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    store.db.execute("DELETE FROM artifact_access")
    assert archive.sweep()["files"] == 1
    store.db.execute("UPDATE artifact_layout SET version=2")
    monkeypatch.setattr(Archive, "volume_ready", lambda self: True)
    assert store.get(digest) == {"legacy read": "exact"}
    assert store.artifact_path(digest).exists() and not old.exists()


def test_migration_resumes_after_interruption_and_keeps_originals(store, monkeypatch):
    store.db.execute("UPDATE artifact_layout SET version=1")
    hashes = [store.put({"original": i}) for i in range(4)]
    replace = os.replace
    moves = []

    def interrupt(source, target):
        if Path(target).parent == store.config.artifacts / "runtime/objects":
            if len(moves) == 2:
                raise OSError("simulated interrupted migration")
            moves.append(source)
        return replace(source, target)

    from pathlib import Path

    monkeypatch.setattr(os, "replace", interrupt)
    with pytest.raises(OSError, match="interrupted migration"):
        migrate_objects(store)
    assert store.one("SELECT version FROM artifact_layout")["version"] == 1
    assert [store.get(h) for h in hashes] == [{"original": i} for i in range(4)]
    monkeypatch.setattr(os, "replace", replace)
    assert migrate_objects(store)["status"] == "completed"
    assert store.one("SELECT version FROM artifact_layout")["version"] == 2
    assert all(store.artifact_paths(h)[0].exists() for h in hashes)
    assert not any(store.artifact_paths(h)[1].parent.exists() for h in hashes)
    assert migrate_objects(store)["moved_this_pass"] == 0


def test_migration_checks_all_objects_before_moving_any(store):
    store.db.execute("UPDATE artifact_layout SET version=1")
    good = store.put({"good": 1})
    bad = store.put({"bad": 2})
    store.artifact_path(bad).write_bytes(b"damaged")
    with pytest.raises(RuntimeError, match="integrity"):
        migrate_objects(store)
    assert store.artifact_paths(good)[1].exists()
    assert not store.artifact_paths(good)[0].exists()


def test_migration_does_not_fetch_cold_originals_or_rewrite_receipts(store, archive, monkeypatch):
    store.db.execute("UPDATE artifact_layout SET version=1")
    digest = store.put({"oldkeyword": "offline migration"})
    old = store.artifact_path(digest)
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    store.db.execute("DELETE FROM artifact_access")
    assert archive.sweep()["files"] == 1
    rows = store.rows("SELECT * FROM archive_files")

    def forbidden(self):
        raise AssertionError("Migration must use the existing local catalog")

    monkeypatch.setattr(Archive, "require_volume", forbidden)
    assert migrate_objects(store)["local_objects"] == 0
    assert store.rows("SELECT * FROM archive_files") == rows
    assert archive.search("oldkeyword")


def test_migration_refuses_running_coordinator_and_unknown_entries(store):
    with store.runtime_lock(), pytest.raises(RuntimeError, match="runtime owns"):
        migrate_objects(store)
    bucket = store.config.artifacts / "aa"
    bucket.mkdir()
    (bucket / "keep.txt").write_text("unrelated")
    with pytest.raises(ValueError, match="Unregistered"):
        migrate_objects(store)
    assert (bucket / "keep.txt").read_text() == "unrelated"
