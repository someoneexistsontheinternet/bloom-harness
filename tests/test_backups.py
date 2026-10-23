import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from bloom.backups import Backups
from bloom.reset import verify_bundle
from bloom.store import Store
from test_storage import archive, aged


def test_weekly_backup_has_consistent_sqlite_and_every_original_and_is_not_repeated(
    store, archive, monkeypatch
):
    task = store.new_task("Retain this exact conversation")
    raw = store.put({"tool": "evidence", "text": "exact result"})
    original = store.rows("SELECT hash,bytes FROM artifacts")
    backup = Backups(archive).run_if_due()
    assert backup["status"] == "completed"
    assert verify_bundle(Path(backup["path"]))["original_artifacts"] == len(original)
    with zipfile.ZipFile(backup["path"]) as bundle:
        assert json.loads(bundle.read("artifacts/runtime/objects/" + raw))["text"] == "exact result"
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["counts"]["tasks"] == 1 and len(manifest["files"]) == len(original) + 1
    assert Backups(archive).run_if_due()["status"] == "not_due"
    assert store.one("SELECT count(*) n FROM conversation_backups")["n"] == 1
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "ready"
    # Ordinary seven-day archival can move the verified package; catalog stays.
    path = Path(backup["path"])
    data = path.read_bytes()
    aged(path, data)
    assert archive.sweep()["files"] == 1
    assert not path.exists() and archive.read(str(path.relative_to(store.config.root))) == data
    assert store.one("SELECT status FROM conversation_backups")["status"] == "completed"


def test_missing_cold_drive_never_creates_a_shadow_or_a_false_backup(store, archive):
    archive.online["value"] = False
    assert Backups(archive).run_if_due()["status"] == "waiting_for_volume"
    assert not (store.config.artifacts / "backups").exists()
    assert not store.rows("SELECT * FROM conversation_backups")


def test_corrupt_backup_source_cannot_publish_or_remove_originals(store, archive, monkeypatch):
    raw = store.put({"must": "remain"})
    original = store.artifact_path(raw).read_bytes()
    read = Store.bytes
    monkeypatch.setattr(
        Store,
        "bytes",
        lambda self, digest, **kw: b"corrupt" if digest == raw else read(self, digest, **kw),
    )
    result = Backups(archive).run_if_due()
    assert result["status"] == "failed"
    assert store.artifact_path(raw).read_bytes() == original
    assert not list((store.config.artifacts / "backups").glob("*/conversation.zip"))


def test_backup_crash_after_rename_reuses_verified_bundle(store, archive, monkeypatch):
    store.new_task("Keep the snapshot after interrupted publication")
    backup = Backups(archive)
    with monkeypatch.context() as patch:
        patch.setattr(backup, "complete", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            backup.run_if_due()
    row = store.one("SELECT * FROM conversation_backups")
    assert row["status"] == "creating" and Path(row["path"]).is_file()
    assert backup.run_if_due()["status"] == "not_due"
    assert store.one("SELECT count(*) n FROM conversation_backups")["n"] == 1
    assert store.one("SELECT status FROM conversation_backups")["status"] == "completed"


def test_snapshot_does_not_mix_concurrent_new_artifacts_into_original_list(
    store, archive, monkeypatch
):
    store.new_task("Before snapshot")
    expected = store.one("SELECT count(*) n FROM artifacts")["n"]
    original = Store.bytes
    added = []

    def concurrent(self, digest, **kwargs):
        if not added:
            added.append(store.put({"concurrent": "Created after SQLite snapshot"}))
        return original(self, digest, **kwargs)

    monkeypatch.setattr(Store, "bytes", concurrent)
    result = Backups(archive).run_if_due()
    assert result["status"] == "completed" and result["artifact_count"] == expected
    assert store.artifact_path(added[0]).exists()
    with zipfile.ZipFile(result["path"]) as bundle:
        assert "artifacts/runtime/objects/" + added[0] not in bundle.namelist()
