from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def uid(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".pending")
    try:
        with temp.open("xb") as file:
            os.chmod(temp, 0o600)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


class Store:
    def __init__(self, config):
        self.config = config
        config.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._session_lock = (config.workspace / "session.lock").open("a")
        try:
            fcntl.flock(self._session_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if (config.workspace / "reset-pending.json").exists():
                raise RuntimeError("A session reset is incomplete; resume it before opening Bloom")
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _initialize(self):
        config = self.config
        config.initialize()
        self.path = config.workspace / "bloom.sqlite3"
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text())
        # Explicit, independently scoped recall assignments may share an owner
        # message. Preserve every legacy row while removing only that uniqueness.
        with self.transaction():
            unique_owner = any(
                index["unique"]
                and [r["name"] for r in self.rows("PRAGMA index_info(" + index["name"] + ")")]
                == ["owner_item"]
                for index in self.rows("PRAGMA index_list(memory_retrievals)")
            )
            if unique_owner:
                definition = next(
                    line
                    for line in Path(__file__).with_name("schema.sql").read_text().splitlines()
                    if line.startswith("CREATE TABLE IF NOT EXISTS memory_retrievals(")
                )
                self.db.execute(
                    definition.replace(
                        "IF NOT EXISTS memory_retrievals(", "memory_retrievals_upgrade(", 1
                    )
                )
                self.db.execute(
                    "INSERT INTO memory_retrievals_upgrade SELECT * FROM memory_retrievals"
                )
                self.db.execute("DROP TABLE memory_retrievals")
                self.db.execute("ALTER TABLE memory_retrievals_upgrade RENAME TO memory_retrievals")
        # Existing stores remain writable by the preceding release until an
        # explicit migration. Fresh stores start with the organized layout.
        self.db.execute(
            "INSERT OR IGNORE INTO artifact_layout VALUES(1,?)",
            (1 if self.one("SELECT 1 FROM artifacts LIMIT 1") else 2,),
        )
        self.db.execute("INSERT OR IGNORE INTO schema_versions VALUES(1,?)", (now(),))
        self.db.execute(
            "INSERT OR IGNORE INTO agents VALUES('astra','astra',NULL,'Astra','full',1,?,?)",
            (str(config.root), now()),
        )
        self.db.execute("INSERT OR IGNORE INTO sessions VALUES('astra','astra',?)", (now(),))

    def close(self):
        if getattr(self, "db", None) is not None:
            self.db.close()
        if getattr(self, "_session_lock", None) is not None:
            self._session_lock.close()

    @contextlib.contextmanager
    def transaction(self):
        nested = self.db.in_transaction
        savepoint = "s_" + uuid.uuid4().hex
        self.db.execute("SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("RELEASE " + savepoint if nested else "COMMIT")
        except BaseException:
            if nested:
                self.db.execute("ROLLBACK TO " + savepoint)
                self.db.execute("RELEASE " + savepoint)
            else:
                self.db.execute("ROLLBACK")
            raise

    @contextlib.contextmanager
    def runtime_lock(self):
        with (self.config.workspace / "runtime.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another Bloom runtime owns this workspace") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def artifact_paths(self, digest):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid artifact hash")
        return (
            self.config.artifacts / "runtime/objects" / digest,
            self.config.artifacts / digest[:2] / digest,
        )

    def artifact_path(self, digest):
        paths = self.artifact_paths(digest)
        return paths[0 if self.one("SELECT version FROM artifact_layout")["version"] == 2 else 1]

    def put(self, value, media_type="application/json") -> str:
        data = value if isinstance(value, bytes) else encoded(value)
        digest = hashlib.sha256(data).hexdigest()
        path = self.artifact_path(digest)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError("Artifact integrity failure")
        else:
            atomic(path, data)
        self.db.execute(
            "INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?)",
            (digest, len(data), media_type, now()),
        )
        self.touch(digest)
        return digest

    def touch(self, digest):
        stamp = time.time()
        # Coarsen repeated reads to avoid an fsync for each context item.
        self.db.execute(
            "INSERT INTO artifact_access VALUES(?,?) ON CONFLICT(hash) DO UPDATE SET accessed_at=excluded.accessed_at WHERE artifact_access.accessed_at<?",
            (digest, stamp, stamp - 3600),
        )

    def bytes(self, digest, *, touch=True, cache=True):
        paths = self.artifact_paths(digest)
        if touch and self.one("SELECT 1 FROM artifacts WHERE hash=?", (digest,)):
            self.touch(digest)
        for path in paths:
            try:
                data = path.read_bytes()
                break
            except FileNotFoundError:
                continue
        else:
            from .storage import Archive

            archive = Archive(self)
            # Preserve historical archive paths and cold receipts unchanged.
            relative = next(
                (
                    str(p.relative_to(self.config.root))
                    for p in paths
                    if archive.record(str(p.relative_to(self.config.root)))
                ),
                None,
            )
            if relative is None:
                raise FileNotFoundError("No local or catalogued artifact: " + digest)
            data = archive.read(relative, digest)
            if hashlib.sha256(data).hexdigest() != digest:
                raise RuntimeError("Archived artifact integrity failure")
            if cache:
                atomic(self.artifact_path(digest), data)
                self.db.execute(
                    "UPDATE archive_files SET removed_at=NULL WHERE path=? AND sha256=?",
                    (relative, digest),
                )
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError("Artifact integrity failure")
        return data

    def get(self, digest):
        return json.loads(self.bytes(digest))

    def event(self, kind, detail=None, task=None, agent=None):
        self.db.execute(
            "INSERT INTO events(task_id,agent_id,kind,detail_hash,created_at) VALUES(?,?,?,?,?)",
            (task, agent, kind, self.put(detail or {}), now()),
        )

    def item(
        self,
        session,
        envelope,
        task=None,
        call=None,
        key=None,
        *,
        origin=None,
        source_task=None,
        source_agent=None,
    ) -> int:
        old = self.one("SELECT id FROM items WHERE source_key=?", (key,)) if key else None
        if old:
            return old["id"]
        kind = envelope.get("type", "message")
        content = envelope.get("content", [])
        text = (
            content
            if isinstance(content, str)
            else "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        )
        if kind == "function_call_output":
            out = envelope.get("output", "")
            text = (
                out
                if isinstance(out, str)
                else "\n".join(
                    p.get("text", "[Native image/file content; see original envelope]") for p in out
                )
            )
        cur = self.db.execute(
            "INSERT INTO items(session_id,task_id,model_call_id,source_key,kind,role,phase,provider_id,call_id,tool_name,text,envelope_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session,
                task,
                call,
                key,
                kind,
                envelope.get("role"),
                envelope.get("phase"),
                envelope.get("id"),
                envelope.get("call_id"),
                envelope.get("name"),
                text,
                self.put(envelope),
                now(),
            ),
        )
        if origin:
            self.db.execute(
                "INSERT INTO item_origins VALUES(?,?,?,?)",
                (cur.lastrowid, origin, source_task, source_agent),
            )
        if origin in ("owner", "owner_steering") and task:
            from .turns import owner_input

            owner_input(self, task, cur.lastrowid)
        return cur.lastrowid

    def new_task(
        self, objective, agent="astra", parent=None, channel="local", address="", key=None
    ):
        task = key or uid("task")
        if self.one("SELECT id FROM tasks WHERE id=?", (task,)):
            return task
        with self.transaction():
            self.db.execute(
                "INSERT INTO tasks(id,agent_id,parent_id,objective,status,max_turns,channel,address,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    task,
                    agent,
                    parent,
                    objective,
                    "ready",
                    self.config.data["runtime"]["worker_turns"],
                    channel,
                    address,
                    now(),
                    now(),
                ),
            )
            self.item(
                agent,
                {"role": "user", "content": objective},
                task,
                key=task + ":objective",
                origin="assignment" if parent else "owner",
                source_task=parent,
            )
            if parent:
                from .turns import inherit

                inherit(self, task, parent)
            self.event("task.created", {"objective": objective}, task, agent)
            if agent == "astra" and parent is None:
                self.event("owner.message", {"source": channel}, task, agent)
        return task

    def status(self, task, status, error=None):
        self.db.execute(
            "UPDATE tasks SET status=?,error=?,updated_at=? WHERE id=?",
            (status, error, now(), task),
        )

    def readonly(self, sql, args=()):
        uri = self.path.as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            count = 0

            def progress():
                nonlocal count
                count += 1
                return count > 10000

            db.set_progress_handler(progress, 1000)
            return [dict(row) for row in db.execute(sql, args).fetchmany(1000)]
