import json
import plistlib
from types import SimpleNamespace

import pytest

from bloom import service


def test_unload_waits_for_actual_job_disappearance(store, monkeypatch):
    path = store.config.root / "owned.plist"
    path.write_bytes(plistlib.dumps({"Label": service.LABEL}))
    monkeypatch.setattr(service, "installed_path", lambda root: path)
    loaded_after_ack = iter([0, 0, 0, 1])
    calls = []

    def run(command, **kwargs):
        calls.append(command[1])
        code = next(loaded_after_ack) if command[1] == "print" else 0
        return SimpleNamespace(returncode=code, stdout="", stderr="")

    monkeypatch.setattr(service.subprocess, "run", run)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)
    assert service.unload(store.config.root)["stopped"]
    assert calls == ["print", "bootout", "print", "print", "print"]


@pytest.mark.parametrize("has_prior", [False, True])
def test_registration_failure_records_outcome_and_recovers(store, monkeypatch, has_prior):
    root = store.config.root
    directory = root / "workspace/service"
    directory.mkdir()
    (directory / "candidate.json").write_text(json.dumps({"commit": "new"}))
    if has_prior:
        (directory / "active.json").write_text(json.dumps({"commit": "old"}))
    attempts, stopped = [], []

    def launch(root, plan):
        attempts.append(plan["commit"])
        if plan["commit"] == "new":
            raise RuntimeError("Bootstrap failed: 5")
        return {"healthy": True, "source": "old-source"}

    monkeypatch.setattr(service, "launch", launch)
    monkeypatch.setattr(service, "health", lambda *args: {"healthy": False})
    monkeypatch.setattr(service, "unload", lambda root: stopped.append(root) or {"stopped": True})
    monkeypatch.setattr("sys.argv", ["service", "start", "--root", str(root)])
    with pytest.raises(SystemExit) as outcome:
        service.main()
    assert outcome.value.code == 1
    result = json.loads((directory / "last-start.json").read_text())
    assert result["health"] == {"healthy": False, "error": "Bootstrap failed: 5"}
    if has_prior:
        assert attempts == ["new", "old"] and result["rollback"]["healthy"] and not stopped
        assert json.loads((directory / "active.json").read_text())["commit"] == "old"
    else:
        assert attempts == ["new"] and stopped == [root] and result["cleanup"]["stopped"]
