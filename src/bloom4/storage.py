"""Verified cold payloads and portable backups; never replace a disconnected mount."""

import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import zipfile

from .agents import Agents
from .memory import Memory
from .store import Store, atomic, encoded, now, uid


def connected(path):
    """Require an existing destination and the actual removable-volume mount."""
    path = Path(path)
    if len(path.parts) > 2 and path.parts[1] == "Volumes":
        if not Path(*path.parts[:3]).is_mount():
            return False
    return path.is_dir()


def references(value):
    if isinstance(value, dict):
        if "sha256" in value and "path" in value:
            yield value["sha256"]
        for item in value.values(): yield from references(item)
    elif isinstance(value, list):
        for item in value: yield from references(item)


class Archive:
    """Offload inactive payloads after verified gzip copies; active state stays local."""

    def __init__(self, store: Store):
        self.store = store

    def configure(self, destination, days=7):
        destination = Path(destination).expanduser().absolute()
        if not connected(destination):
            raise FileNotFoundError("Connect/create the intended archive destination first; no substitute created")
        if days < 0: raise ValueError("Inactivity days cannot be negative")
        return self.store.append("archive.configured", {"destination": str(destination), "days": days}, actor="owner")

    def pins(self):
        result = set()
        agents = Agents(self.store)
        for agent in agents.all():
            if agent.persistent:
                result.update(references(Memory(self.store).packet(agent.id)))
                snapshots = self.store.events("context.snapshot", target=agent.id)
                through = snapshots[-1]["data"]["through"] if snapshots else 0
                for event in self.store.events(target=agent.id, after=through):
                    if event["kind"] in ("context.snapshot", "model.completed", "model.started", "history.added", "tool.completed"):
                        result.update(references(event["data"]))
        for task in self.store.index.tasks().values():
            if task["status"] != "completed":
                for event in self.store.events(task=task["id"]): result.update(references(event["data"]))
        return result

    def sweep(self):
        configured = self.store.events("archive.configured")
        if not configured: return {"status": "unconfigured", "moved": 0}
        config = configured[-1]["data"]; destination = Path(config["destination"])
        if not connected(destination):
            return {"status": "deferred", "reason": "Archive destination disconnected", "moved": 0}
        pins = self.pins(); cutoff = time.time() - config["days"] * 86400
        moved = 0
        for path in sorted((self.store.root / "objects").iterdir()):
            if path.name.startswith(".") or path.name in pins or path.stat().st_mtime >= cutoff:
                continue
            stamp = path.stat().st_mtime_ns
            data = path.read_bytes(); digest = hashlib.sha256(data).hexdigest()
            if digest != path.name: raise ValueError("Refusing to archive corrupt original")
            target = destination / (digest + ".gz")
            if not target.exists():
                atomic(target, gzip.compress(data, mtime=0))
            if hashlib.sha256(gzip.decompress(target.read_bytes())).hexdigest() != digest:
                raise ValueError("Cold copy failed verification; original retained")
            receipt = self.store.append("archive.receipt", {"sha256": digest, "cold_path": str(target),
                "bytes": len(data), "verified_at": now(), "source": str(path.relative_to(self.store.root))})
            with self.store.locked():
                if path.exists() and path.stat().st_mtime_ns == stamp and connected(destination) and path.name not in self.pins():
                    path.unlink(); moved += 1
                    for event in self.store.events("result.saved"):
                        if event["data"]["source"]["sha256"] == digest:
                            named = Path(event["data"]["path"])
                            if named.is_relative_to(self.store.root / "results") and named.exists() and hashlib.sha256(named.read_bytes()).hexdigest() == digest:
                                named.unlink()
        return {"status": "completed", "moved": moved}

    def due(self):
        if not self.store.events("archive.configured"): return None
        checks = self.store.events("archive.checked")
        if checks and time.time() - checks[-1]["data"]["checked_at"] < 3600: return None
        try: result = self.sweep()
        except (OSError, ValueError) as exc:
            result = {"status": "deferred", "reason": type(exc).__name__ + ": " + str(exc)}
        self.store.append("archive.checked", {**result, "checked_at": time.time()})
        return result


def backup(store, destination):
    """Bundle committed workspace files and cold originals; require an offline workspace."""
    destination = Path(destination).expanduser().resolve()
    if destination.exists(): raise FileExistsError("Backup destination already exists")
    if destination.is_relative_to(store.root): raise ValueError("Write the backup outside its source workspace")
    lock = (store.root / ".runtime.lock").open("a+")
    try:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError("Stop the workspace runtime before backing it up")
        for spec in (store.root / "processes").glob("*/spec.json"):
            if not spec.with_name("done.json").exists():
                raise RuntimeError("Settle detached processes before a consistent backup")
        with store.locked():
            manifest = {}; destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name("." + destination.name + "." + uid("backup"))
            try:
                with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted(store.root.rglob("*")):
                        if path.is_symlink(): raise ValueError("Backup refuses symlinks; retain an explicit source reference")
                        if not path.is_file() or path.name.startswith("index.sqlite3") or path.name in (".writer.lock", ".runtime.lock", "status.json"):
                            continue
                        relative = str(path.relative_to(store.root)); data = path.read_bytes()
                        archive.writestr(relative, data); manifest[relative] = hashlib.sha256(data).hexdigest()
                    for event in store.events("archive.receipt"):
                        digest = event["data"]["sha256"]; relative = "objects/" + digest
                        if relative in manifest: continue
                        data = store.read({"sha256": digest})
                        archive.writestr(relative, data); manifest[relative] = hashlib.sha256(data).hexdigest()
                    archive.writestr("backup-manifest.json", encoded({"files": manifest, "created_at": now()}))
                verify_backup(temporary)
                os.chmod(temporary, 0o600); os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return {"path": str(destination), "files": len(manifest), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
    finally:
        lock.close()


def verify_backup(path):
    """Verify every member and reject traversal or duplicate names before extraction."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names): raise ValueError("Duplicate archive member")
        manifest = json.loads(archive.read("backup-manifest.json"))
        if set(names) != set(manifest["files"]) | {"backup-manifest.json"}:
            raise ValueError("Archive membership differs from manifest")
        for name, digest in manifest["files"].items():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("Unsafe archive path")
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise ValueError("Backup hash mismatch: " + name)
        return manifest


def restore(path, destination):
    """Restore into a new directory and reconstruct the index without executing work."""
    destination = Path(destination).expanduser().resolve()
    if destination.exists(): raise FileExistsError("Restore requires a new workspace directory")
    verify_backup(path)
    destination.mkdir(parents=True, mode=0o700)
    with zipfile.ZipFile(path) as archive:
        archive.extractall(destination)
    store = Store(destination)
    try:
        store.sync(rebuild=True)
        return {"workspace": str(destination), "events": len(store.events()), "executed": False}
    finally: store.close()
