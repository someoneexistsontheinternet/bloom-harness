import asyncio
import hashlib
import json
import os
import sys

import pytest

from bloom.runtime import Runtime
from bloom.store import now


def operation(store, task, name, ident):
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
        (ident, task, "tool", name, "running", store.put({}), now()),
    )


async def test_exact_file_edits_and_stale_read(store):
    runtime = Runtime(store)
    task = store.new_task("edit file")
    args = {"path": "example.txt", "text": "one\ntwo\n", "expected_sha256": "absent"}
    result = await runtime.tools.run("astra", task, "a", "write_file", args)
    read = await runtime.tools.run(
        "astra", task, "b", "read_file", {"path": "example.txt", "start": 2, "end": 2}
    )
    assert read["text"] == "two\n"
    assert read["sha256"] == result["sha256"]
    with pytest.raises(ValueError, match="Stale"):
        await runtime.tools.run("astra", task, "c", "write_file", args)
    await runtime.tools.run(
        "astra",
        task,
        "d",
        "patch_file",
        {"path": "example.txt", "old": "two", "new": "three", "expected_sha256": result["sha256"]},
    )
    assert (store.config.root / "example.txt").read_text() == "one\nthree\n"
    (store.config.root / "empty.txt").write_text("")
    empty = await runtime.tools.run("astra", task, "empty", "read_file", {"path": "empty.txt"})
    assert empty["text"] == "" and empty["line_count"] == 0


async def test_worker_permissions_match_tool_schema(store):
    runtime = Runtime(store)
    task = store.new_task("permissions")
    result = await runtime.request(
        "astra",
        task,
        "delegate",
        {
            "operation": "delegate",
            "role": "luna",
            "purpose": "evidence",
            "mode": "read",
            "objective": "Inspect only",
        },
    )
    worker = result["agent_id"]
    names = {x["name"] for x in runtime.tools.schemas(worker)}
    assert "write_file" not in names and "desktop" not in names and "web_fetch" not in names
    with pytest.raises(PermissionError):
        await runtime.tools.run(
            worker,
            result["task_id"],
            "a",
            "write_file",
            {"path": "forbidden", "text": "x", "expected_sha256": "absent"},
        )
    with pytest.raises(PermissionError):
        await runtime.tools.run(worker, result["task_id"], "b", "read_file", {"path": "/etc/hosts"})
    with pytest.raises(PermissionError, match="operational"):
        await runtime.tools.run(
            worker, result["task_id"], "c", "read_file", {"path": str(store.path)}
        )


async def test_detached_process_input_and_reconnect(store):
    runtime = Runtime(store)
    task = store.new_task("durable process")
    operation(store, task, "start_process", "process-check")
    result = await runtime.tools.processes.start(
        "astra",
        "process-check",
        'python -u -c "import sys; print(sys.prefix, flush=True); print(input(), flush=True)"',
    )
    handle = result["process_id"]
    try:
        for _ in range(50):
            row = await runtime.tools.processes.observe("astra", handle)
            if row.get("identity") and row.get("output"):
                break
            await asyncio.sleep(0.05)
        assert str(store.config.root / ".venv") in row["output"] or sys.prefix in row["output"]
        # A new Runtime reattaches by persisted identity rather than relaunching.
        other = Runtime(store)
        assert (await other.tools.processes.control("astra", handle, "input", "hello\n"))["ok"]
        for _ in range(50):
            row = await other.tools.processes.observe("astra", handle)
            if row.get("result"):
                break
            await asyncio.sleep(0.05)
        assert row["result"]["exit_code"] == 0
        assert "hello" in row["output"]
    finally:
        row = await runtime.tools.processes.observe("astra", handle)
        if not row.get("result") and row.get("identity"):
            await runtime.tools.processes.control("astra", handle, "cancel")


async def test_kernel_sandbox_contains_descendant_writes(store):
    runtime = Runtime(store)
    task = store.new_task("containment")
    worker = await runtime.request(
        "astra",
        task,
        "spawn",
        {
            "operation": "delegate",
            "role": "luna",
            "purpose": "attempt",
            "mode": "read",
            "objective": "Check sandbox",
        },
    )
    operation(store, worker["task_id"], "start_process", "sandbox-check")
    destination = store.config.root / "escaped.txt"
    command = f'python -c \'import subprocess; subprocess.run(["/bin/sh","-c","echo REACHED_CHILD; echo escaped > {destination}"],check=True)\''
    result = await runtime.tools.processes.start(worker["agent_id"], "sandbox-check", command)
    for _ in range(100):
        row = await runtime.tools.processes.observe(worker["agent_id"], result["process_id"])
        if row.get("result"):
            break
        await asyncio.sleep(0.05)
    assert row.get("result"), row
    assert row["result"]["exit_code"] != 0
    assert "REACHED_CHILD" in row["output"]
    assert "Operation not permitted" in row["output"] or "Permission denied" in row["output"]
    assert not destination.exists()


async def test_luna_scratch_script_cannot_overwrite_project_runtime_or_artifact_files(store):
    import shlex

    runtime = Runtime(store)
    task = store.new_task("Protect the shared runtime from workspace writes")
    worker = await runtime.request(
        "astra",
        task,
        "spawn",
        {
            "operation": "delegate",
            "role": "luna",
            "purpose": "attempt",
            "mode": "read",
            "objective": "Test a disposable scratch script",
        },
    )
    protected = [
        store.config.workspace / "protected.txt",
        store.config.artifacts / "protected.txt",
        store.config.root / "protected.txt",
    ]
    for path in protected:
        path.write_text("preserved")
    operation(store, worker["task_id"], "start_process", "private-write-check")
    script = (
        'import os\nfrom pathlib import Path\np=Path(os.environ["TMPDIR"])/"candidate.py"\np.write_text("print(123)")\nexec(p.read_text())\nfor value in '
        + repr([str(p) for p in protected])
        + ':\n try:\n  Path(value).write_text("corrupted")\n  raise AssertionError("Private write was permitted")\n except PermissionError:\n  print("PROTECTED", flush=True)\n'
    )
    result = await runtime.tools.processes.start(
        worker["agent_id"], "private-write-check", "python -c " + shlex.quote(script)
    )
    for _ in range(100):
        result = await runtime.tools.processes.observe(worker["agent_id"], result["process_id"])
        if result.get("result"):
            break
        await asyncio.sleep(0.05)
    assert result.get("result", {}).get("exit_code") == 0, result
    assert result["output"].count("PROTECTED") == 3 and "123" in result["output"]
    assert not (store.config.root / "candidate.py").exists()
    assert all(path.read_text() == "preserved" for path in protected)
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(PermissionError):
        runtime.tools.path(
            worker["agent_id"], str(store.config.root / ".venv" / "pyvenv.cfg"), write=True
        )
