"""Bounded, rebuildable human views over authoritative model-call records."""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

from .privacy import clean
from .storage import ColdStorageUnavailable
from .store import Store, atomic, encoded, now

SCHEMA = "bloom-call-view-1"
POOLS = (
    (
        "main",
        "a.role='astra' AND c.purpose NOT IN ('curation','search','source_summary')",
        "recent_main_calls",
    ),
    (
        "workers",
        "(a.role!='astra' OR c.purpose IN ('curation','search','source_summary'))",
        "recent_worker_calls",
    ),
)


def slug(value, limit=48):
    return (
        re.sub(r"[^a-z0-9]+", "-", clean(str(value)).lower()).strip("-")[:limit].rstrip("-")
        or "untitled"
    )


def safe_id(value):
    if not re.fullmatch("[A-Za-z0-9_-]+", value):
        raise ValueError("Unsafe identifier in call view")
    return value


def view_path(pool, row):
    try:
        stamp = dt.datetime.fromisoformat(row["started_at"]).astimezone(dt.timezone.utc)
        day, clock = stamp.strftime("%Y-%m-%d"), stamp.strftime("%H%M%S")
    except ValueError:
        day, clock = "undated", "unknown-time"
    task = slug(row["task_title"] or "session") + "--" + safe_id(row["task_id"] or "session")
    agent = slug(row["agent_name"]) + "--" + safe_id(row["agent_id"])
    call = clock + "-" + slug(row["purpose"], 24) + "--" + safe_id(row["id"])
    return str(Path(pool) / day / task / agent / call)


def write_changed(path, data):
    if not path.exists() or path.read_bytes() != data:
        atomic(path, data)


def checked_path(base, relative):
    path = base / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Call view path escapes its directory")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Call views refuse symbolic paths")
        if parent == base:
            break
    return path


def original(store, digest, allow_cold):
    if not allow_cold and not any(p.is_file() for p in store.artifact_paths(digest)):
        raise ColdStorageUnavailable(
            "Original is cold; run export-calls with the RAID connected to include it"
        )
    return store.bytes(digest, touch=False, cache=False)


def durable(store, row, allow_cold):
    try:
        for key in ("request_hash", "response_hash"):
            if row[key]:
                original(store, row[key], allow_cold)
        return True
    except ColdStorageUnavailable:
        return False


def render_call(store, target, row, pool, allow_cold):
    signature = hashlib.sha256(encoded(row)).hexdigest()
    receipt_path = target / "receipt.json"
    try:
        previous = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    except json.JSONDecodeError:
        previous = {}  # This is a derived view and can be reconstructed.
    # A stable call needs only small receipt/stat reads, not repeated large
    # prompt reads or writes. Edited/missing views are rebuilt on the next pass.
    if previous.get("source_signature") == signature and previous.get("schema") == SCHEMA:
        intact = True
        for name, info in previous.get("files", {}).items():
            path = checked_path(target, name)
            if info["availability"] != "local" or not path.exists():
                intact = False
                break
            stat = path.stat()
            if [stat.st_size, stat.st_mtime_ns] != info["stat"]:
                intact = False
                break
        if intact:
            return previous
    receipt = dict(
        row,
        schema=SCHEMA,
        pool=pool,
        source_signature=signature,
        source_of_truth="workspace/bloom.sqlite3 and immutable artifact originals",
        files={},
    )
    for kind in ("request", "response"):
        digest = row[kind + "_hash"]
        if not digest:
            continue  # Failed/prepared calls must not acquire a fake response.
        path = checked_path(target, kind + ".json")
        info = {"artifact_hash": digest}
        try:
            # A verified existing projection remains useful while the RAID is
            # offline; never overwrite it with an availability placeholder.
            data = path.read_bytes() if path.exists() else None
            try:
                matches = (
                    data is not None
                    and hashlib.sha256(encoded(json.loads(data))).hexdigest() == digest
                )
            except (ValueError, UnicodeError):
                matches = False
            if not matches:
                data = (
                    json.dumps(
                        json.loads(original(store, digest, allow_cold)),
                        ensure_ascii=False,
                        indent=2,
                    ).encode()
                    + b"\n"
                )
                write_changed(path, data)
            stat = path.stat()
            info.update(
                availability="local",
                view_sha256=hashlib.sha256(data).hexdigest(),
                stat=[stat.st_size, stat.st_mtime_ns],
            )
        except ColdStorageUnavailable as exc:
            info.update(availability="cold", detail=str(exc))
        receipt["files"][kind + ".json"] = info
    write_changed(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2).encode() + b"\n")
    return receipt


def remove_view(base, entry):
    target = checked_path(base, entry["path"])
    if not target.exists():
        return True
    permitted = {"request.json", "response.json", "receipt.json"}
    children = list(target.iterdir())
    if any(p.name not in permitted or not p.is_file() or p.is_symlink() for p in children):
        return False  # Preserve user-added material; surface deferred cleanup.
    receipt = target / "receipt.json"
    if receipt.exists() and json.loads(receipt.read_text()).get("id") != entry["id"]:
        raise RuntimeError("Call view receipt does not match its managed identity")
    for path in children:
        path.unlink()
    for directory in (target, *target.parents):
        if directory == base:
            break
        try:
            directory.rmdir()
        except OSError:
            break
    return True


