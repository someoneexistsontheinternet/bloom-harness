import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from bloom.execution import Processes
from bloom.runtime import Runtime
from bloom.storage import Archive, BLOCK, ColdStorageUnavailable
from bloom.store import now
from test_runtime_delivery import message, saved


@pytest.fixture
def archive(store, monkeypatch):
    mount = store.config.root / "test-volume"
    cold = mount / "cold"
    cold.mkdir(parents=True)
    (cold / ".volume-id").write_text("test-volume-id")
    store.config.data["storage"].update(
        enabled=True, cold_root=str(cold), volume_mount=str(mount), volume_id="test-volume-id"
    )
    archive = Archive(store)
    online = {"value": True}
    monkeypatch.setattr(archive, "volume_ready", lambda: online["value"])
    monkeypatch.setattr(archive, "open_file", lambda path: False)
    archive.online = online
    return archive


def aged(path, data, days=8):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_cold_copy_cutoff_and_offline_search_across_distant_chunks(store, archive):
    data = (
        "firstkeyword\n" + "ordinary log content\n" * (BLOCK // 8) + "lastkeyword 中文检索\n"
    ).encode()
    old = aged(store.config.root / "logs/task/old.log", data)
    recent = aged(store.config.root / "logs/task/recent.log", b"recent-only", 6)
    result = archive.sweep()
    assert result["status"] == "completed" and result["files"] == 1
    assert not old.exists() and recent.exists()
    row = archive.record("logs/task/old.log")
    assert archive.object_path(row["sha256"]).read_bytes() == data
    assert row["sha256"] == hashlib.sha256(data).hexdigest()
    archive.online["value"] = False
    found = archive.search("firstkeyword lastkeyword")
    assert len(found) == 1 and found[0]["path"] == "logs/task/old.log"
    assert archive.search("中文检索")
    with pytest.raises(ColdStorageUnavailable):
        archive.read("logs/task/old.log")
    archive.online["value"] = True
    archive.restore(row["id"])
    assert old.read_bytes() == data
    with pytest.raises(FileExistsError):
        archive.restore(row["id"])


def test_disconnection_keeps_hot_source_and_retries_without_duplicate_index(
    store, archive, monkeypatch
):
    path = aged(store.config.root / "logs/task/log.txt", b"disconnect evidence\n")
    original = archive.copy

    def disconnect(source):
        value = original(source)
        archive.online["value"] = False
        raise ColdStorageUnavailable("Simulated cable disconnection after durable copy")

    monkeypatch.setattr(archive, "copy", disconnect)
    assert archive.sweep()["status"] == "waiting_for_volume"
    assert path.read_bytes() == b"disconnect evidence\n"
    archive.online["value"] = True
    monkeypatch.setattr(archive, "copy", original)
    assert archive.sweep()["files"] == 1
    assert len(archive.search("disconnect")) == 1
    assert archive.sweep()["files"] == 0


def test_copy_corruption_never_removes_source(store, archive):
    data = b"irreplaceable source\n"
    path = aged(store.config.root / "logs/task/log.txt", data)
    destination = archive.object_path(hashlib.sha256(data).hexdigest())
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"corrupted prior object")
    result = archive.sweep()
    assert result["status"] == "completed_with_errors"
    assert path.read_bytes() == data and not archive.search("irreplaceable")


