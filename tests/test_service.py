import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from bloom.service import health, prepare, source_manifest
from bloom.workspaces import git
from test_runtime_delivery import message, saved


def test_release_without_astra_memory_approval_is_refused_before_unload(store, monkeypatch):
    from bloom import service

    legacy = store.config.artifacts / "releases/legacy-memory"
    legacy.mkdir(parents=True)
    plan = {"root": str(store.config.root), "source": str(legacy), "manifest": {}}

    def forbidden(*args, **kwargs):
        raise AssertionError("Do not stop a service for an incompatible release")

    monkeypatch.setattr(service, "unload", forbidden)
    with pytest.raises(ValueError, match="Astra memory approval"):
        service.launch(store.config.root, plan)


def test_frozen_service_restart_retains_records_and_checks_actual_pid(store, monkeypatch):
    # This lifecycle fixture has no command workload. Real dependency hashing
    # and sandbox execution are covered in test_toolchain_environment.
    monkeypatch.setattr("bloom.toolchain.verify", lambda root: {"fixture": True})
    source = Path(__file__).resolve().parents[1]
    root = store.config.root
    shutil.copytree(source / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (root / ".gitignore").write_text(".venv/\nworkspace/\nartifacts/\nlogs/\n__pycache__/\n")
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Bloom validation")
    git(root, "config", "user.email", "validation@localhost")
    git(root, "add", "src", "config", "prompts", ".gitignore")
    git(root, "commit", "-m", "Frozen service fixture")
    prepared = prepare(root)
    plan = json.loads((root / "workspace/service/candidate.json").read_text())
    assert plan["manifest"] == source_manifest(Path(plan["source"]))
    task = store.new_task("Restore the saved final without buying another call")
    saved(store, task, [message("Retained answer")])
    identities = []
    for _ in range(2):
        if _ == 1:
            (root / "src/bloom/__init__.py").write_text('__version__ = "0.1.0-release-check"\n')
            git(root, "add", "src/bloom/__init__.py")
            git(root, "commit", "-m", "Reviewed harmless version change")
            prepare(root)
            plan = json.loads((root / "workspace/service/candidate.json").read_text())
        env = dict(
            os.environ,
            PYTHONPATH=plan["source"] + "/src",
            BLOOM_SOURCE_ROOT=plan["source"],
            BLOOM_RELEASE_ID=plan["commit"],
            BLOOM_SERVICE="1",
            PYTHONDONTWRITEBYTECODE="1",
            OMP_NUM_THREADS="1",
        )
        with (root / "bootstrap.log").open("ab") as log:
            proc = subprocess.Popen(
                [str(source / ".venv/bin/python"), "-m", "bloom.cli", "--root", str(root), "run"],
                env=env,
                stdout=log,
                stderr=log,
            )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = health(root, plan)
                if status["healthy"] and status["pid"] == proc.pid:
                    break
                assert proc.poll() is None, (root / "bootstrap.log").read_text()
                time.sleep(0.05)
            assert status["healthy"] and status["pid"] == proc.pid, status
            inspection = subprocess.run(
                [str(source / ".venv/bin/python"), "-m", "bloom.cli", "status"],
                env=dict(env, BLOOM_ROOT=str(root)),
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert inspection.returncode == 0, inspection.stderr
            assert any(t["id"] == task for t in json.loads(inspection.stdout)["tasks"])
            assert not (Path(plan["source"]) / "workspace").exists()
            assert status["version"] == ("0.1.0-release-check" if _ == 1 else "0.1.0")
            identities.append(status["pid"])
            deadline = time.monotonic() + 5
            while (
                store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] != "completed"
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert (
                store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
            )
            assert len(store.rows("SELECT * FROM model_calls")) == 1
            assert len(store.rows("SELECT * FROM deliveries")) == 1
        finally:
            proc.terminate()
            assert proc.wait(timeout=10) == 0
        assert not health(root, plan)["healthy"]
    assert identities[0] != identities[1]
    assert len(list((root / "logs/service").glob("*/launch.json"))) == 2
    assert source_manifest(Path(plan["source"])) == plan["manifest"]
    (root / "src/bloom/__init__.py").write_text("# unreviewed source edit\n")
    with pytest.raises(RuntimeError, match="Commit reviewed"):
        prepare(root)