def export_calls(store, *, allow_cold=True):
    base = store.config.artifacts / "runtime/calls"
    checked_path(store.config.artifacts, "runtime/calls")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (store.config.workspace / "call-views.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running"}
        manifest = store.config.workspace / "call-views.json"
        prior = json.loads(manifest.read_text()) if manifest.exists() else {"entries": []}
        selected = []
        for pool, condition, setting in POOLS:
            limit = store.config.data["runtime"][setting]
            if type(limit) is not int or limit < 0:
                raise ValueError("Recent call limits must be nonnegative integers")
            rows = store.rows(
                f"SELECT c.*,a.name AS agent_name,a.role AS agent_role,t.objective AS task_title FROM model_calls c JOIN agents a ON a.id=c.agent_id LEFT JOIN tasks t ON t.id=c.task_id WHERE {condition} ORDER BY c.rowid DESC LIMIT ?",
                (limit,),
            )
            selected.extend(
                (pool, row, {"id": row["id"], "path": view_path(pool, row)}) for row in rows
            )
        planned = {e["path"]: e for e in prior["entries"]}
        planned.update((entry["path"], entry) for _, _, entry in selected)
        atomic(
            manifest,
            encoded({"schema": SCHEMA, "status": "updating", "entries": list(planned.values())}),
        )
        index = []
        for pool, row, entry in selected:
            receipt = render_call(store, checked_path(base, entry["path"]), row, pool, allow_cold)
            index.append(
                dict(
                    entry,
                    pool=pool,
                    task=row["task_title"],
                    agent=row["agent_name"],
                    started_at=row["started_at"],
                    status=row["status"],
                    model=row["model_requested"],
                    availability={k: v["availability"] for k, v in receipt["files"].items()},
                )
            )
        keep = {entry["path"] for _, _, entry in selected}
        deferred = []
        for path, entry in list(planned.items()):
            if path in keep:
                continue
            row = store.one("SELECT * FROM model_calls WHERE id=?", (entry["id"],))
            if not row:
                raise RuntimeError("Cannot prune a call view without its durable record")
            if durable(store, row, allow_cold) and remove_view(base, entry):
                del planned[path]
            else:
                deferred.append(entry)
        # Retire only recognizable old projections after validating originals.
        legacy_removed = 0
        legacy = store.config.root / "logs/model-calls"
        for pool, _, _ in POOLS:
            folder = checked_path(legacy, pool)
            for path in folder.glob("call_*.json"):
                if path.is_symlink():
                    raise ValueError("Legacy call projection is a symlink")
                value = json.loads(path.read_text())
                row = store.one("SELECT * FROM model_calls WHERE id=?", (value.get("id"),))
                if not row or path.name != row["id"] + ".json":
                    continue
                if any(value.get(key) != row[key] for key in ("request_hash", "response_hash")):
                    continue
                if durable(store, row, allow_cold):
                    path.unlink()
                    legacy_removed += 1
            if folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
        if legacy.exists() and not any(legacy.iterdir()):
            legacy.rmdir()
        result = {
            "schema": SCHEMA,
            "status": "completed",
            "updated_at": now(),
            "recent_calls": len(selected),
            "recent_json_files": len(selected),
            "pools": {pool: sum(p == pool for p, _, _ in selected) for pool, _, _ in POOLS},
            "limits": {pool: store.config.data["runtime"][setting] for pool, _, setting in POOLS},
            "originals_retained": True,
            "prune_deferred": deferred,
            "legacy_views_removed": legacy_removed,
            "directory": str(base),
            "calls": index,
        }
        write_changed(
            base / "index.json", json.dumps(result, ensure_ascii=False, indent=2).encode() + b"\n"
        )
        lines = [
            "# Recent model calls",
            "",
            "Generated inspection views; SQLite and immutable originals remain authoritative.",
            "Dates/folder times use UTC. Requests/responses are native JSON with readable formatting.",
            "Every physical attempt has its own folder. Missing responses and cold originals are explicit in receipts.",
            "",
            "| Started | Pool | Task | Agent | Status | Call |",
            "|---|---|---|---|---|---|",
        ]

        def cell(value):
            return clean(str(value or "session")).replace("|", "\\|").replace("\n", " ")[:100]

        for row in index:
            lines.append(
                "| "
                + " | ".join(
                    cell(row[k]) for k in ("started_at", "pool", "task", "agent", "status")
                )
                + " | [receipt]("
                + quote(row["path"] + "/receipt.json")
                + ") |"
            )
        if deferred:
            lines += [
                "",
                f"{len(deferred)} older views retained pending original verification or user-added-file review.",
            ]
        write_changed(base / "README.md", ("\n".join(lines) + "\n").encode())
        write_changed(
            store.config.artifacts / "README.md",
            b"# Artifacts\n\n- [Runtime call index](runtime/calls/README.md): named, bounded views of main and worker calls.\n- `runtime/objects/`: immutable internal payloads, addressed by hash.\n- `releases/`: frozen executable source and rollback snapshots.\n- Other named task/run directories: requested outputs and supporting evidence; their notes explain provenance.\n\nSQLite is authoritative. Generated call views are rebuildable; originals and archive receipts are retained.\n",
        )
        atomic(
            manifest,
            encoded(
                {
                    "schema": SCHEMA,
                    "status": "completed",
                    "entries": list(planned.values()),
                    "updated_at": result["updated_at"],
                }
            ),
        )
        return {k: v for k, v in result.items() if k != "calls"}


def refresh(config):
    store = Store(config)
    try:
        return export_calls(store, allow_cold=False)
    finally:
        store.close()
