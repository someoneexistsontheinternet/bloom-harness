"""Explicit, resumable relocation of immutable runtime objects."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path

from .store import Store, atomic, encoded, now
from .storage import Archive, sync_directory


def migrate_objects(store):
    """Run with the coordinator and archive schedule stopped, never on startup."""
    with store.runtime_lock(), (store.config.workspace / "archive.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        root = store.config.artifacts
        destination = root / "runtime/objects"
        for path in (root, root / "runtime", destination):
            if path.is_symlink():
                raise ValueError("Object migration refuses symbolic storage directories")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        registered = {r["hash"]: r for r in store.rows("SELECT * FROM artifacts")}
        buckets = sorted(p for p in root.iterdir() if re.fullmatch("[0-9a-f]{2}", p.name))
        # Inspect everything before the first move; unfamiliar files stay intact.
        for bucket in buckets:
            if bucket.is_symlink() or not bucket.is_dir():
                raise ValueError("Unexpected legacy bucket: " + bucket.name)
            for path in bucket.iterdir():
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.name not in registered
                    or path != store.artifact_paths(path.name)[1]
                ):
                    raise ValueError("Unregistered or unsafe legacy entry: " + str(path))
        entries = []
        archive = Archive(store)
        for digest, row in registered.items():
            current, legacy = store.artifact_paths(digest)
            local = []
            for path in (current, legacy):
                if path.is_symlink():
                    raise ValueError("Object migration refuses symbolic payloads")
                if path.exists():
                    data = path.read_bytes()
                    if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != digest:
                        raise RuntimeError("Object failed pre-migration integrity check: " + digest)
                    local.append(path)
            cold = [
                archive.record(str(p.relative_to(store.config.root))) for p in (current, legacy)
            ]
            cold = [r for r in cold if r and r["sha256"] == digest and r["bytes"] == row["bytes"]]
            if not local and not cold:
                raise FileNotFoundError(
                    "Original has no local copy or verified archive catalog entry: " + digest
                )
            entries.append(
                {
                    "hash": digest,
                    "bytes": row["bytes"],
                    "from": str(legacy.relative_to(store.config.root)),
                    "to": str(current.relative_to(store.config.root)),
                    "local_verified": bool(local),
                    "archive_ids": [r["id"] for r in cold],
                }
            )
        journal = store.config.workspace / "artifact-migration.json"
        previous = json.loads(journal.read_text()) if journal.exists() else {}
        receipt = {
            "status": "prepared",
            "started_at": previous.get("started_at", now()),
            "from_version": 1,
            "to_version": 2,
            "objects": entries,
            "archive_receipts_preserved": True,
        }
        atomic(journal, encoded(receipt))  # Intent persists before filesystem effects.
        moved = 0
        for entry in entries:
            current, legacy = store.artifact_paths(entry["hash"])
            if legacy.exists():
                if current.exists():
                    # Both complete copies were verified above. Keep one original.
                    legacy.unlink()
                else:
                    os.replace(legacy, current)
                    sync_directory(current.parent)
                sync_directory(legacy.parent)
                moved += 1
        # Both-layout readers make an interrupted move resumable. Switch writes
        # only after every existing original has a verified destination/receipt.
        store.db.execute("UPDATE artifact_layout SET version=2 WHERE singleton=1")
        for bucket in buckets:
            bucket.rmdir()  # Never recursively delete an unexpected new entry.
        sync_directory(root)
        receipt.update(
            status="completed",
            completed_at=now(),
            moved_this_pass=moved,
            local_objects=sum(e["local_verified"] for e in entries),
        )
        atomic(journal, encoded(receipt))
        return {k: v for k, v in receipt.items() if k != "objects"}


def main():
    from .config import Config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BLOOM_ROOT", Path(__file__).resolve().parents[2])),
    )
    parser.add_argument("action", choices=["migrate"])
    args = parser.parse_args()
    store = Store(Config(args.root))
    try:
        print(json.dumps(migrate_objects(store), indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
