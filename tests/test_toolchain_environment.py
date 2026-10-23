import asyncio
import hashlib
import io
import json
import shlex
import subprocess
import tarfile

import jsonschema
import pytest

from bloom import execution, toolchain
from bloom.runtime import Runtime
from test_control_provenance import invoke
from test_runtime_delivery import message
from test_tools_processes import operation


def test_command_path_prefers_selected_developer_binaries_to_system_shims(tmp_path, monkeypatch):
    developer = tmp_path / "Xcode.app/Contents/Developer"
    real_bin = developer / "usr/bin"
    shim_bin = tmp_path / "system-shims"
    for directory, value in ((real_bin, "native"), (shim_bin, "shim")):
        directory.mkdir(parents=True)
        binary = directory / "bloom-toolchain-probe"
        binary.write_text(f"#!/bin/sh\nprintf '{value}'\n")
        binary.chmod(0o755)
    monkeypatch.setattr(toolchain, "developer_directory", lambda: developer)
    result = subprocess.run(
        ["bloom-toolchain-probe"],
        env={"PATH": toolchain.search_path(tmp_path, str(shim_bin))},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and result.stdout == "native", result.stderr


def test_selected_xcode_read_grant_preserves_sibling_and_write_denials(tmp_path, monkeypatch):
    assigned = tmp_path / "assigned"
    assigned.mkdir()
    selected = tmp_path / "Applications" / "Xcode Test.app"
    developer = selected / "Contents/Developer"
    developer.mkdir(parents=True)
    library = selected / "Contents/SharedFrameworks/library.txt"
    library.parent.mkdir()
    library.write_text("selected toolchain")
    sibling = selected.parent / "Unrelated.app/private.txt"
    sibling.parent.mkdir()
    sibling.write_text("unrelated app")
    receipt = tmp_path / "Library/Preferences/com.apple.dt.Xcode.plist"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("existing license receipt")
    private_pref = receipt.parent / "unrelated.plist"
    private_pref.write_text("unrelated preference")
    monkeypatch.setenv("DEVELOPER_DIR", str(sibling.parent))
    monkeypatch.setattr(execution, "XCODE_LICENSE_RECEIPT", receipt)

    def selection(args, **kwargs):
        assert args == ["/usr/bin/xcode-select", "--print-path"]
        assert "DEVELOPER_DIR" not in kwargs["env"]
        return subprocess.CompletedProcess(args, 0, str(developer) + "\n", "")

    with monkeypatch.context() as patch:
        patch.setattr(execution.subprocess, "run", selection)
        profile = execution.sandbox("read", assigned)

    for path in (library, receipt):
        original = path.read_text()
        result = subprocess.run(profile + ["/bin/cat", str(path)], capture_output=True, text=True)
        assert result.returncode == 0 and result.stdout == original, result.stderr
        write = subprocess.run(profile + ["/usr/bin/touch", str(path)], capture_output=True)
        assert write.returncode != 0 and path.read_text() == original
    for path in (sibling, private_pref):
        denied = subprocess.run(profile + ["/bin/cat", str(path)], capture_output=True)
        assert denied.returncode != 0


@pytest.mark.parametrize("output", ["", ".", "/", "relative/Developer"])
def test_invalid_developer_selection_does_not_expand_read_grants(monkeypatch, output):
    monkeypatch.setattr(
        execution.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, output, ""),
    )
    assert execution.developer_read_paths() == ()


def test_unavailable_developer_selection_keeps_existing_sandbox(monkeypatch):
    def unavailable(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(execution.subprocess, "run", unavailable)
    assert execution.developer_read_paths() == ()


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
    baseline = subprocess.run(["/usr/bin/git", "--version"], capture_output=True, text=True)
    assert baseline.returncode == 0, "Unsandboxed system Git failed: " + baseline.stderr
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
    developer = toolchain.developer_directory()
    assert developer is not None, "Install/select Apple Command Line Tools or Xcode"
    for ident, command, expected in [
        ("heredoc", str(python) + " - <<'PYTEST'\nprint(739)\nPYTEST\n", 0),
        ("pipeline", "false | head -n 1", 1),
        ("path-git", "git --version", 0),
        ("developer-git", shlex.quote(str(developer / "usr/bin/git")) + " --version", 0),
        ("denied", "echo forbidden > forbidden.txt", 1),
    ]:
        operation(store, child["task_id"], "start_process", ident)
        result = await runtime.tools.processes.start(child["agent_id"], ident, command)
        for _ in range(100):
            if result.get("result"):
                break
            await asyncio.sleep(0.02)
            result = await runtime.tools.processes.observe(child["agent_id"], result["process_id"])
        assert result["result"]["exit_code"] == expected, f"{ident}: {result.get('output', '')}"
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
