"""Consume owner-authored TOML; SQLite owns acceptance and idempotency."""

from __future__ import annotations

import hashlib
import os
import stat
import tomllib

from .privacy import clean, StreamRedactor
from .store import now, uid


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def accept(runtime, raw, source):
    store = runtime.store
    digest = store.put(StreamRedactor().feed(raw, final=True), "application/toml")
    ident, operation = "content_" + digest, "invalid"
    try:
        request = clean(tomllib.loads(raw.decode()))
        ident = request.get("request_id", ident)
        if not isinstance(ident, str) or not ident.strip():
            ident = "invalid_" + digest
            raise ValueError("request_id must be a nonempty string")
        previous = store.one("SELECT * FROM owner_requests WHERE id=?", (ident,))
        if previous:
            if previous["source_hash"] != digest:
                store.event(
                    "owner.request_conflict",
                    {"request_id": ident, "rejected_source": digest, "source_path": source},
                )
            return  # A retry never executes its accepted effect again.
        operation = request["operation"]
        fields = {
            "message": {"text"},
            "steer": {"task", "text"},
            "pause": {"task"},
            "resume": {"task"},
        }
        if operation not in fields or set(request) - fields[operation] - {
            "operation",
            "request_id",
        }:
            raise ValueError("Unknown owner command or fields")
        if "text" in fields[operation] and (
            not isinstance(request.get("text"), str) or not request["text"].strip()
        ):
            raise ValueError("text must be nonempty")
        if "task" in fields[operation] and (
            not isinstance(request.get("task"), str)
            or not store.one("SELECT 1 FROM tasks WHERE id=?", (request["task"],))
        ):
            raise ValueError("Unknown task")
        # These four owner effects are database-only. Acceptance and all effects
        # commit together; any exception rolls back both before recording failure.
        with store.transaction():
            if operation == "message":
                task = store.new_task(
                    request["text"], key="task_" + hashlib.sha256(ident.encode()).hexdigest()[:24]
                )
                result = {"task": task}
            elif operation == "steer":
                task = runtime.steer(request["task"], request["text"])
                result = {"task": task}
            else:
                task = request["task"]
                (runtime.pause if operation == "pause" else runtime.resume)(task)
                result = {operation + "d": task}
            store.db.execute(
                "INSERT INTO owner_requests VALUES(?,?,?,?,?,?,?,?)",
                (ident, digest, operation, "completed", task, store.put(result), source, now()),
            )
    except (ValueError, KeyError, TypeError) as exc:
        # Validation failures are durable too. Unexpected database/I/O failures
        # leave the claimed file for retry rather than pretending it was consumed.
        with store.transaction():
            store.db.execute(
                "INSERT OR IGNORE INTO owner_requests VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    digest,
                    clean(str(operation)),
                    "failed",
                    None,
                    store.put({"error": clean(str(exc))}),
                    source,
                    now(),
                ),
            )


def drain(runtime):
    directory = runtime.config.workspace / "requests"
    processing = directory / "processing"
    processing.mkdir(exist_ok=True)
    # Claim by atomic rename before reading. A restart drains claims first. The
    # producer publishes complete files with atomic(), never edits claimed files.
    pending = sorted(directory.glob("*.toml"), key=lambda p: (p.lstat().st_mtime_ns, p.name))
    claimed = sorted(processing.glob("*.toml"), key=lambda p: (p.lstat().st_mtime_ns, p.name))
    for path in (claimed + pending)[:64]:
        if path.parent == directory:
            target = processing / (uid("claim") + "--" + path.name)
            os.rename(path, target)
            sync_directory(directory)
            sync_directory(processing)
            path = target
        source = str(directory / path.name.partition("--")[2])
        if not stat.S_ISREG(path.lstat().st_mode):
            rejected = directory / "rejected"
            rejected.mkdir(exist_ok=True)
            os.rename(path, rejected / path.name)
            sync_directory(rejected)
            sync_directory(processing)
            runtime.store.event(
                "owner.request_rejected",
                {
                    "source_path": source,
                    "reason": "Requests must be regular files; retained under requests/rejected",
                },
            )
            continue
        accept(runtime, path.read_bytes(), source)
        # If this unlink is interrupted, the next pass reads the durable receipt.
        path.unlink()
        sync_directory(processing)
