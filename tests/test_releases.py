from pathlib import Path

import pytest

from bloom.workspaces import Workspaces, git


async def test_frozen_candidate_check_merge_rollback_preserves_unrelated_notes(store):
    repo = store.config.root / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Bloom validation")
    git(repo, "config", "user.email", "validation@localhost")
    (repo / "value.txt").write_text("old\n")
    (repo / "notes.md").write_text("original notes\n")
    git(repo, "add", "value.txt", "notes.md")
    git(repo, "commit", "-m", "Original fixture")
    task = store.new_task("reviewed self-change")
    workspaces = Workspaces(store)
    row = workspaces.create(task, str(repo))
    candidate = Path(row["path"])
    (candidate / "value.txt").write_text("new\n")
    git(candidate, "add", "value.txt")
    git(candidate, "commit", "-m", "Candidate change")
    workspaces.submit(task)
    check = await workspaces.validate(task, 'test "$(cat value.txt)" = new')
    assert check["exit_code"] == 0, check
    with pytest.raises(ValueError, match="Review the exact"):
        workspaces.integrate(check["release"])
    with pytest.raises(ValueError, match="diff does not match"):
        workspaces.review(check["release"], "astra", "0" * 64, "Incorrect revision review")
    workspaces.review(
        check["release"],
        "astra",
        check["diff_sha256"],
        "Inspected the value.txt change from old to new and the passing exact-value check; no other source files changed.",
    )
    (repo / "notes.md").write_text("unrelated active notes\n")
    workspaces.integrate(check["release"])
    assert (repo / "value.txt").read_text() == "new\n"
    assert (repo / "notes.md").read_text() == "unrelated active notes\n"
    workspaces.rollback(check["release"])
    assert (repo / "value.txt").read_text() == "old\n"
    assert (repo / "notes.md").read_text() == "unrelated active notes\n"
