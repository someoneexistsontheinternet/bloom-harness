"""Immutable filesystem events and payloads, with a rebuildable search index."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid


def now():
    return dt.datetime.now(dt.UTC).isoformat()


def uid(prefix="id"):
    return prefix + "_" + uuid.uuid4().hex


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def atomic(path: Path, data: bytes):
    """Publish complete bytes, syncing the file and containing directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as f:
            os.chmod(temporary, 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
        raise ValueError("Use an identifier of 1–100 letters, digits, underscores or hyphens")
    return value


class Store:
    """Own immutable records; every SQLite row can be reconstructed from events."""

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd = None
        for name in ("events", "objects", "inbox", "received", "seen", "quarantine", "work"):
            (self.root / name).mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "index.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events(
              seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, at TEXT NOT NULL,
              kind TEXT NOT NULL, actor TEXT NOT NULL, target TEXT NOT NULL,
              task TEXT NOT NULL, parent TEXT, dedup TEXT UNIQUE, digest TEXT NOT NULL,
              path TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_target ON events(target,seq);
            CREATE INDEX IF NOT EXISTS events_task ON events(task,seq);
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(id UNINDEXED,text);
        """)
        self.sync()

    @contextlib.contextmanager
    def locked(self):
        """Serialize file commits across CLI processes; allow nested local operations."""
        with self._thread_lock:
            if not self._depth:
                self._fd = (self.root / ".writer.lock").open("a+")
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if not self._depth:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                    self._fd.close()

    def _index(self, event, path):
        existing = self.db.execute("SELECT digest FROM events WHERE id=?", (event["id"],)).fetchone()
        if existing:
            if existing[0] != event["digest"]:
                raise ValueError("An immutable event changed: " + event["id"])
            return
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            event["seq"], event["id"], event["at"], event["kind"], event["actor"],
            event["target"], event["task"], event.get("parent"), event.get("dedup"),
            event["digest"], str(path.relative_to(self.root)), encoded(event["data"]).decode()))
        text = encoded(event).decode()
        if event["data"].get("search_source"):
            text += "\n" + self.read(event["data"]["search_source"]).decode(errors="replace")
        self.db.execute("INSERT INTO search VALUES(?,?)", (event["id"], text))

    def sync(self, rebuild=False):
        """Recover a committed file missing from the index; never infer a missing event."""
        with self.locked():
            if rebuild:
                self.db.execute("DELETE FROM events")
                self.db.execute("DELETE FROM search")
            known = {r["path"] for r in self.db.execute("SELECT path FROM events")}
            paths = sorted((self.root / "events").glob("*/*.json"))
            actual = {str(p.relative_to(self.root)) for p in paths}
            if known - actual:
                raise ValueError("Indexed original event is missing; restore it before continuing")
            for path in paths:
                if str(path.relative_to(self.root)) in known and not rebuild:
                    continue
                event = json.loads(path.read_bytes())
                digest = event.pop("digest")
                if hashlib.sha256(encoded(event)).hexdigest() != digest:
                    raise ValueError("Event checksum mismatch: " + str(path))
                event["digest"] = digest
                self._index(event, path)
            self.db.commit()

    def append(self, kind, data, *, actor="system", target="", task="", parent=None, dedup=None):
        """Commit one event atomically; identical idempotent submissions reuse it."""
        with self.locked():
            self.sync()
            if dedup:
                old = self.db.execute("SELECT * FROM events WHERE dedup=?", (dedup,)).fetchone()
                if old:
                    if (old["kind"], old["actor"], old["target"], old["task"], old["parent"],
                        old["data"]) != (kind, actor, target, task, parent, encoded(data).decode()):
                        raise ValueError("Idempotency key reused with different content: " + dedup)
                    return self.get(old["id"])
            seq = self.db.execute("SELECT coalesce(max(seq),0)+1 FROM events").fetchone()[0]
            event = dict(id=uid("ev"), seq=seq, at=now(), kind=kind, actor=actor,
                         target=target, task=task, parent=parent, dedup=dedup, data=data)
            event["digest"] = hashlib.sha256(encoded(event)).hexdigest()
            path = self.root / "events" / event["at"][:10] / f"{seq:012d}-{event['id']}.json"
            atomic(path, encoded(event))
            self._index(event, path)
            self.db.commit()
            return event

    def get(self, event_id):
        row = self.db.execute("SELECT path,digest FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise KeyError(event_id)
        event = json.loads((self.root / row["path"]).read_bytes())
        digest = event.pop("digest")
        if digest != row["digest"] or hashlib.sha256(encoded(event)).hexdigest() != digest:
            raise ValueError("Original event checksum mismatch")
        return {**event, "digest": digest}

    def events(self, kind=None, *, target=None, task=None, after=0):
        terms, args = ["seq> ?"], [after]
        for key, val in (("kind", kind), ("target", target), ("task", task)):
            if val is not None:
                terms.append(key + "=?")
                args.append(val)
        return [self.get(r[0]) for r in self.db.execute(
            "SELECT id FROM events WHERE " + " AND ".join(terms) + " ORDER BY seq", args)]

    def put(self, data, media="text/plain"):
        """Save exact bytes once; JSON metadata points to a portable relative path."""
        if isinstance(data, str):
            data = data.encode()
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / "objects" / digest
        if not path.exists():
            atomic(path, data)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Original object checksum mismatch")
        return dict(sha256=digest, path=str(path.relative_to(self.root)), bytes=len(data), media=media)

    def read(self, ref):
        digest = ref["sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid content digest")
        data = (self.root / "objects" / digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Original object checksum mismatch")
        return data

    def search(self, query, limit=30):
        words = re.findall(r"\w+", query)
        if not words:
            return []
        match = " OR ".join('"' + word + '"' for word in words)
        return [self.get(row[0]) for row in self.db.execute(
            "SELECT id FROM search WHERE search MATCH ? ORDER BY rank LIMIT ?", (match, limit))]

    def close(self):
        self.db.close()
