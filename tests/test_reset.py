from test_memory import approve_fixture
import json
import os
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest

from bloom import reset, service
from bloom.context import Context
from bloom.gateway import Gateway
from bloom.memory import Memory
from bloom.runtime import Runtime
from bloom.storage import Archive, ColdStorageUnavailable
from bloom.store import Store, atomic, encoded, now
from test_memory import Curator, fact
from test_storage import archive, aged


async def populated(store):
    task = store.new_task("Remember the exact reset-test orchid marker.")
    await approve_fixture(Memory(store, Curator(store, lambda data: [fact(data)])), task=task)
    Context(store, None).memory(
        "global", 0, "Previous learned preference", "Original memory detail", "test", []
    )
    store.status(task, "completed")
    atomic(
        store.config.workspace / "requests/old.toml",
        b'operation="message"\ntext="Do not replay me"',
    )
    atomic(store.config.workspace / "authoring/astra/old.toml", b"old control")
    atomic(store.config.artifacts / "runtime/calls/index.json", b'{"old":true}')
    atomic(store.config.workspace / "web/native/helper-fixture", b"keep permissions and helper")
    return task


async def test_reset_empty_context_restore_exact_history_and_archive_search(
    store, archive, monkeypatch
):
    config = store.config
    root = config.root
    await populated(store)
    archived = aged(root / "logs/archive-test/old.txt", b"orchid searchable while disconnected")
    assert archive.sweep()["files"] == 1 and not archived.exists()
    before = store.rows("SELECT * FROM items")
    original = {r["hash"]: store.bytes(r["hash"]) for r in store.rows("SELECT hash FROM artifacts")}
    catalog = store.rows("SELECT * FROM archive_files")
    store.close()
    result = reset.apply(config)
    assert result["phase"] == "completed"
    assert reset.verify_bundle(Path(result["backup"]["path"]))["verified"]
    assert not (config.workspace / "reset-pending.json").exists()
    assert not (root / "memories/global.md").exists()
    assert not (config.workspace / "requests").exists()
    assert not (config.workspace / "authoring").exists()
    assert not (config.artifacts / "runtime/calls").exists()
    assert (
        config.workspace / "web/native/helper-fixture"
    ).read_bytes() == b"keep permissions and helper"
    fresh = Store(config)
    try:
        assert all(result["after"][name] == 0 for name in reset.EMPTY)
        assert fresh.rows("SELECT * FROM archive_files") == catalog
        monkeypatch.setattr(Archive, "volume_ready", lambda self: False)
        assert Archive(fresh).search("orchid searchable")
        task = fresh.new_task("A brand new question")
        context = Context(fresh, None)
        instructions, items = await context.compose("astra", task, [])
        assert "Previous learned preference" not in instructions
        assert "reset-test orchid" not in json.dumps(items)
        assert not fresh.rows("SELECT * FROM memory_facts")
    finally:
        fresh.close()
    # A real extracted snapshot reopens and resolves every original envelope.
    recovered = root / "recovery-check"
    recovered.mkdir()
    with zipfile.ZipFile(result["backup"]["path"]) as bundle:
        bundle.extractall(recovered)
    with reset.reader(config, recovered / "workspace/bloom.sqlite3") as old:
        assert old.rows("SELECT * FROM items") == before
    for value, raw in original.items():
        assert (recovered / "artifacts/runtime/objects" / value).read_bytes() == raw


async def test_missing_cold_original_aborts_before_mutation_then_resumes(
    store, archive, monkeypatch
):
    config = store.config
    task = store.new_task("Retain me")
    store.status(task, "completed")
    value = store.put({"orphan": "cold original payload"})
    path = store.artifact_path(value)
    os.utime(path, (time.time() - 8 * 86400,) * 2)
    store.db.execute("DELETE FROM artifact_access WHERE hash=?", (value,))
    assert archive.sweep()["files"] == 1 and not path.exists()
    before = store.rows("SELECT * FROM items")
    store.close()
    monkeypatch.setattr(Archive, "volume_ready", lambda self: False)
    with pytest.raises(ColdStorageUnavailable):
        reset.apply(config)
    pending = json.loads((config.workspace / "reset-pending.json").read_text())
    assert pending["phase"] == "preparing"
    with reset.reader(config) as old:
        assert old.rows("SELECT * FROM items") == before
    with pytest.raises(RuntimeError, match="reset is incomplete"):
        Store(config)
    monkeypatch.setattr(Archive, "volume_ready", lambda self: True)
    result = reset.apply(config)
    assert result["id"] == pending["id"]
    with zipfile.ZipFile(result["backup"]["path"]) as bundle:
        assert (
            json.loads(bundle.read("artifacts/runtime/objects/" + value))["orphan"]
            == "cold original payload"
        )