def test_indexed_receipt_survives_interrupted_unlink(store, archive, monkeypatch):
    path = aged(store.config.root / "logs/task/log.txt", b"committed searchable receipt\n")
    unlink = Path.unlink

    def fail(selected, *args, **kwargs):
        if selected == path:
            raise OSError("Unlink interrupted")
        return unlink(selected, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        assert archive.sweep()["status"] == "completed_with_errors"
    assert path.exists() and archive.search("searchable")
    assert archive.sweep()["files"] == 1
    assert store.one("SELECT count(*) AS n FROM archive_files")["n"] == 1
    assert len(archive.search("searchable")) == 1


def test_source_change_retains_new_data_and_indexes_verified_old_version(
    store, archive, monkeypatch
):
    path = aged(store.config.root / "logs/task/log.txt", b"originalkeyword\n")
    index = archive.index

    def changed(source, digest, before):
        source.write_bytes(b"newkeyword\n")
        return index(source, digest, before)

    monkeypatch.setattr(archive, "index", changed)
    assert archive.sweep()["files"] == 0
    assert path.read_bytes() == b"newkeyword\n"
    assert archive.search("originalkeyword") and not archive.search("newkeyword")


def test_current_context_unfinished_tree_and_recent_access_stay_local(store, archive):
    task = store.new_task("Current owner context")
    item = store.one("SELECT envelope_hash FROM items WHERE task_id=?", (task,))["envelope_hash"]
    original = store.put({"old provider payload": "still needed by unfinished task"})
    store.db.execute(
        "INSERT INTO model_calls(id,task_id,agent_id,purpose,model_requested,provider,status,request_hash,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("pending", task, "astra", "turn", "fixture", "fixture", "uncertain", original, now()),
    )
    accessed = store.put({"recently inspected": True})
    for digest in (item, original, accessed):
        path = store.artifact_path(digest)
        os.utime(path, (time.time() - 8 * 86400,) * 2)
    store.db.execute("DELETE FROM artifact_access WHERE hash IN (?,?)", (item, original))
    assert archive.sweep()["files"] == 0
    assert all(store.artifact_path(h).exists() for h in (item, original, accessed))


def test_cold_artifact_reads_promote_exact_original_to_local_cache(store, archive, monkeypatch):
    value = {"archived original": "exact source data"}
    digest = store.put(value)
    path = store.artifact_path(digest)
    os.utime(path, (time.time() - 8 * 86400,) * 2)
    store.db.execute("DELETE FROM artifact_access")
    assert archive.sweep()["files"] == 1 and not path.exists()
    monkeypatch.setattr(Archive, "volume_ready", lambda self: archive.online["value"])
    archive.online["value"] = False
    with pytest.raises(ColdStorageUnavailable):
        store.get(digest)
    archive.online["value"] = True
    assert store.get(digest) == value and path.exists()
    archive.online["value"] = False
    assert store.get(digest) == value


async def test_offline_runtime_reuses_saved_answer_and_ignores_cold_volume(
    store, archive, monkeypatch
):
    aged(store.config.root / "logs/old/log.txt", b"historical background evidence")
    assert archive.sweep()["files"] == 1
    archive.online["value"] = False
    task = store.new_task("Continue with no RAID")
    saved(store, task, [message("Local runtime completed")])
    runtime = Runtime(store)

    async def forbidden(*args, **kwargs):
        raise AssertionError("No new model request is needed")

    runtime.models.infer = forbidden

    def volume_must_not_be_touched(self):
        raise AssertionError("Active runtime must not touch cold storage")

    monkeypatch.setattr(Archive, "volume_ready", volume_must_not_be_touched)
    await runtime.run(until_idle=True)
    assert store.one("SELECT status FROM tasks")["status"] == "completed"
    assert archive.search("historical")


async def test_archived_process_output_keeps_exit_status_while_offline(store, archive, monkeypatch):
    directory = store.config.workspace / "processes/old"
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps({"exit_code": 0}))
    aged(directory / "output.log", b"old process evidence\n")
    store.db.execute(
        "INSERT INTO processes(id,agent_id,target,cwd,command_hash,status,exit_code,directory,created_at) VALUES('p','astra','local',?,?,'completed',0,?,?)",
        (str(store.config.root), store.put("fixture"), str(directory), now()),
    )
    assert archive.sweep()["files"] == 1
    monkeypatch.setattr(Archive, "volume_ready", lambda self: archive.online["value"])
    archive.online["value"] = False
    result = await Processes(store).observe("astra", "p")
    assert result["result"]["exit_code"] == 0 and result["output_available"] is False
    archive.online["value"] = True
    result = await Processes(store).observe("astra", "p")
    assert result["output"] == "old process evidence\n"


def test_unmounted_or_wrong_volume_is_not_recreated(store, archive, monkeypatch):
    aged(store.config.root / "logs/old/file.txt", b"retain me")
    monkeypatch.delattr(archive, "volume_ready")
    assert archive.sweep()["status"] == "waiting_for_volume"
    monkeypatch.setattr("os.path.ismount", lambda path: True)
    (archive.cold_root() / ".volume-id").write_text("different-volume")
    assert archive.sweep()["status"] == "waiting_for_volume"
    assert (store.config.root / "logs/old/file.txt").exists()


