"""Frozen source releases and observable GUI launchd lifecycle. No credential copies."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import plistlib
import shlex
import sqlite3
import subprocess
import tarfile
import time
from pathlib import Path

from .store import atomic, encoded
from .privacy import clean

LABEL = "dev.bloom.agent"


def checked(*args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace"))
    return result.stdout


def process_identity(pid):
    return subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True
    ).stdout.strip()


def source_manifest(directory):
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def prepare(root):
    relevant = [
        "src",
        "config",
        "prompts",
        "tests",
        "pyproject.toml",
        "uv.lock",
        "AGENTS.md",
        "scripts/service.py",
    ]
    if checked("git", "status", "--porcelain", "--", *relevant, cwd=root).strip():
        raise RuntimeError(
            "Commit reviewed source/configuration/tests before preparing a release; operational notes do not block release"
        )
    from .toolchain import verify

    tools = verify(root)
    commit = checked("git", "rev-parse", "HEAD", cwd=root).decode().strip()
    release = root / "artifacts/releases" / commit
    with tarfile.open(
        fileobj=io.BytesIO(checked("git", "archive", commit, cwd=root)), mode="r:"
    ) as archive:
        expected = {}
        for member in archive.getmembers():
            if member.isfile():
                expected[member.name] = hashlib.sha256(
                    archive.extractfile(member).read()
                ).hexdigest()
            elif not member.isdir():
                raise RuntimeError(
                    "Release archives may only contain ordinary files and directories"
                )
        if not release.exists():
            release.mkdir(parents=True)
            try:
                archive.extractall(release, filter="data")
            except BaseException:
                atomic(
                    release / "INCOMPLETE", b"Preparation failed; do not start this candidate.\n"
                )
                raise
    if source_manifest(release) != expected:
        raise RuntimeError(
            "Frozen source differs from the committed archive; candidate was not selected"
        )
    plan = {
        "commit": commit,
        "source": str(release),
        "root": str(root),
        "python": str(root / ".venv/bin/python"),
        "manifest": expected,
        "tools": tools,
    }
    atomic(root / "workspace/service/candidate.json", encoded(plan))
    atomic(root / "workspace/service" / (LABEL + ".plist"), plistlib.dumps(make_plist(root, plan)))
    return {k: v for k, v in plan.items() if k != "manifest"}


def make_plist(root, plan):
    from .toolchain import search_path

    command = shlex.join([plan["python"], "-m", "bloom.cli", "--root", str(root), "run"])
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/zsh", "-c", "exec " + command],
        # Let Python perform its own OS access check before opening RAID logs/cwd.
        "WorkingDirectory": str(Path.home()),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "EnvironmentVariables": {
            "PYTHONPATH": plan["source"] + "/src",
            "BLOOM_SOURCE_ROOT": plan["source"],
            "BLOOM_ROOT": str(root),
            "BLOOM_RELEASE_ID": plan["commit"],
            "BLOOM_SERVICE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": search_path(root),
        },
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def health(root, expected=None):
    try:
        state = json.loads((root / "workspace/runtime.json").read_text())
    except (OSError, ValueError):
        return {"healthy": False, "reason": "No readable runtime heartbeat"}
    alive = (
        bool(state.get("start_identity"))
        and process_identity(state["pid"]) == state["start_identity"]
    )
    current = time.time() - state.get("updated_at", 0) < 15
    matched = expected is None or (
        state.get("release") == expected["commit"] and state.get("source") == expected["source"]
    )
    healthy = (
        alive
        and current
        and matched
        and state.get("status") == "running"
        and state.get("discord") in ("disabled", "ready")
    )
    return {
        "healthy": healthy,
        "alive": alive,
        "fresh": current,
        "expected_source": matched,
        **state,
    }


def redirect_service_logs(root):
    run = time.strftime("local-%Y%m%d_%H%M%S") + "-" + str(os.getpid())
    directory = root / "logs/service" / run
    directory.mkdir(parents=True, exist_ok=False)
    for descriptor, name in ((1, "stdout.log"), (2, "stderr.log")):
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.dup2(fd, descriptor)
        os.close(fd)
    atomic(
        directory / "launch.json",
        encoded(
            {
                "pid": os.getpid(),
                "start_identity": process_identity(os.getpid()),
                "root": str(root),
                "source": os.environ.get("BLOOM_SOURCE_ROOT"),
                "release": os.environ.get("BLOOM_RELEASE_ID"),
                "python": os.sys.executable,
                "started_at": time.time(),
                "command": ["bloom", "--root", str(root), "run"],
            }
        ),
    )
    return directory


def installed_path(root, label=None):
    label = label or LABEL
    path = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    if path.exists():
        existing = plistlib.loads(path.read_bytes())
        if existing.get("Label") != label or existing.get("EnvironmentVariables", {}).get(
            "BLOOM_ROOT"
        ) != str(root):
            raise RuntimeError(
                "Existing launch agent belongs to another root or an unverified installation; inspect before replacement"
            )
    return path


def unload(root, timeout=30, *, label=None):
    path = installed_path(root, label=label) if label else installed_path(root)
    label = f"gui/{os.getuid()}/" + (label or LABEL)

    def loaded():
        return subprocess.run(["launchctl", "print", label], capture_output=True).returncode == 0

    if not loaded():
        return {"label": label, "stopped": True, "already_stopped": True}
    if not path.exists():
        raise RuntimeError("Loaded service has no owned installed plist; refusing to stop it")
    result = subprocess.run(["launchctl", "bootout", label], capture_output=True, text=True)
    if result.returncode and loaded():
        raise RuntimeError("Could not unload the owned service: " + clean(result.stderr.strip()))
    deadline = time.monotonic() + timeout
    # bootout may acknowledge removal before launchd unregisters the old job.
    # A new bootstrap with that label during this interval can fail with status 5.
    while loaded():
        if time.monotonic() >= deadline:
            raise RuntimeError("Owned service is still unloading; no replacement was registered")
        time.sleep(0.1)
    return {"label": label, "stopped": True, "exit_status": result.returncode}


def launch(root, plan, timeout=30):
    if (root / "workspace/reset-pending.json").exists():
        raise RuntimeError("A session reset is incomplete; run service.py reset to resume it")
    if Path(plan["root"]) != root or Path(plan["source"]).parent != root / "artifacts/releases":
        raise ValueError("Release plan is not owned by this workspace")
    if source_manifest(Path(plan["source"])) != plan["manifest"]:
        raise ValueError("Prepared source changed; refusing to start it")
    if plan.get("tools"):
        from .toolchain import verify

        if verify(root) != plan["tools"]:
            raise ValueError("Prepared command dependency changed; refusing deployment")
    database = root / "workspace/bloom.sqlite3"
    if database.exists():
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            for table, module in (
                ("task_assignments", "assignments"),
                ("research_views", "research_views"),
                ("semantic_upkeep", "semantic_upkeep"),
                ("live_previews", "live_preview"),
            ):
                if (
                    db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
                    and db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()
                    and "src/bloom/" + module + ".py" not in plan["manifest"]
                ):
                    raise ValueError(
                        "This release cannot preserve accepted "
                        + table
                        + " receipts; choose a compatible release"
                    )
            if any(
                db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
                and db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()
                for table in ("worker_dependencies", "worker_batches", "worker_reporting")
            ):
                schema = Path(plan["source"]) / "src/bloom/schema.sql"
                if not schema.is_file() or any(
                    table not in schema.read_text()
                    for table in ("worker_dependencies", "worker_batches", "worker_reporting")
                ):
                    raise ValueError(
                        "This release cannot preserve explicit worker supplementary scheduling; choose a compatible release"
                    )
            if any(
                db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
                and db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()
                for table in ("worker_launches", "worker_awaits", "preset_invocations")
            ):
                if (
                    "src/bloom/workers.py" not in plan["manifest"]
                    or "src/bloom/presets.py" not in plan["manifest"]
                ):
                    raise ValueError(
                        "This release cannot resume explicit worker launches/preset receipts; choose a compatible release"
                    )
            if (
                db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone()
                and db.execute(
                    "SELECT 1 FROM events WHERE kind='model.tool_contract' LIMIT 1"
                ).fetchone()
            ):
                if "src/bloom/progress.py" not in plan["manifest"]:
                    raise ValueError(
                        "This release cannot read public progress/process-wait arguments; choose a compatible release"
                    )
            if (
                db.execute("SELECT 1 FROM sqlite_master WHERE name='process_awaits'").fetchone()
                and db.execute("SELECT 1 FROM process_awaits LIMIT 1").fetchone()
            ):
                if (
                    "process_awaits"
                    not in (Path(plan["source"]) / "src/bloom/runtime.py").read_text()
                ):
                    raise ValueError(
                        "This release cannot resume foreground process waits; choose a compatible release"
                    )
            if (
                db.execute("SELECT 1 FROM sqlite_master WHERE name='model_call_sources'").fetchone()
                and db.execute("SELECT 1 FROM model_call_sources LIMIT 1").fetchone()
            ):
                context_source = Path(plan["source"]) / "src/bloom/context.py"
                if (
                    not context_source.is_file()
                    or "model_call_sources" not in context_source.read_text()
                ):
                    raise ValueError(
                        "This release cannot read derived compaction sources; choose a compatible release"
                    )
    if database.exists() and "src/bloom/evidence.py" not in plan["manifest"]:
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            if (
                db.execute("SELECT 1 FROM sqlite_master WHERE name='evidence_batches'").fetchone()
                and db.execute("SELECT 1 FROM evidence_batches LIMIT 1").fetchone()
            ):
                raise ValueError(
                    "This release cannot resume parallel evidence batches; choose a compatible release"
                )
    if database.exists() and "src/bloom/recall.py" not in plan["manifest"]:
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            if (
                db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_retrievals'").fetchone()
                and db.execute("SELECT 1 FROM memory_retrievals LIMIT 1").fetchone()
            ):
                raise ValueError(
                    "This release cannot resume managed memory retrieval; choose a compatible release"
                )
    if database.exists():
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_approvals'").fetchone():
                runtime_source = Path(plan["source"]) / "src/bloom/runtime.py"
                if (
                    "src/bloom/working_context.py" not in plan["manifest"]
                    or not runtime_source.is_file()
                    or "def approve_memory(" not in runtime_source.read_text()
                ):
                    raise ValueError(
                        "This release lacks Astra memory approval and independent working compaction; choose a compatible release"
                    )
    path = installed_path(root)
    label = f"gui/{os.getuid()}/" + LABEL
    if (
        not path.exists()
        and subprocess.run(["launchctl", "print", label], capture_output=True).returncode == 0
    ):
        raise RuntimeError("Loaded service has no matching owned plist; inspect before replacement")
    launched_at = time.time()
    unload(root)
    atomic(path, plistlib.dumps(make_plist(root, plan)))
    checked("launchctl", "enable", label)
    checked("launchctl", "bootstrap", f"gui/{os.getuid()}", str(path))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = health(root, plan)
        if state["healthy"] and state.get("runtime_started_at", 0) >= launched_at:
            return state
        time.sleep(0.3)
    return health(root, plan)


def main():
    parser = argparse.ArgumentParser(description="Prepare and control a committed Bloom release")
    parser.add_argument(
        "action", choices=("prepare", "start", "stop", "status", "rollback", "reset")
    )
    parser.add_argument(
        "--allow-terminal-records",
        action="store_true",
        help="For an explicitly authorized reset, preserve terminal failures in backup without relabeling them",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BLOOM_ROOT", str(Path(__file__).resolve().parents[2]))),
    )
    args = parser.parse_args()
    root = args.root.resolve()
    if args.allow_terminal_records and args.action != "reset":
        parser.error("--allow-terminal-records applies only to reset")
    state = root / "workspace/service"
    active = state / "active.json"
    previous = state / "previous.json"
    label = f"gui/{os.getuid()}/" + LABEL
    if args.action == "prepare":
        result = prepare(root)
    elif args.action == "status":
        loaded = subprocess.run(["launchctl", "print", label], capture_output=True).returncode == 0
        plan = json.loads(active.read_text()) if active.exists() else None
        result = {"loaded": loaded, "label": label, "health": health(root, plan)}
    elif args.action == "stop":
        if not installed_path(root).exists():
            raise RuntimeError(
                "No owned installed plist; refusing to stop an unverified service label"
            )
        result = unload(root)
    else:
        selected = previous if args.action == "rollback" else state / "candidate.json"
        if not selected.exists():
            raise RuntimeError(
                "No recorded "
                + ("previous release" if args.action == "rollback" else "prepared candidate")
            )
        plan = json.loads(selected.read_text())
        prior = json.loads(active.read_text()) if active.exists() else None
        reset = None
        if args.action == "reset":
            from .config import Config
            from .reset import apply, inspect
            from .storage import LABEL as ARCHIVE_LABEL

            config = Config(root)
            if (
                plan.get("root") != str(root)
                or Path(plan["source"]).parent != root / "artifacts/releases"
                or source_manifest(Path(plan["source"])) != plan["manifest"]
            ):
                raise ValueError("Prepare an intact owned release before resetting")
            if not (root / "workspace/reset-pending.json").exists():
                check = inspect(config, allow_terminal_records=args.allow_terminal_records)
                if not check["ready"]:
                    raise RuntimeError(
                        "Settle unfinished/uncertain work before reset: "
                        + json.dumps(check["blockers"])
                    )
            # Verify ownership of both jobs before changing either one.
            installed_path(root)
            installed_path(root, label=ARCHIVE_LABEL)
            old_runtime = health(root)
            atomic(
                state / "reset-request.json",
                encoded(
                    {
                        "requested_at": time.time(),
                        "candidate": plan["commit"],
                        "prior_health": old_runtime,
                        "allow_terminal_records": args.allow_terminal_records,
                        "action": "Back up and replace session; retain archive catalog, credentials and worktree files",
                    }
                ),
            )
            try:
                unload(root, label=ARCHIVE_LABEL)
                unload(root)
                deadline = time.monotonic() + 30
                while (
                    old_runtime.get("alive")
                    and process_identity(old_runtime["pid"]) == old_runtime["start_identity"]
                ):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Prior runtime has not exited; session reset refused")
                    time.sleep(0.1)
                reset = apply(config, allow_terminal_records=args.allow_terminal_records)
            except Exception as exc:
                atomic(
                    state / "last-reset.json",
                    encoded(
                        {
                            "commit": plan["commit"],
                            "reset_error": clean(str(exc)),
                            "health": health(root),
                            "recovery": "Resolve the reported blocker and re-run reset; pending intent is resumable.",
                        }
                    ),
                )
                raise
        try:
            status = launch(root, plan)
        except Exception as exc:
            status = {"healthy": False, "error": clean(str(exc))}
        result = {"commit": plan["commit"], "health": status}
        if reset:
            result["reset"] = reset
        if status["healthy"]:
            if prior and prior["commit"] != plan["commit"]:
                atomic(previous, encoded(prior))
            atomic(active, encoded(plan))
        else:
            try:
                if prior and prior["commit"] != plan["commit"]:
                    previous_health = health(root, prior)
                    result["rollback"] = (
                        dict(previous_health, reused_running_prior=True)
                        if previous_health["healthy"]
                        else launch(root, prior)
                    )
                else:
                    # An initial failed deployment must not keep crash-looping.
                    result["cleanup"] = unload(root)
            except Exception as exc:
                result["recovery_error"] = clean(str(exc))
                try:
                    result["cleanup"] = unload(root)
                except Exception as cleanup:
                    result["cleanup_error"] = clean(str(cleanup))
        if reset and (status["healthy"] or result.get("rollback", {}).get("healthy")):
            from .storage import install
            from .store import Store

            try:
                store = Store(config)
                try:
                    if config.data.get("storage", {}).get("enabled"):
                        result["archive"] = install(
                            store, plan=plan if status["healthy"] else prior
                        )
                finally:
                    store.close()
            except Exception as exc:
                result["archive_error"] = clean(str(exc))
        atomic(state / ("last-" + args.action + ".json"), encoded(result))
    print(json.dumps(result, indent=2))
    if (result.get("health", {}).get("healthy") is False and args.action != "status") or result.get(
        "archive_error"
    ):
        raise SystemExit(1)