@pytest.mark.parametrize("boundary", ["after_retire", "after_install"])
async def test_interrupted_publication_resumes_same_backup_without_replay(
    store, monkeypatch, boundary
):
    config = store.config
    await populated(store)
    store.close()
    replace = reset.os.replace

    def crash(source, target):
        replace(source, target)
        retired = "retired" in Path(target).parts and Path(source).name == "bloom.sqlite3"
        installed = Path(source).name == "database-fresh.sqlite3"
        if (boundary == "after_retire" and retired) or (boundary == "after_install" and installed):
            raise OSError("simulated power loss")

    with monkeypatch.context() as patch:
        patch.setattr(reset.os, "replace", crash)
        with pytest.raises(OSError, match="power loss"):
            reset.apply(config)
    state = json.loads((config.workspace / "reset-pending.json").read_text())
    with pytest.raises(RuntimeError, match="reset is incomplete"):
        Store(config)
    with pytest.raises(RuntimeError, match="reset is incomplete"):
        service.launch(config.root, {})
    result = reset.apply(config)
    assert result["id"] == state["id"] and result["backup"]["sha256"] == state["backup"]["sha256"]
    assert result["after"]["items"] == 0


def test_reset_refuses_open_store_and_unfinished_effects(store):
    with pytest.raises(RuntimeError, match="exclusive workspace/session.lock"):
        reset.apply(store.config)
    task = store.new_task("unfinished work")
    config = store.config
    store.close()
    with pytest.raises(RuntimeError, match="unfinished/uncertain"):
        reset.apply(config)
    assert not (config.workspace / "reset-pending.json").exists()
    with reset.reader(config) as old:
        assert old.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "ready"


async def test_explicit_reset_preserves_terminal_failures_and_worktree_files_instead_of_faking_success(
    store,
):
    task = store.new_task("A retained failed attempt")
    with pytest.raises(ValueError, match="does not match"):
        await approve_fixture(
            Memory(
                store,
                Curator(store, lambda data: [fact(data, quote="Invented evidence that is absent")]),
            ),
            task=task,
        )
    store.status(task, "blocked", "Original validation failure")
    worktree = store.config.root / "worktrees/retained"
    worktree.mkdir(parents=True)
    (worktree / "work.txt").write_text("Preserve this uncommitted work exactly.")
    store.db.execute(
        "INSERT INTO worktrees VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "retained",
            task,
            str(store.config.root),
            str(worktree),
            "retained-branch",
            "base",
            "candidate",
            "submitted",
            now(),
        ),
    )
    batches = store.rows("SELECT * FROM memory_batches")
    tasks = store.rows("SELECT * FROM tasks")
    config = store.config
    store.close()
    assert not reset.inspect(config)["ready"]
    plan = reset.inspect(config, allow_terminal_records=True)
    assert plan["ready"] and plan["retained_terminal_records"]["worktrees"][0]["path"] == str(
        worktree
    )
    result = reset.apply(config, allow_terminal_records=True)
    assert result["phase"] == "completed" and result["after"]["tasks"] == 0
    assert (worktree / "work.txt").read_text() == "Preserve this uncommitted work exactly."
    recovered = config.root / "original.sqlite3"
    with zipfile.ZipFile(result["backup"]["path"]) as bundle:
        recovered.write_bytes(bundle.read("workspace/bloom.sqlite3"))
    with reset.reader(config, recovered) as old:
        assert old.rows("SELECT * FROM tasks") == tasks
        assert old.rows("SELECT * FROM memory_batches") == batches
        assert old.one("SELECT path FROM worktrees")["path"] == str(worktree)


