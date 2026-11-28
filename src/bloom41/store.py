"""Immutable file journal and payloads; SQLite is a rebuildable lookup cache."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value):
        raise ValueError("Expected a short identifier containing letters, digits, _ or -")
    return value


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex[:24]


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with pending.open("xb") as stream:
            os.chmod(pending, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        sync_directory(path.parent)
    finally:
        pending.unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.control = self.root / "control"
        self.journal = self.control / "events"
        self.objects = self.root / "artifacts/objects"
        for path in (
            self.journal,
            self.objects,
            self.root / "workspace/agents",
            self.root / "workspace/jobs",
        ):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.control, 0o700)
        self.db = sqlite3.connect(self.root / "index.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY, key TEXT UNIQUE, kind TEXT, subject TEXT,
                at TEXT, data TEXT, path TEXT, digest TEXT);
            CREATE INDEX IF NOT EXISTS subjects ON events(kind,subject,seq);
        """)
        self.journal_stamp = None
        self.sync()

    @contextmanager
    def locked(self):
        with (self.control / "writer.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def sync(self, rebuild=False):
        with self.locked():
            self._sync(rebuild)

    def _sync(self, rebuild=False):
        stamp = self.journal.stat().st_mtime_ns
        if not rebuild and self.journal_stamp == stamp:
            return
        with self.db:
            if rebuild:
                self.db.execute("DELETE FROM events")
            known = {r[0] for r in self.db.execute("SELECT path FROM events")}
            paths = sorted(self.journal.glob("*.json"))
            actual = {p.name for p in paths}
            if known - actual:
                raise ValueError("An indexed journal original is missing")
            for path in paths:
                if path.name in known:
                    continue
                row = json.loads(path.read_bytes())
                checksum = row.pop("digest")
                if digest(encoded(row)) != checksum:
                    raise ValueError("Journal digest mismatch: " + path.name)
                self._index(row, path.name, checksum)
        self.journal_stamp = stamp

    def _index(self, row, name, checksum):
        self.db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
            (
                row["seq"],
                row["key"],
                row["kind"],
                row["subject"],
                row["at"],
                encoded(row["data"]).decode(),
                name,
                checksum,
            ),
        )

    def event(self, kind, subject, data, *, key):
        with self.locked():
            self._sync()
            old = self.by_key(key)
            if old:
                if (old["kind"], old["subject"], old["data"]) != (kind, subject, data):
                    raise ValueError("Idempotency key reused with different content")
                return old
            seq = self.db.execute("SELECT coalesce(max(seq),0)+1 FROM events").fetchone()[0]
            row = {
                "seq": seq,
                "key": key,
                "kind": kind,
                "subject": subject,
                "at": now(),
                "data": data,
            }
            row["digest"] = digest(encoded(row))
            name = f"{seq:012d}-{digest(key.encode())[:16]}.json"
            atomic(self.journal / name, encoded(row))
            # The journal is durable first. If indexing fails, leave the stamp
            # unchanged so the next sync recovers the acknowledged original.
            with self.db:
                self._index(row, name, row["digest"])
            self.journal_stamp = self.journal.stat().st_mtime_ns
            return row

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        result["data"] = json.loads(result["data"])
        return result

    def by_key(self, key):
        return self.decode(self.db.execute("SELECT * FROM events WHERE key=?", (key,)).fetchone())

    def events(self, kind=None, subject=None, *, after=0):
        terms, args = ["seq>?"], [after]
        for name, value in (("kind", kind), ("subject", subject)):
            if value is not None:
                terms.append(name + "=?")
                args.append(value)
        return [
            self.decode(r)
            for r in self.db.execute(
                "SELECT * FROM events WHERE " + " AND ".join(terms) + " ORDER BY seq", args
            )
        ]

    def latest(self, kind, subject):
        row = self.db.execute(
            "SELECT * FROM events WHERE kind=? AND subject=? ORDER BY seq DESC LIMIT 1",
            (kind, subject),
        ).fetchone()
        return self.decode(row)

    def put(self, value, media="application/json"):
        data = (
            value
            if isinstance(value, bytes)
            else encoded(value)
            if media == "application/json"
            else value.encode()
            if isinstance(value, str)
            else value
        )
        checksum = digest(data)
        path = self.objects / checksum
        if not path.exists():
            atomic(path, data)
        elif digest(path.read_bytes()) != checksum:
            raise ValueError("Immutable object was modified")
        return {"sha256": checksum, "media": media, "bytes": len(data)}

    def read(self, ref):
        checksum = ref["sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError("Invalid source digest")
        data = (self.objects / checksum).read_bytes()
        if digest(data) != checksum:
            raise ValueError("Immutable source digest mismatch")
        return data

    def json(self, ref):
        return json.loads(self.read(ref))

    def expose(self, ref, root, *, links=()):
        """Materialize an authorized, read-only source view; originals stay private."""
        directory = Path(root) / "sources"
        path = directory / ref["sha256"]
        raw = self.read(ref)
        if not path.exists() or digest(path.read_bytes()) != ref["sha256"]:
            atomic(path, raw)
        metadata = directory / (ref["sha256"] + ".json")
        previous = json.loads(metadata.read_bytes()) if metadata.exists() else {}
        value = {**ref, "links": sorted(set(previous.get("links", [])).union(links))}
        if previous != value:
            atomic(metadata, encoded(value))
        return path

    def close(self):
        self.db.close()
