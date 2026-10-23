"""Weekly, independently verifiable full conversation bundles; no runtime reset."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import zipfile

from .reset import counts, digest, reader, verify_bundle
from .storage import Archive, sync_directory
from .store import atomic, encoded, now, uid


class Backups:
    def __init__(self, archive):
        self.archive = archive
        self.store = archive.store
        self.config = archive.config

    def complete(self, row, verified):
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE conversation_backups SET status='completed',completed_at=?,sha256=?,bytes=?,artifact_count=?,error=NULL WHERE id=?",
                (
                    now(),
                    verified["sha256"],
                    verified["bytes"],
                    verified["original_artifacts"],
                    row["id"],
                ),
            )
            self.store.event(
                "backup.verified", {"backup": row["id"], "path": row["path"], **verified}
            )
        result = self.store.one("SELECT * FROM conversation_backups WHERE id=?", (row["id"],))
        atomic(self.config.workspace / "backups/latest.json", encoded(result))
        return result

    def run_if_due(self):
        if not self.config.data.get("storage", {}).get("weekly_backups", True):
            return {"status": "disabled"}
        with (self.config.workspace / "archive.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "already_running"}
            # A disconnected RAID is never replaced by an SSD shadow directory.
            if not self.archive.volume_ready():
                return {"status": "waiting_for_volume"}
            for row in self.store.rows(
                "SELECT * FROM conversation_backups WHERE status='creating'"
            ):
                if Path(row["path"]).is_file():
                    try:
                        self.complete(row, verify_bundle(Path(row["path"])))
                    except Exception as exc:
                        self.store.db.execute(
                            "UPDATE conversation_backups SET status='failed',error=? WHERE id=?",
                            (type(exc).__name__ + ": retained incomplete bundle", row["id"]),
                        )
                else:
                    self.store.db.execute(
                        "UPDATE conversation_backups SET status='interrupted',error='Incomplete attempt retained; no verified bundle published' WHERE id=?",
                        (row["id"],),
                    )
            latest = self.store.one(
                "SELECT * FROM conversation_backups WHERE status='completed' ORDER BY completed_at DESC LIMIT 1"
            )
            if (
                latest
                and (
                    dt.datetime.now(dt.timezone.utc)
                    - dt.datetime.fromisoformat(latest["completed_at"])
                ).total_seconds()
                < 7 * 86400
            ):
                return {"status": "not_due", "latest": latest}
            size = (
                self.store.one("SELECT coalesce(sum(bytes),0) bytes FROM artifacts")["bytes"]
                + 2 * self.store.path.stat().st_size
            )
            if shutil.disk_usage(self.config.root).free < size + 256 * 1024**2:
                return {
                    "status": "waiting_for_space",
                    "estimated_required_bytes": size + 256 * 1024**2,
                }
            ident = (
                "weekly-"
                + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + "-"
                + uid("snapshot")[-8:]
            )
            directory = self.config.artifacts / "backups" / ident
            directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            final = directory / "conversation.zip"
            pending = directory / "conversation.pending.zip"
            snapshot = directory / "snapshot.sqlite3"
            self.store.db.execute(
                "INSERT INTO conversation_backups VALUES(?,?,?,?,NULL,NULL,NULL,NULL,NULL)",
                (ident, str(final), "creating", now()),
            )
            try:
                # Online backup API yields a coherent WAL snapshot without
                # blocking new work or changing the live journal mode.
                with contextlib.closing(sqlite3.connect(snapshot)) as target:
                    self.store.db.backup(target, pages=256, sleep=0.05)
                os.chmod(snapshot, 0o600)
                with reader(self.config, snapshot) as saved:
                    manifest = {
                        "format": "bloom-conversation-backup-1",
                        "root": str(self.config.root),
                        "backup_id": ident,
                        "created_at": now(),
                        "source": str(self.config.source),
                        "counts": counts(saved),
                        "files": {},
                        "coverage": "Consistent SQLite plus every registered immutable original, including full requests/responses/tool evidence/images. Named mutable project files, credentials and code are not included. Original path/hash mappings remain in SQLite; archive catalog/search indexes are retained.",
                    }
                    with zipfile.ZipFile(
                        pending, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6
                    ) as bundle:
                        os.chmod(pending, 0o600)
                        name = "workspace/bloom.sqlite3"
                        bundle.write(snapshot, name)
                        manifest["files"][name] = {
                            "sha256": digest(snapshot),
                            "bytes": snapshot.stat().st_size,
                        }
                        for row in saved.rows("SELECT hash,bytes FROM artifacts ORDER BY hash"):
                            raw = saved.bytes(row["hash"], touch=False, cache=False)
                            if len(raw) != row["bytes"]:
                                raise RuntimeError("Original size differs from retained receipt")
                            name = "artifacts/runtime/objects/" + row["hash"]
                            bundle.writestr(name, raw)
                            manifest["files"][name] = {"sha256": row["hash"], "bytes": row["bytes"]}
                        bundle.writestr("manifest.json", encoded(manifest))
                with pending.open("rb") as stream:
                    os.fsync(stream.fileno())
                verified = verify_bundle(pending)
                self.archive.require_volume()
                os.replace(pending, final)
                sync_directory(directory)
                row = self.store.one("SELECT * FROM conversation_backups WHERE id=?", (ident,))
                result = self.complete(row, verified)
                snapshot.unlink()
                sync_directory(directory)
                return result
            except Exception as exc:
                from .diagnostics import label

                self.store.db.execute(
                    "UPDATE conversation_backups SET status='failed',error=? WHERE id=?",
                    (label(exc), ident),
                )
                return {
                    "status": "failed",
                    "backup": ident,
                    "error": label(exc),
                    "retained_directory": str(directory),
                }


def main():
    from .config import Config
    from .store import Store

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BLOOM_ROOT", Path(__file__).resolve().parents[2])),
    )
    parser.add_argument("action", choices=("create", "status", "verify"))
    parser.add_argument("--path", type=Path)
    args = parser.parse_args()
    if args.action == "verify":
        if not args.path:
            parser.error("verify requires --path")
        result = verify_bundle(args.path)
    else:
        with contextlib.closing(Store(Config(args.root))) as store:
            result = (
                Backups(Archive(store)).run_if_due()
                if args.action == "create"
                else store.rows("SELECT * FROM conversation_backups ORDER BY created_at DESC")
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and result.get("status") == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
