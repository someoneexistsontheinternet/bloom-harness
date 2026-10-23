import asyncio
import json
from pathlib import Path

import pytest

from bloom.service import process_identity
from bloom.workspaces import Workspaces, git


async def test_failed_or_cancelled_candidate_cannot_integrate_and_cancel_stops_owned_check(store):
    repo = store.config.root / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Bloom validation")
    git(repo, "config", "user.email", "validation@localhost")
    (repo / "value").write_text("before")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Base")
    task = store.new_task("Test failed candidate containment")
    workspaces = Workspaces(store)
    row = workspaces.create(task, str(repo))
    candidate = Path(row["path"])
    (candidate / "value").write_text("after")
    git(candidate, "add", ".")
    git(candidate, "commit", "-m", "Candidate")
    workspaces.submit(task)
    failed = await workspaces.validate(task, "exit 7")
    assert failed["exit_code"] == 7
    with pytest.raises(ValueError, match="passing frozen"):
        workspaces.integrate(failed["release"])
    assert (repo / "value").read_text() == "before"
    pending = asyncio.create_task(workspaces.validate(task, "sleep 30"))
    directory = store.config.workspace / "release-checks" / task
    for _ in range(100):
        files = list(directory.glob("*/process.json"))
        if len(files) == 2:
            break
        await asyncio.sleep(0.02)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    receipts = list(directory.glob("*/interrupted.json"))
    assert len(receipts) == 1
    identity = json.loads((receipts[0].parent / "process.json").read_text())
    assert process_identity(identity["pid"]) != identity["start_identity"]
    assert json.loads(receipts[0].read_text())["exit_code"] is not None
    assert (repo / "value").read_text() == "before"
