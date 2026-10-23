import asyncio
import json
import os
import time

import pytest

from bloom.safari_helper import PROTOCOL, SafariHelper, fingerprint
from bloom.store import atomic, encoded


@pytest.fixture
def helper(tmp_path):
    value = SafariHelper(tmp_path / "workspace")
    value.app.mkdir(parents=True)
    atomic(
        value.manifest_path,
        encoded({"build_id": fingerprint(value.workspace), "protocol": PROTOCOL}),
    )
    return value


def answer(path, **fields):
    request = json.loads(path.read_text())
    atomic(
        path.with_name("response.json"),
        encoded({"id": request["id"], "build_id": request["build_id"], **fields}),
    )
    return request


async def test_native_response_exact_private_and_success_scratch_removed(helper, monkeypatch):
    seen = []

    async def dispatch(path):
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        seen.append(answer(path, ok=True, stdout='exact\nquoted "evidence"\n'))

    monkeypatch.setattr(helper, "dispatch", dispatch)
    assert await helper.call("eval", 123, "/owned/evaluate.js") == 'exact\nquoted "evidence"\n'
    assert seen[0]["action"] == "eval" and seen[0]["args"] == ["123", "/owned/evaluate.js"]
    assert not list(helper.spool.iterdir())


async def test_missing_permission_is_explicit_and_failure_evidence_is_archivable(
    helper, monkeypatch
):
    async def dispatch(path):
        answer(path, ok=False, kind="permission", error="Grant Safari access in the named helper")

    monkeypatch.setattr(helper, "dispatch", dispatch)
    with pytest.raises(PermissionError, match="Grant Safari access"):
        await helper.call("open")
    retained = list(
        (helper.workspace.parent / "logs/browser-helper").glob("*/open-*/response.json")
    )
    assert len(retained) == 1 and json.loads(retained[0].read_text())["kind"] == "permission"
    assert not list(helper.spool.iterdir())


async def test_response_identity_mismatch_is_not_accepted_or_erased(helper, monkeypatch):
    async def dispatch(path):
        answer(path, ok=True, stdout="wrong response", id="wrong")

    monkeypatch.setattr(helper, "dispatch", dispatch)
    with pytest.raises(RuntimeError, match="identity/build mismatch"):
        await helper.call("url", 1)
    assert list((helper.workspace.parent / "logs/browser-helper").glob("*/url-*/response.json"))


async def test_timeout_and_cancellation_leave_do_not_execute_marker(helper, monkeypatch):
    async def dispatch(path):
        pass

    monkeypatch.setattr(helper, "dispatch", dispatch)
    with pytest.raises(TimeoutError):
        await helper.call("open", timeout=0.01)
    first = next(helper.spool.iterdir())
    assert (first / "cancelled").is_file() and not (first / "response.json").exists()
    task = asyncio.create_task(helper.call("url", 123))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all((p / "cancelled").is_file() for p in helper.spool.iterdir())


async def test_expired_orphan_moves_to_logs_with_honest_uncertain_receipt(helper, monkeypatch):
    async def dispatch(path):
        pass

    monkeypatch.setattr(helper, "dispatch", dispatch)
    with pytest.raises(TimeoutError):
        await helper.call("open", timeout=0.01)
    directory = next(helper.spool.iterdir())
    path = directory / "request.json"
    request = json.loads(path.read_text())
    request["deadline"] = time.time() - 300
    atomic(path, encoded(request))
    os.utime(directory, (time.time() - 300,) * 2)
    helper.sweep()
    receipt = next((helper.workspace.parent / "logs/browser-helper").glob("*/open-*/response.json"))
    assert json.loads(receipt.read_text())["receipt_source"] == "client_expiry"
    assert not directory.exists()


async def test_late_success_after_timeout_is_preserved_for_effect_recovery(helper, monkeypatch):
    async def dispatch(path):
        pass

    monkeypatch.setattr(helper, "dispatch", dispatch)
    with pytest.raises(TimeoutError):
        await helper.call("open", timeout=0.01)
    directory = next(helper.spool.iterdir())
    answer(directory / "request.json", ok=True, stdout="owned-window-123")
    os.utime(directory, (time.time() - 300,) * 2)
    helper.sweep()
    receipt = next((helper.workspace.parent / "logs/browser-helper").glob("*/open-*/response.json"))
    assert json.loads(receipt.read_text())["stdout"] == "owned-window-123"
    assert receipt.with_name("cancelled").exists()


async def test_unprepared_or_mismatched_helper_does_not_dispatch(tmp_path, helper, monkeypatch):
    async def forbidden(*args):
        raise AssertionError("Must not dispatch a mismatched build")

    monkeypatch.setattr(helper, "dispatch", forbidden)
    with pytest.raises(RuntimeError, match="not prepared"):
        await SafariHelper(tmp_path / "empty").call("open")
    atomic(helper.manifest_path, encoded({"build_id": "old", "protocol": PROTOCOL}))
    with pytest.raises(RuntimeError, match="differs from this source"):
        await helper.call("open")
    with pytest.raises(ValueError, match="Invalid"):
        await helper.call("shell", "anything")
