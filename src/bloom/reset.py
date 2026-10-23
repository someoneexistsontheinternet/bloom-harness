"""Explicit, recoverable clean-session resets. Never a model-facing tool."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
import zipfile
import zlib
from pathlib import Path

from .config import Config
from .storage import sync_directory
from .store import Store, atomic, encoded, now

MUTABLE = (
    "memories",
    "workspace/requests",
    "workspace/authoring",
    "workspace/receipts",
    "workspace/processes",
    "workspace/call-views.json",
    "workspace/desktop-latest.png",
    "artifacts/runtime/calls",
    "artifacts/runtime/tool-results",
    "artifacts/runtime/web",
)
CATALOG = ("archive_files", "archive_chunks", "conversation_backups")
EMPTY = (
    "tasks",
    "items",
    "contexts",
    "memories",
    "memory_revisions",
    "memory_nodes",
    "memory_facts",
    "memory_batches",
    "model_calls",
    "operations",
    "processes",
    "model_text_streams",
    "live_previews",
    "deliveries",
    "owner_requests",
    "inbound_messages",
    "browser_sessions",
    "channel_routes",
    "memory_retrievals",
    "evidence_batches",
    "evidence_workers",
    "model_call_sources",
    "process_awaits",
    "preset_invocations",
    "worker_launches",
    "worker_awaits",
    "worker_dependencies",
    "worker_batches",
    "worker_reporting",
    "memory_approvals",
    "working_compactions",
    "task_assignments",
    "model_call_diagnostics",
    "model_reconciliations",
    "research_views",
    "evidence_grants",
    "semantic_upkeep",
    "semantic_upkeep_due",
)


def checked(root, relative):
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError("Reset paths must remain inside the workspace")
    path = root / value
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Reset refuses symbolic paths: " + relative)
        if part == root:
            break
    return path


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(256 * 1024), b""):
            result.update(block)
    return result.hexdigest()


@contextlib.contextmanager
def reader(config, path=None):
    # No schema initialization, cache promotion, access-time update or secrets.
    store = Store.__new__(Store)
    store.config = config
    store.path = path or config.workspace / "bloom.sqlite3"
    store.db = sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    store.db.row_factory = sqlite3.Row
    try:
        yield store
    finally:
        store.close()


def counts(store):
    tables = {r["name"] for r in store.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    return {
        name: store.one("SELECT count(*) AS n FROM " + name)["n"]
        for name in (*EMPTY, "agents", "sessions", "artifacts", *CATALOG)
        if name in tables
    }


def inspect(config, *, allow_terminal_records=False):
    blockers = []
    with reader(config) as store:
        tables = {
            r["name"] for r in store.rows("SELECT name FROM sqlite_master WHERE type='table'")
        }
        checks = {
            "tasks": "status NOT IN ('completed','cancelled')",
            "operations": "status IN ('prepared','running','uncertain')",
            "model_calls": "status IN ('prepared','running','uncertain')",
            "processes": "status NOT IN ('completed','failed')",
            "deliveries": "status!='sent'",
            "worktrees": "1",
            "browser_sessions": "pending_call IS NOT NULL",
            "memory_batches": "status!='completed'",
        }
        retained = {}
        if allow_terminal_records:
            # Explicit owner reset may archive terminal failures without lying
            # about their outcome. Active/uncertain effects remain hard blockers.
            checks["tasks"] = "status NOT IN ('completed','cancelled','blocked')"
            checks["memory_batches"] = (
                "status NOT IN ('completed','failed','interrupted','cancelled','awaiting_approval')"
            )
            checks["worktrees"] = (
                "NOT EXISTS(SELECT 1 FROM tasks t WHERE t.id=worktrees.task_id AND t.status IN ('completed','cancelled','blocked'))"
            )
            retained["tasks"] = store.rows("SELECT id,status FROM tasks WHERE status='blocked'")
            retained["memory_batches"] = store.rows(
                "SELECT id,status,call_id FROM memory_batches WHERE status IN ('failed','interrupted','cancelled','awaiting_approval')"
            )
            retained["worktrees"] = store.rows(
                "SELECT w.id,w.task_id,w.path,w.branch,w.submitted_commit,w.status FROM worktrees w JOIN tasks t ON t.id=w.task_id WHERE t.status IN ('completed','cancelled','blocked')"
            )
        for table, predicate in checks.items():
            if table in tables:
                number = store.one("SELECT count(*) AS n FROM " + table + " WHERE " + predicate)[
                    "n"
                ]
                if number:
                    blockers.append({"table": table, "unsettled": number})
        # A terminal SQLite row alone does not prove its detached supervisor died.
        from .service import process_identity

        for row in store.rows("SELECT id,pid,start_identity FROM processes WHERE target='local'"):
            if (
                row["pid"]
                and row["start_identity"]
                and process_identity(row["pid"]) == row["start_identity"]
            ):
                blockers.append({"live_process": row["id"], "pid": row["pid"]})
        selected = list(MUTABLE)
        if "web_runs" in tables:
            for row in store.rows("SELECT operation_id FROM web_runs"):
                if not re.fullmatch(r"[A-Za-z0-9_-]+", row["operation_id"]):
                    raise ValueError("Unsafe recorded browser operation ID")
                selected.append("workspace/web/" + row["operation_id"])
                lease = checked(config.root, selected[-1]) / "browser.json"
                if lease.exists() and json.loads(lease.read_text()).get("status") != "closed":
                    blockers.append({"unsettled_browser_lease": row["operation_id"]})
        result = {
            "root": str(config.root),
            "counts": counts(store),
            "blockers": blockers,
            "reset_paths": selected,
            "artifact_bytes": store.one("SELECT coalesce(sum(bytes),0) AS n FROM artifacts")["n"],
            "allow_terminal_records": allow_terminal_records,
            "retained_terminal_records": retained,
        }
    bridge = checked(config.root, "workspace/web/bridge")
    if bridge.exists():
        for path in bridge.iterdir():
            if path.is_dir() and not (path / "response.json").is_file():
                blockers.append({"pending_browser_request": path.name})
    result["ready"] = not blockers
    return result


def inventory(root, paths):
    result = {}
    for relative in paths:
        base = checked(root, relative)
        if not base.exists():
            continue
        for path in [base, *sorted(base.rglob("*"))] if base.is_dir() else [base]:
            name = str(path.relative_to(root))
            checked(root, name)
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                result[name] = {"directory": True}
            elif stat.S_ISREG(mode):
                result[name] = {"sha256": digest(path), "bytes": path.stat().st_size}
            else:
                raise ValueError("Reset refuses special files: " + name)
    return result


def verify_bundle(path):
    """Stream every member and verify SQLite plus all registered original bytes."""
    with zipfile.ZipFile(path) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        names = bundle.namelist()
        if len(names) != len(set(names)) or set(names) != set(manifest["files"]) | {
            "manifest.json"
        }:
            raise RuntimeError("Reset backup member set differs from its manifest")
        for name, expected in manifest["files"].items():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("Unsafe reset backup member")
            actual = hashlib.sha256()
            size = 0
            with bundle.open(name) as stream:
                for block in iter(lambda: stream.read(256 * 1024), b""):
                    actual.update(block)
                    size += len(block)
            if actual.hexdigest() != expected["sha256"] or size != expected["bytes"]:
                raise RuntimeError("Reset backup SHA-256 mismatch: " + name)
        temporary = path.parent / (".verify-" + uuid.uuid4().hex + ".sqlite3")
        try:
            with temporary.open("xb") as out, bundle.open("workspace/bloom.sqlite3") as source:
                os.chmod(temporary, 0o600)
                shutil.copyfileobj(source, out)
            with contextlib.closing(
                sqlite3.connect(temporary.as_uri() + "?mode=ro", uri=True)
            ) as db:
                if (
                    db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                    or db.execute("PRAGMA foreign_key_check").fetchall()
                ):
                    raise RuntimeError("Reset backup database failed integrity checks")
                for value, size in db.execute("SELECT hash,bytes FROM artifacts"):
                    member = manifest["files"].get("artifacts/runtime/objects/" + value)
                    if member != {"sha256": value, "bytes": size}:
                        raise RuntimeError("Reset backup lacks a registered original: " + value)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "verified": True,
        "sha256": digest(path),
        "bytes": path.stat().st_size,
        "members": len(names),
        "original_artifacts": manifest["counts"]["artifacts"],
    }


def backup(config, state, directory):
    source = config.workspace / "bloom.sqlite3"
    snapshot = directory / "database-before.sqlite3"
    # No open runtime/archive/store is permitted. Remove WAL state before rename.
    with contextlib.closing(sqlite3.connect(source)) as db:
        if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            raise RuntimeError("Database checkpoint is busy; reset did not proceed")
        if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise RuntimeError("Could not settle database sidecars")
        with contextlib.closing(sqlite3.connect(snapshot)) as target:
            db.backup(target)
    os.chmod(snapshot, 0o600)
    with snapshot.open("rb") as stream:
        os.fsync(stream.fileno())
    entries = inventory(config.root, state["reset_paths"])
    entries["workspace/bloom.sqlite3"] = {"sha256": digest(source), "bytes": source.stat().st_size}
    final = directory / "backup.zip"
    temporary = directory / "backup.pending.zip"
    manifest = {
        "format": "bloom-session-backup-1",
        "root": str(config.root),
        "reset_id": state["id"],
        "created_at": now(),
        "counts": state["before"],
        "files": {},
        "note": "Original SQLite plus all registered payloads. No code, credentials, configuration or OS permission reset.",
    }
    with zipfile.ZipFile(
        temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as bundle:
        os.chmod(temporary, 0o600)
        for name, meta in entries.items():
            if meta.get("directory"):
                continue
            path = snapshot if name == "workspace/bloom.sqlite3" else config.root / name
            bundle.write(path, name)
            manifest["files"][name] = {"sha256": digest(path), "bytes": path.stat().st_size}
        with reader(config, snapshot) as store:
            for row in store.rows("SELECT hash,bytes FROM artifacts ORDER BY hash"):
                raw = store.bytes(row["hash"], touch=False, cache=False)
                if len(raw) != row["bytes"]:
                    raise RuntimeError("Original payload size differs from SQLite")
                name = "artifacts/runtime/objects/" + row["hash"]
                bundle.writestr(name, raw)
                manifest["files"][name] = {"sha256": row["hash"], "bytes": row["bytes"]}
        bundle.writestr("manifest.json", encoded(manifest))
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, final)
    sync_directory(directory)
    verified = verify_bundle(final)
    return entries, dict(verified, path=str(final))


def fresh_database(config, state, directory):
    if state.get("rewind"):
        from .rewind import prepare_database

        return prepare_database(config, state, directory)
    path = directory / "database-fresh.sqlite3"
    # A failed preparation is safe to rebuild: publication has not started.
    path.unlink(missing_ok=True)
    with (
        contextlib.closing(sqlite3.connect(path)) as db,
        db,
        reader(config, directory / "database-before.sqlite3") as old,
    ):
        db.executescript(Path(__file__).with_name("schema.sql").read_text())
        db.execute("INSERT INTO schema_versions VALUES(1,?)", (now(),))
        db.execute("INSERT INTO artifact_layout VALUES(1,2)")
        db.execute(
            "INSERT INTO agents VALUES('astra','astra',NULL,'Astra','full',1,?,?)",
            (str(config.root), now()),
        )
        db.execute("INSERT INTO sessions VALUES('astra','astra',?)", (now(),))
        for table in CATALOG:
            if not old.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)):
                continue
            cursor = old.db.execute("SELECT * FROM " + table)
            slots = ",".join("?" for _ in cursor.description)
            db.executemany("INSERT INTO " + table + " VALUES(" + slots + ")", cursor)
        for row in old.db.execute(
            "SELECT c.id,a.path,c.text_zlib FROM archive_chunks c JOIN archive_files a ON a.id=c.file_id"
        ):
            db.execute(
                "INSERT INTO archive_search(rowid,path,text) VALUES(?,?,?)",
                (row[0], row[1], zlib.decompress(row[2]).decode()),
            )
        # Use a durable reset-time cutoff, rather than dropping messages arriving
        # during the backup/restart by asking Discord for its latest on startup.
        if config.data["discord"]["enabled"]:
            address = config.data["discord"]["channel_id"]
            old_cursor = old.one(
                "SELECT message_id FROM discord_cursors WHERE address=?", (address,)
            )
            cutoff = max(
                state["discord_cutoff"], int(old_cursor["message_id"]) if old_cursor else 0
            )
            db.execute("INSERT INTO discord_cursors VALUES(?,?,?)", (address, str(cutoff), now()))
    os.chmod(path, 0o600)
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    sync_directory(directory)
    return digest(path)


@contextlib.contextmanager
def reset_locks(config):
    config.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.ExitStack() as stack:
        for relative in (
            "workspace/reset.lock",
            "workspace/session.lock",
            "workspace/runtime.lock",
            "workspace/archive.lock",
            "workspace/call-views.lock",
            "workspace/desktop.lock",
            "workspace/web/safari.lock",
        ):
            path = checked(config.root, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            file = stack.enter_context(path.open("a"))
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "Workspace is in use; reset needs exclusive " + relative
                ) from exc
        yield


def ensure_stopped(config):
    from .service import LABEL, process_identity
    import subprocess

    for label in (LABEL, LABEL + ".archive"):
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/" + label], capture_output=True
        )
        if result.returncode == 0:
            # A different workspace's same-named service does not own this root.
            from .service import installed_path

            try:
                installed_path(config.root, label=label)
            except RuntimeError:
                continue
            raise RuntimeError("Unload the owned service before reset: " + label)
    heartbeat = config.workspace / "runtime.json"
    if heartbeat.exists():
        row = json.loads(heartbeat.read_text())
        if row.get("start_identity") and process_identity(row["pid"]) == row["start_identity"]:
            raise RuntimeError("Recorded runtime is still alive; reset refused")
    # Older releases predate session.lock. Refuse other open database handles too.
    path = config.workspace / "bloom.sqlite3"
    if path.exists():
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-F", "p", "--", str(path)], capture_output=True
        )
        if result.returncode not in (0, 1) or result.stderr:
            raise RuntimeError("Cannot establish exclusive database ownership")
        holders = [
            line[1:].decode() for line in result.stdout.splitlines() if line.startswith(b"p")
        ]
        if holders:
            raise RuntimeError(
                "Another process still has the database open; PIDs: " + ", ".join(holders)
            )


def publish(config, state, directory):
    retired = directory / "retired"
    for relative in ["workspace/bloom.sqlite3", *state["reset_paths"]]:
        if relative not in state["inventory"]:
            continue
        source = checked(config.root, relative)
        target = checked(config.root, str((retired / relative).relative_to(config.root)))
        wanted = {
            name: meta
            for name, meta in state["inventory"].items()
            if name == relative or name.startswith(relative + "/")
        }
        if target.exists():
            actual = inventory(retired, [relative])
            if actual != wanted:
                raise RuntimeError("Retired state changed during reset: " + relative)
            if source.exists() and not (
                relative == "workspace/bloom.sqlite3" and digest(source) == state["fresh_sha256"]
            ):
                raise RuntimeError("New state appeared during reset: " + relative)
            continue
        if inventory(config.root, [relative]) != wanted:
            raise RuntimeError("Source changed after backup: " + relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        sync_directory(source.parent)
        sync_directory(target.parent)
    source = directory / "database-fresh.sqlite3"
    target = config.workspace / "bloom.sqlite3"
    if target.exists():
        if digest(target) != state["fresh_sha256"]:
            raise RuntimeError("Reset database differs from prepared state")
    else:
        if digest(source) != state["fresh_sha256"]:
            raise RuntimeError("Prepared reset database changed")
        os.replace(source, target)
        sync_directory(source.parent)
        sync_directory(target.parent)
    with reader(config) as store:
        after = counts(store)
        if state.get("rewind"):
            from .rewind import verify_database

            verify_database(store, state["rewind"])
        elif any(after[name] for name in EMPTY) or after["agents"] != 1 or after["sessions"] != 1:
            raise RuntimeError("Reset did not create an empty Astra session")
        if any(after[name] != state["before"][name] for name in CATALOG):
            raise RuntimeError("Reset lost archive catalog entries")
        if (
            store.rows("PRAGMA foreign_key_check")
            or store.one("PRAGMA integrity_check")["integrity_check"] != "ok"
        ):
            raise RuntimeError("Fresh database failed integrity checks")
    return after


def apply(config, *, allow_terminal_records=False, rewind=None):
    """Requires stopped services and all Store handles closed; resumes its intent."""
    with reset_locks(config):
        ensure_stopped(config)
        pending = checked(config.root, "workspace/reset-pending.json")
        if pending.exists():
            state = json.loads(pending.read_text())
            if state["root"] != str(config.root) or not re.fullmatch(
                r"local-\d{8}_\d{6}-[0-9a-f]{8}", state["id"]
            ):
                raise ValueError("Reset intent belongs to another root or has an invalid ID")
            if state.get("rewind") != rewind:
                raise ValueError("Resume the exact pending reset/rewind intent")
        else:
            before = inspect(config, allow_terminal_records=allow_terminal_records)
            if not before["ready"]:
                raise RuntimeError(
                    "Settle unfinished/uncertain work before reset: "
                    + json.dumps(before["blockers"])
                )
            identity = time.strftime("local-%Y%m%d_%H%M%S") + "-" + uuid.uuid4().hex[:8]
            state = {
                "id": identity,
                "root": str(config.root),
                "phase": "preparing",
                "started_at": now(),
                "before": before["counts"],
                "reset_paths": before["reset_paths"],
                "allow_terminal_records": allow_terminal_records,
                "retained_terminal_records": before["retained_terminal_records"],
                "discord_cutoff": max(0, int(time.time() * 1000) - 1420070400000) << 22,
            }
            if rewind:
                from .rewind import specification

                if specification(config, rewind["item"], rewind["request_id"]) != rewind:
                    raise ValueError("Rewind boundary changed since inspection")
                state["rewind"] = rewind
                # Later files/effects remain. Retire only authoring and derived views.
                state["reset_paths"] = [
                    "memories",
                    "workspace/requests",
                    "workspace/authoring",
                    "workspace/receipts",
                    "workspace/call-views.json",
                    "artifacts/runtime/calls",
                ]
            atomic(pending, encoded(state))
        directory = checked(config.root, "artifacts/session-resets/" + state["id"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if state["phase"] == "preparing":
            state["inventory"], state["backup"] = backup(config, state, directory)
            state["fresh_sha256"] = fresh_database(config, state, directory)
            state["phase"] = "publishing"
            atomic(pending, encoded(state))
        verification = verify_bundle(Path(state["backup"]["path"]))
        if verification["sha256"] != state["backup"]["sha256"]:
            raise RuntimeError("Verified reset backup was replaced")
        state["after"] = publish(config, state, directory)
        state.update(phase="completed", completed_at=now())
        atomic(config.workspace / "resets" / (state["id"] + ".json"), encoded(state))
        atomic(
            config.workspace / "session.json",
            encoded(
                {
                    "reset_id": state["id"],
                    "started_at": state["started_at"],
                    "backup": state["backup"],
                    "discord_cutoff": state["discord_cutoff"],
                }
            ),
        )
        pending.unlink()
        sync_directory(pending.parent)
        return {
            k: state[k]
            for k in ("id", "phase", "started_at", "completed_at", "backup", "before", "after")
        } | {"retained_terminal_records": state.get("retained_terminal_records", {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BLOOM_ROOT", str(Path(__file__).resolve().parents[2]))),
    )
    parser.add_argument("action", choices=("plan", "apply", "verify"))
    parser.add_argument("--backup", type=Path)
    parser.add_argument(
        "--allow-terminal-records",
        action="store_true",
        help="Explicit owner reset: back up terminal failed/blocked records and preserve finished worktree files; active/uncertain effects still block",
    )
    args = parser.parse_args()
    if args.action == "verify":
        if not args.backup:
            parser.error("verify requires --backup")
        result = verify_bundle(args.backup)
    else:
        config = Config(args.root)
        result = (
            inspect(config, allow_terminal_records=args.allow_terminal_records)
            if args.action == "plan"
            else apply(config, allow_terminal_records=args.allow_terminal_records)
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
