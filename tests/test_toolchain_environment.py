import asyncio
import hashlib
import io
import json
import tarfile

import jsonschema
import pytest

from bloom import toolchain
from bloom.runtime import Runtime
from test_control_provenance import invoke
from test_runtime_delivery import message
from test_tools_processes import operation


def test_dependency_package_checks_and_immutable_install(store, monkeypatch):
    data = b"#!/bin/sh\nexit 0\n"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in ("rg", "COPYING", "LICENSE-MIT", "UNLICENSE"):
            item = tarfile.TarInfo(toolchain.PACKAGE + "/" + name)
            value = data if name == "rg" else b"fixture license"
            item.size = len(value)
            archive.addfile(item, io.BytesIO(value))
    path = store.config.root / "package.tar.gz"
    path.write_bytes(output.getvalue())
    monkeypatch.setattr(toolchain, "ARCHIVE_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(toolchain, "BINARY_SHA256", hashlib.sha256(data).hexdigest())
    result = toolchain.prepare(store.config.root, path)
    assert result == toolchain.verify(store.config.root)
    assert toolchain.prepare(store.config.root, path) == result
    binary = toolchain.directory(store.config.root) / "rg"
    binary.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="pinned"):
        toolchain.prepare(store.config.root, path)
    assert binary.read_bytes() == b"changed"  # Never silently replace an active dependency.


async def test_worker_shell_heredoc_pipeline_and_grants(store):
    runtime = Runtime(store)
    task = store.new_task("Exercise actual local shell contracts")
    child = await runtime.request(
        "astra",
        task,
        "child",
        {
            "operation": "delegate",
            "role": "luna",
            "purpose": "evidence",
            "mode": "read",
            "objective": "Inspect without editing project files",
        },
    )
    python = store.config.root / ".venv/bin/python"
    for ident, command, expected in [
        ("heredoc", str(python) + " - <<'PYTEST'\nprint(739)\nPYTEST\n", 0),
        ("pipeline", "false | head -n 1", 1),
        ("system-git", "/usr/bin/git --version", 0),
        ("denied", "echo forbidden > forbidden.txt", 1),
    ]:
        operation(store, child["task_id"], "start_process", ident)
        result = await runtime.tools.processes.start(child["agent_id"], ident, command)
        for _ in range(100):
            if result.get("result"):
                break
            await asyncio.sleep(0.02)
            result = await runtime.tools.processes.observe(child["agent_id"], result["process_id"])
        assert result["result"]["exit_code"] == expected, result
        if ident == "heredoc":
            assert result["output"].strip() == "739"
    assert not (store.config.root / "forbidden.txt").exists()


async def test_evidence_target_enum_and_long_valid_report(store):
    runtime = Runtime(store)
    task = store.new_task("Check scoped evidence")
    await invoke(
        runtime,
        store,
        task,
        "apply_request",
        {
            "path": "config/agent-presets/inspect-files.toml",
            "bindings": {"question": "Inspect evidence", "cwd": str(store.config.root)},
        },
        "launch",
    )
    child = store.one("SELECT * FROM tasks WHERE parent_id=?", (task,))
    schemas = {s["name"]: s for s in runtime.tools.schemas(child["agent_id"])}
    assert schemas["read_file"]["parameters"]["properties"]["target"]["enum"] == ["local"]
    with pytest.raises(jsonschema.ValidationError):
        await runtime.tools.run(
            child["agent_id"],
            child["id"],
            "bad",
            "start_process",
            {"command": "true", "target": "Luna shallow RAID discovery"},
        )
    assert not store.rows("SELECT * FROM processes")
    (store.config.root / "source.txt").write_text("Exact observed marker.")
    receipt = await invoke(runtime, store, child["id"], "read_file", {"path": "source.txt"}, "read")
    summary = "Substantive evidence. " * 300
    item = store.item(
        child["agent_id"],
        message(
            json.dumps(
                {
                    "status": "found",
                    "summary": summary,
                    "evidence": [
                        {"operation_id": receipt["id"], "quote": "Exact observed marker."}
                    ],
                }
            )
        ),
        child["id"],
    )
    store.db.execute(
        "UPDATE tasks SET final_item=?,status='completed' WHERE id=?", (item, child["id"])
    )
    report = runtime.evidence.report(store.one("SELECT * FROM tasks WHERE id=?", (child["id"],)))
    assert report["summary"] == summary and not report.get("invalid_report")
    assert not runtime.evidence.retry_report(child["id"])
