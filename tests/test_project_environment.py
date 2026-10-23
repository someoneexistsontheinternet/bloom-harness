import asyncio
import shlex
from pathlib import Path

from bloom.runtime import Runtime
from bloom.workspaces import git
from test_tools_processes import operation


async def test_read_worker_can_run_aliased_project_venv_git_and_disposable_tests(store):
    project = store.config.root / "project"
    project.mkdir()
    sibling = store.config.root / "other-private.txt"
    sibling.write_text("Private sibling fixture")
    (project / "test_read.py").write_text(
        'from pathlib import Path\nimport pytest\ndef test_scratch(tmp_path):\n    p=tmp_path/"value";p.write_text("evidence");assert p.read_text()=="evidence"\ndef test_sibling_data_is_still_denied():\n    with pytest.raises(PermissionError):\n        Path('
        + repr(str(sibling))
        + ").read_bytes()\n"
    )
    git(project, "init", "-b", "main")
    git(project, "config", "user.name", "Bloom validation")
    git(project, "config", "user.email", "validation@localhost")
    git(project, "add", ".")
    git(project, "commit", "-m", "Read-only environment fixture")
    runtime = Runtime(store)
    task = store.new_task("Exercise a real assigned worktree and environment")
    sol = await runtime.request(
        "astra",
        task,
        "sol",
        {
            "operation": "delegate",
            "role": "sol",
            "objective": "Own the candidate",
            "repository": str(project),
        },
    )
    luna = await runtime.request(
        sol["agent_id"],
        sol["task_id"],
        "luna",
        {
            "operation": "delegate",
            "role": "luna",
            "mode": "read",
            "objective": "Inspect the supplied tests",
        },
    )
    operation(store, luna["task_id"], "start_process", "read-venv")
    python = store.config.root / ".venv/bin/python"
    command = (
        "git status --porcelain && "
        + shlex.quote(str(python))
        + ' -m pytest -c /dev/null --rootdir . -q -p no:cacheprovider --basetemp "$TMPDIR/pytest" test_read.py'
    )
    started = await runtime.tools.processes.start(luna["agent_id"], "read-venv", command)
    for _ in range(100):
        result = await runtime.tools.processes.observe(luna["agent_id"], started["process_id"])
        if result.get("result"):
            break
        await asyncio.sleep(0.05)
    assert result.get("result", {}).get("exit_code") == 0, result
    assert "2 passed" in result["output"]
    worktree = store.one("SELECT path FROM worktrees WHERE task_id=?", (sol["task_id"],))["path"]
    assert git(worktree, "status", "--porcelain") == ""