@pytest.mark.parametrize(
    "kind", ["active_task", "uncertain_effect", "uncertain_inference", "pending_delivery"]
)
def test_terminal_reset_option_does_not_bypass_live_or_uncertain_work(store, kind):
    task = store.new_task("Do not lose live effects")
    store.status(task, "blocked")
    digest = store.put({})
    if kind == "active_task":
        store.status(task, "running")
    elif kind == "uncertain_effect":
        store.db.execute(
            "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
            ("uncertain", task, "tool", "write_file", "uncertain", digest, now()),
        )
    elif kind == "uncertain_inference":
        store.db.execute(
            "INSERT INTO model_calls(id,agent_id,task_id,purpose,model_requested,provider,status,request_hash,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("unknown", "astra", task, "turn", "fixture", "fixture", "uncertain", digest, now()),
        )
    else:
        Runtime(store).delivery.queue(None, task, "Pending delivery")
    config = store.config
    store.close()
    assert not reset.inspect(config, allow_terminal_records=True)["ready"]
    with pytest.raises(RuntimeError, match="unfinished/uncertain"):
        reset.apply(config, allow_terminal_records=True)
    assert not (config.workspace / "reset-pending.json").exists()


async def test_reset_refuses_symlinks_and_detects_changed_backup(store):
    config = store.config
    task = store.new_task("keep")
    store.status(task, "completed")
    (config.root / "memories/redirect").symlink_to(config.root / "config")
    store.close()
    with pytest.raises(ValueError, match="symbolic paths"):
        reset.apply(config)
    (config.root / "memories/redirect").unlink()
    result = reset.apply(config)
    with zipfile.ZipFile(result["backup"]["path"], "a") as bundle:
        bundle.writestr("unrecorded.txt", b"corruption")
    with pytest.raises(RuntimeError, match="member set"):
        reset.verify_bundle(Path(result["backup"]["path"]))


async def test_reset_discord_cutoff_keeps_downtime_input_and_ignores_old_history(store):
    config = store.config
    config.data["discord"]["enabled"] = True
    task = store.new_task("old owner request")
    store.status(task, "completed")
    store.close()
    result = reset.apply(config)
    fresh = Store(config)
    try:
        runtime = Runtime(fresh)
        address = config.data["discord"]["channel_id"]
        cutoff = int(fresh.one("SELECT message_id FROM discord_cursors")["message_id"])
        records = [
            {
                "id": str(value),
                "channel_id": address,
                "content": "message " + str(value),
                "author": {"id": config.data["discord"]["owner_id"]},
            }
            for value in (cutoff - 1, cutoff + 1)
        ]

        async def history(*args, **kwargs):
            return list(reversed(records))

        runtime.delivery.discord.request = history
        gateway = Gateway(fresh, runtime.delivery.discord, runtime.receive_discord, catch_up=True)
        await gateway.reconcile_history()
        await gateway.reconcile_history()
        assert [
            r["external_id"] for r in fresh.rows("SELECT external_id FROM inbound_messages")
        ] == [str(cutoff + 1)]
        assert fresh.one("SELECT count(*) AS n FROM tasks")["n"] == 1
        assert not gateway.state()  # No previous gateway session to resume.
    finally:
        fresh.close()


async def test_service_reset_restarts_candidate_and_matching_archive(store, monkeypatch):
    config = store.config
    root = config.root
    await populated(store)
    store.close()
    release = root / "artifacts/releases/test-release"
    release.mkdir(parents=True)
    (release / "test.txt").write_text("frozen source")
    plan = {
        "root": str(root),
        "commit": "test-release",
        "source": str(release),
        "manifest": service.source_manifest(release),
    }
    atomic(root / "workspace/service/candidate.json", encoded(plan))
    calls = []
    monkeypatch.setattr(service, "installed_path", lambda root, label=None: root / "unused.plist")
    monkeypatch.setattr(service, "unload", lambda root, label=None: calls.append(("stop", label)))
    monkeypatch.setattr(reset, "ensure_stopped", lambda config: None)
    monkeypatch.setattr(
        service,
        "launch",
        lambda root, plan: calls.append(("start", plan["commit"])) or {"healthy": True},
    )
    # Config is read by the controller; opt into storage without copying secrets.
    (root / "config/local.toml").write_text("[storage]\nenabled=true\n")
    monkeypatch.setattr(
        "bloom.storage.install",
        lambda store, plan=None: (
            calls.append(("archive", plan["commit"])) or {"source_commit": plan["commit"]}
        ),
    )
    monkeypatch.setattr("sys.argv", ["service", "reset", "--root", str(root)])
    service.main()
    outcome = json.loads((root / "workspace/service/last-reset.json").read_text())
    assert outcome["reset"]["after"]["items"] == 0 and outcome["health"]["healthy"]
    assert calls == [
        ("stop", service.LABEL + ".archive"),
        ("stop", None),
        ("start", "test-release"),
        ("archive", "test-release"),
    ]