def test_open_files_and_executable_releases_are_not_offloaded(store, archive, monkeypatch):
    path = aged(store.config.root / "logs/active/output.log", b"active but idle")
    release = aged(store.config.artifacts / "releases/active/src/main.py", b"pass\n")
    monkeypatch.delattr(archive, "open_file")
    with path.open("ab"):
        assert archive.sweep()["files"] == 0
    assert path.exists() and release.exists()


def test_restore_selects_exact_historical_version(store, archive):
    path = aged(store.config.root / "logs/old/file.txt", b"version one")
    archive.sweep()
    first = archive.record("logs/old/file.txt")
    aged(path, b"version two")
    archive.sweep()
    archive.restore(first["id"])
    assert path.read_bytes() == b"version one"


def test_interrupted_search_index_rebuilds_without_partial_matches(store, archive, monkeypatch):
    path = aged(store.config.root / "logs/old/file.txt", b"completely indexed evidence\n")
    chunks = archive.chunks

    def interrupted(source):
        yield "partial-only evidence"
        raise OSError("Index reader interrupted")

    monkeypatch.setattr(archive, "chunks", interrupted)
    assert archive.sweep()["status"] == "completed_with_errors"
    assert path.exists() and not archive.search("partial")
    monkeypatch.setattr(archive, "chunks", chunks)
    assert archive.sweep()["files"] == 1
    assert archive.search("completely") and not archive.search("partial")


def test_search_cache_redacts_registered_credentials_across_chunk_boundary(store, archive):
    from bloom.privacy import register

    secret = "synthetic-archive-credential-1234567890"
    register(secret)
    raw = b"prefix " * (BLOCK // 7) + secret.encode() + b" safe-keyword\n"
    path = aged(store.config.root / "logs/old/file.txt", raw)
    assert archive.sweep()["files"] == 1
    import zlib

    for row in store.rows("SELECT text_zlib FROM archive_chunks"):
        assert secret not in zlib.decompress(row["text_zlib"]).decode()
    assert archive.search("safe")


def test_call_export_retains_cold_pointers_when_volume_is_offline(store, archive, monkeypatch):
    from bloom.operations import export_calls

    digest = store.put({"payload": "historical provider response"})
    store.db.execute(
        "INSERT INTO model_calls(id,agent_id,purpose,model_requested,provider,status,request_hash,response_hash,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("call_cold", "astra", "test", "fixture", "fixture", "completed", digest, digest, now()),
    )
    path = store.artifact_path(digest)
    os.utime(path, (time.time() - 8 * 86400,) * 2)
    store.db.execute("DELETE FROM artifact_access")
    assert archive.sweep()["files"] == 1
    monkeypatch.setattr(Archive, "volume_ready", lambda self: False)
    assert export_calls(store)["originals_retained"]
    receipt = next((store.config.artifacts / "runtime/calls/main").rglob("receipt.json"))
    data = json.loads(receipt.read_text())
    assert (
        data["request_hash"] == digest and data["files"]["request.json"]["availability"] == "cold"
    )
    assert not (receipt.parent / "request.json").exists()


def test_mount_path_disappearing_mid_copy_cannot_create_ssd_fallback(store, archive, monkeypatch):
    import contextlib

    path = aged(store.config.root / "logs/old/file.txt", b"anchored device copy")
    mount = Path(archive.settings["volume_mount"])
    detached = mount.with_name("detached-original-device")
    folder = archive.folder
    switched = False

    @contextlib.contextmanager
    def disappear(parent, name, create=False):
        nonlocal switched
        if not switched:
            switched = True
            mount.rename(detached)
            mount.mkdir()  # Simulate an empty SSD mount-point shadow.
        with folder(parent, name, create) as descriptor:
            yield descriptor

    monkeypatch.setattr(archive, "folder", disappear)
    digest, _ = archive.copy(path)
    assert not list(mount.iterdir())
    assert (detached / "cold/objects" / digest[:2] / digest).read_bytes() == b"anchored device copy"
    assert path.exists()


def test_removable_io_never_holds_live_database_write_transaction(store, archive, monkeypatch):
    path = aged(store.config.root / "logs/old/file.txt", b"no removable IO in writer transaction")

    def online():
        assert not store.db.in_transaction
        return True

    monkeypatch.setattr(archive, "volume_ready", online)
    assert archive.sweep()["files"] == 1 and not path.exists()
