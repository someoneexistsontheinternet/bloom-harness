"""Verified cold copies, an SSD search catalog, and independent scheduled upkeep."""

from __future__ import annotations

import argparse
import codecs
import contextlib
import fcntl
import hashlib
import gzip
import json
import os
import plistlib
import re
import stat
import subprocess
import time
import uuid
import zlib
from pathlib import Path

from .privacy import clean, StreamRedactor
from .store import atomic, encoded, now

LABEL = "dev.bloom.agent.archive"
BLOCK = 256 * 1024


class ColdStorageUnavailable(OSError):
    pass


def fingerprint(path):
    row = path.lstat()
    return row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Archive:
    def __init__(self, store):
        self.store = store
        self.config = store.config
        self.settings = self.config.data.get("storage", {})

    def cold_root(self):
        # Merely loading configuration must never stat a disconnected volume.
        value = self.settings.get("cold_root")
        if not value or not Path(value).is_absolute():
            raise ColdStorageUnavailable("Cold storage is not configured")
        return Path(value)

    def volume_ready(self):
        mount = Path(self.settings.get("volume_mount", "/nonexistent-bloom-volume"))
        cold = self.cold_root()
        if not mount.is_absolute() or not cold.is_relative_to(mount) or cold == mount:
            return False
        # Do not mkdir /Volumes/name when the device is absent: that writes to SSD.
        if not os.path.ismount(mount):
            return False
        try:
            return (cold / ".volume-id").read_text().strip() == self.settings["volume_id"]
        except (OSError, KeyError):
            return False

    def require_volume(self):
        if not self.volume_ready():
            raise ColdStorageUnavailable(
                "The configured cold volume is disconnected or has the wrong identity. Search text remains on the SSD; reconnect the RAID to read uncached originals."
            )

    @contextlib.contextmanager
    def mounted(self):
        self.require_volume()
        descriptor = os.open(self.cold_root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.require_volume()
            if os.fstat(descriptor).st_dev != os.stat(self.settings["volume_mount"]).st_dev:
                raise ColdStorageUnavailable("Cold mount changed while opening it")
            # Every write below uses this directory FD. A cable disconnection
            # cannot redirect mkdir/rename into an SSD shadow at /Volumes/....
            yield descriptor
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def folder(self, parent, name, create=False):
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            if os.fstat(descriptor).st_dev != os.fstat(parent).st_dev:
                raise ColdStorageUnavailable("Archive subdirectory changed devices")
            yield descriptor
        finally:
            os.close(descriptor)

    @contextlib.contextmanager
    def open_object(self, digest):
        self.object_path(digest)  # Validate the content-addressed name.
        with (
            self.mounted() as root,
            self.folder(root, "objects") as objects,
            self.folder(objects, digest[:2]) as parent,
        ):
            compressed = False
            try:
                descriptor = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                descriptor = os.open(digest + ".gz", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                compressed = True
            with os.fdopen(descriptor, "rb") as stream:
                if compressed:
                    with gzip.GzipFile(fileobj=stream, mode="rb") as decoded:
                        yield decoded
                else:
                    yield stream

    def object_path(self, digest):
        if not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("Invalid archive hash")
        return self.cold_root() / "objects" / digest[:2] / digest

    def record(self, relative):
        return self.store.one(
            "SELECT * FROM archive_files WHERE path=? AND indexed=1 ORDER BY id DESC LIMIT 1",
            (relative,),
        )

    def read(self, relative, digest=None):
        row = (
            self.store.one(
                "SELECT * FROM archive_files WHERE path=? AND sha256=? AND indexed=1",
                (relative, digest),
            )
            if digest
            else self.record(relative)
        )
        if not row:
            raise FileNotFoundError("No local file or verified archived original: " + relative)
        self.require_volume()
        try:
            with self.open_object(row["sha256"]) as stream:
                data = stream.read()
        except OSError as exc:
            raise ColdStorageUnavailable(
                "Archived original could not be read; its local search index is retained"
            ) from exc
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise RuntimeError("Archived original failed its SHA-256 check")
        return data

    def range(self, relative, offset, limit):
        row = self.record(relative)
        if not row:
            return None
        self.require_volume()
        try:
            with self.open_object(row["sha256"]) as stream:
                # Gzip offsets and sizes refer to original bytes. Verify before
                # returning a range; no whole-file allocation is needed.
                digest = hashlib.sha256()
                size = 0
                for block in iter(lambda: stream.read(BLOCK), b""):
                    digest.update(block)
                    size += len(block)
                if size != row["bytes"] or digest.hexdigest() != row["sha256"]:
                    raise RuntimeError("Archived output size differs from its receipt")
                stream.seek(offset)
                data = stream.read(limit)
                return {
                    "output": data.decode(errors="replace"),
                    "next_offset": stream.tell(),
                    "total_bytes": row["bytes"],
                    "archive_id": row["id"],
                    "archive_sha256": row["sha256"],
                }
        except OSError as exc:
            raise ColdStorageUnavailable(
                "Archived process output requires the reconnected RAID"
            ) from exc

    def search(self, query, limit=20):
        # Keyword intersection is by whole file, even when terms occur in distant
        # chunks. This query never stats/reads the external volume.
        terms = list(dict.fromkeys(re.findall(r"\w+", query, re.UNICODE)))
        if not terms or len(terms) > 16 or not 1 <= limit <= 100:
            raise ValueError("Use 1–16 search keywords and a limit from 1 to 100")
        queries = ['"' + term.replace('"', '""') + '"' for term in terms]
        part = "SELECT c.file_id FROM archive_search f JOIN archive_chunks c ON c.id=f.rowid WHERE archive_search MATCH ?"
        rows = self.store.rows(
            "SELECT a.* FROM archive_files a WHERE a.indexed=1 AND a.id IN ("
            + " INTERSECT ".join(part for _ in terms)
            + ") ORDER BY a.id DESC LIMIT ?",
            (*queries, limit),
        )
        results = []
        for row in rows:
            chunk = self.store.one(
                "SELECT c.text_zlib FROM archive_search f JOIN archive_chunks c ON c.id=f.rowid WHERE archive_search MATCH ? AND c.file_id=? ORDER BY c.position LIMIT 1",
                (queries[0], row["id"]),
            )
            text = zlib.decompress(chunk["text_zlib"]).decode()
            found = re.search(re.escape(terms[0]), text, re.IGNORECASE)
            start = max(0, found.start() - 120) if found else 0
            results.append(
                {
                    "id": row["id"],
                    "path": row["path"],
                    "sha256": row["sha256"],
                    "bytes": row["bytes"],
                    "kind": row["kind"],
                    "snippet": text[start : start + 480],
                    "copied_at": row["copied_at"],
                }
            )
        return results

    def pins(self):
        """Retain current prompts and unfinished task trees, not every raw call."""
        store = self.store
        tasks = store.rows(
            "WITH RECURSIVE live(id) AS (SELECT id FROM tasks WHERE status NOT IN ('completed','cancelled') UNION SELECT t.id FROM tasks t JOIN live p ON t.parent_id=p.id) SELECT DISTINCT id FROM live"
        )
        task_ids = [r["id"] for r in tasks]
        agents = {"astra"} | {
            r["agent_id"]
            for r in store.rows(
                "SELECT DISTINCT agent_id FROM tasks WHERE status NOT IN ('completed','cancelled')"
            )
        }
        hashes = {r["detail_hash"] for r in store.rows("SELECT detail_hash FROM memories")}
        hashes.update(
            r["source_hash"]
            for r in store.rows(
                "SELECT source_hash FROM working_compactions WHERE status IN ('prepared','running','interrupted') OR context_id=(SELECT id FROM contexts WHERE session_id='astra' ORDER BY rowid DESC LIMIT 1)"
            )
        )
        hashes.update(
            r["source_hash"]
            for r in store.rows(
                "SELECT r.source_hash FROM memories m JOIN memory_revisions r ON r.topic=m.topic AND r.revision=m.revision"
            )
            if r["source_hash"]
        )
        # Active consolidation inputs/results remain local for exact restart reuse.
        hashes.update(
            value
            for row in store.rows(
                "SELECT request_hash,result_hash FROM memory_batches WHERE status!='completed'"
            )
            for value in row.values()
            if value
        )
        hashes.update(
            r["report_hash"]
            for r in store.rows(
                "SELECT report_hash FROM memory_retrievals WHERE report_hash IS NOT NULL AND report_item IS NULL"
            )
        )
        hashes.update(
            r["report_hash"]
            for r in store.rows(
                "SELECT report_hash FROM worker_launches WHERE report_hash IS NOT NULL AND report_item IS NULL AND obsolete=0"
            )
        )
        hashes.update(
            r["detail_hash"]
            for r in store.rows(
                "SELECT d.detail_hash FROM model_call_diagnostics d JOIN model_calls c ON c.id=d.call_id WHERE c.status IN ('prepared','running','uncertain')"
            )
        )
        for agent in agents:
            context = store.one(
                "SELECT * FROM contexts WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (agent,)
            )
            if context:
                rows = store.rows(
                    "SELECT i.envelope_hash FROM items i WHERE i.session_id=? AND (i.id>? OR i.id IN (SELECT item_id FROM context_members WHERE context_id=?))",
                    (agent, context["through_item"], context["id"]),
                )
            else:
                rows = store.rows("SELECT envelope_hash FROM items WHERE session_id=?", (agent,))
            hashes.update(r["envelope_hash"] for r in rows)
            hashes.update(
                r["detail_hash"]
                for r in store.rows(
                    "SELECT detail_hash FROM events WHERE agent_id=? ORDER BY id DESC LIMIT 5",
                    (agent,),
                )
            )
        # Batch IN parameters; avoid SQLite variable limits for large histories.
        for offset in range(0, len(task_ids), 200):
            selected = task_ids[offset : offset + 200]
            slots = ",".join("?" for _ in selected)
            for table, columns in [
                ("items", ["envelope_hash"]),
                ("model_calls", ["request_hash", "response_hash"]),
                ("operations", ["intent_hash", "result_hash"]),
                ("tool_calls", ["arguments_hash"]),
                ("task_outcomes", ["evidence_hash"]),
                ("events", ["detail_hash"]),
            ]:
                hashes.update(
                    value
                    for row in store.rows(
                        f"SELECT {','.join(columns)} FROM {table} WHERE task_id IN ({slots})",
                        selected,
                    )
                    for value in row.values()
                    if value
                )
            hashes.update(
                value
                for row in store.rows(
                    f"SELECT c.request_hash,c.result_hash FROM controls c JOIN operation_controls l ON l.control_id=c.id JOIN operations o ON o.id=l.operation_id WHERE o.task_id IN ({slots})",
                    selected,
                )
                for value in row.values()
                if value
            )
            hashes.update(
                value
                for row in store.rows(
                    f"SELECT template_hash,bindings_hash,request_hash FROM preset_invocations WHERE task_id IN ({slots})",
                    selected,
                )
                for value in row.values()
                if value
            )
            hashes.update(
                r["request_hash"]
                for r in store.rows(
                    f"SELECT request_hash FROM worker_batches WHERE parent_task IN ({slots})",
                    selected,
                )
            )
            hashes.update(
                value
                for row in store.rows(
                    f"SELECT source_hash,view_hash,image_policy_hash FROM research_views WHERE task_id IN ({slots})",
                    selected,
                )
                for value in row.values()
            )
            hashes.update(
                r["artifact_hash"]
                for r in store.rows(
                    f"SELECT g.artifact_hash FROM evidence_grants g JOIN tasks t ON t.agent_id=g.agent_id WHERE t.id IN ({slots})",
                    selected,
                )
            )
        for row in store.rows(
            "SELECT p.payload_hash FROM delivery_payloads p JOIN deliveries d ON d.id=p.delivery_id WHERE d.status!='sent'"
        ):
            hashes.add(row["payload_hash"])
            payload = store.get(row["payload_hash"])
            hashes.update(f["hash"] for f in payload.get("files", []))
        hashes.update(
            r["hash"]
            for r in store.rows(
                "SELECT f.hash FROM web_files f JOIN web_sources s ON s.id=f.source_id JOIN web_runs w ON w.operation_id=s.operation_id JOIN tasks t ON t.id=w.task_id WHERE t.status NOT IN ('completed','cancelled')"
            )
        )
        return hashes

    def open_file(self, path):
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-F", "p", "--", str(path)], capture_output=True, timeout=5
        )
        if result.returncode not in (0, 1) or result.stderr:
            raise RuntimeError("Cannot establish whether this file is open; retained locally")
        return any(line.startswith(b"p") for line in result.stdout.splitlines())

    def candidates(self, cutoff):
        for base in (self.config.root / "logs", self.config.artifacts):
            for directory, folders, files in os.walk(base, followlinks=False):
                folders[:] = sorted(
                    n
                    for n in folders
                    if not (Path(directory) / n).is_symlink()
                    and not (Path(directory) == self.config.artifacts and n == "releases")
                )
                if Path(directory) == self.config.artifacts / "runtime":
                    folders[:] = [n for n in folders if n != "calls"]
                for name in sorted(files):
                    path = Path(directory) / name
                    if path == self.config.artifacts / "README.md":
                        continue
                    try:
                        info = path.lstat()
                    except FileNotFoundError:
                        continue  # Concurrent log rotation/pruning is normal.
                    if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                        yield path
        # Process identity/control state stays local; only settled output is cold.
        for row in self.store.rows(
            "SELECT directory FROM processes WHERE target='local' AND status IN ('completed','failed')"
        ):
            path = Path(row["directory"]) / "output.log"
            if (
                path.is_relative_to(self.config.workspace / "processes")
                and path.is_file()
                and not path.is_symlink()
                and path.stat().st_mtime < cutoff
            ):
                yield path

    def eligible(self, path, cutoff, pins):
        if path.is_relative_to(self.config.artifacts / "runtime/calls"):
            return False  # Bounded projections are rebuilt/pruned, never archived.
        if path.name in pins and path in self.store.artifact_paths(path.name):
            return False
        accessed = self.store.one(
            "SELECT accessed_at FROM artifact_access WHERE hash=?", (path.name,)
        )
        if accessed and accessed["accessed_at"] >= cutoff:
            return False
        saved = self.store.one(
            "SELECT f.accessed_at,t.status FROM tool_result_files f JOIN tasks t ON t.id=f.task_id WHERE f.path=? OR f.path || '.json'=?",
            (str(path), str(path)),
        )
        if saved and (
            saved["accessed_at"] >= cutoff or saved["status"] not in ("completed", "cancelled")
        ):
            return False
        web = self.store.one(
            "SELECT f.accessed_at,t.status FROM web_files f JOIN web_sources s ON s.id=f.source_id JOIN web_runs w ON w.operation_id=s.operation_id JOIN tasks t ON t.id=w.task_id WHERE f.path=?",
            (str(path),),
        )
        if web and (
            web["accessed_at"] >= cutoff or web["status"] not in ("completed", "cancelled")
        ):
            return False
        # An idle service can retain an open run for weeks. Keep its entire run.
        for parent in path.parents:
            if parent == self.config.root:
                break
            launch = parent / "launch.json"
            if launch.is_file():
                try:
                    identity = json.loads(launch.read_text())
                    from .service import process_identity

                    if not isinstance(identity, dict):
                        return False
                    if identity.get("pid") and process_identity(identity["pid"]) == identity.get(
                        "start_identity"
                    ):
                        return False
                except (OSError, ValueError):
                    return False
        return path.lstat().st_mtime < cutoff

    def copy(self, path):
        before = fingerprint(path)
        temporary = uuid.uuid4().hex + ".partial"
        digest = hashlib.sha256()
        # Named result text, metadata and their large original envelopes use the
        # same seven-day sweep, with compressed bytes only on the cold volume.
        compressed = bool(
            self.store.one(
                "SELECT 1 FROM tool_result_files WHERE path=? OR path || '.json'=? OR source_hash=? OR metadata_hash=? LIMIT 1",
                (str(path), str(path), path.name, path.name),
            )
        )
        compressed = compressed or bool(
            self.store.one(
                "SELECT 1 FROM web_files WHERE kind IN ('text','metadata') AND (path=? OR hash=?) LIMIT 1",
                (str(path), path.name),
            )
        )
        with self.mounted() as root, self.folder(root, "incoming", True) as incoming:
            try:
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=incoming
                )
                with path.open("rb") as source, os.fdopen(descriptor, "wb") as output:
                    with (
                        gzip.GzipFile(
                            filename="", fileobj=output, mode="wb", compresslevel=6, mtime=0
                        )
                        if compressed
                        else contextlib.nullcontext(output) as target
                    ):
                        for block in iter(lambda: source.read(BLOCK), b""):
                            target.write(block)
                            digest.update(block)
                    output.flush()
                    os.fsync(output.fileno())
                if fingerprint(path) != before:
                    raise RuntimeError("Source changed during archive copy; retained locally")
                value = digest.hexdigest()
                descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=incoming)
                with os.fdopen(descriptor, "rb") as stream:
                    with (
                        gzip.GzipFile(fileobj=stream, mode="rb")
                        if compressed
                        else contextlib.nullcontext(stream) as decoded
                    ):
                        if hashlib.file_digest(decoded, "sha256").hexdigest() != value:
                            raise RuntimeError("Cold copy verification failed; source retained")
                with (
                    self.folder(root, "objects", True) as objects,
                    self.folder(objects, value[:2], True) as parent,
                ):
                    for name in (value, value + ".gz"):
                        try:
                            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                        except FileNotFoundError:
                            continue
                        with os.fdopen(descriptor, "rb") as stream:
                            with (
                                gzip.GzipFile(fileobj=stream, mode="rb")
                                if name.endswith(".gz")
                                else contextlib.nullcontext(stream) as decoded
                            ):
                                if hashlib.file_digest(decoded, "sha256").hexdigest() != value:
                                    raise RuntimeError("Existing cold object failed verification")
                        break
                    else:
                        os.rename(
                            temporary,
                            value + (".gz" if compressed else ""),
                            src_dir_fd=incoming,
                            dst_dir_fd=parent,
                        )
                        os.fsync(parent)
                        os.fsync(incoming)
                return value, before
            finally:
                try:
                    os.unlink(temporary, dir_fd=incoming)
                except OSError:
                    pass  # Disconnected partials remain on the original device.

    def receipt(self, key, value):
        with self.mounted() as root, self.folder(root, "receipts", True) as parent:
            temporary = uuid.uuid4().hex + ".pending"
            try:
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded(value))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.rename(temporary, key + ".json", src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except OSError:
                    pass

    def chunks(self, stream):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        redactor = StreamRedactor()
        overlap = ""
        while block := stream.read(BLOCK):
            text = decoder.decode(redactor.feed(block))
            # Binary/base64 payloads aren't meaningful text-search terms.
            text = clean(re.sub(r"[A-Za-z0-9+/=_-]{256,}", "[opaque payload]", overlap + text))
            yield text
            overlap = text[-512:]
        tail = decoder.decode(redactor.feed(b"", final=True), final=True)
        if tail:
            yield clean(overlap + tail)

    def index(self, path, digest, before):
        relative = str(path.relative_to(self.config.root))
        previous = self.store.one(
            "SELECT * FROM archive_files WHERE path=? AND sha256=?", (relative, digest)
        )
        if previous and previous["indexed"]:
            return previous
        with self.open_object(digest) as stream:
            sample = stream.read(8192)
        try:
            codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
            kind = "binary" if b"\x00" in sample else "text"
        except UnicodeError:
            kind = "binary"
        if not previous:
            self.store.db.execute(
                "INSERT INTO archive_files(path,sha256,bytes,mtime_ns,mode,kind,copied_at) VALUES(?,?,?,?,?,?,?)",
                (
                    relative,
                    digest,
                    before[2],
                    before[3],
                    stat.S_IMODE(path.stat().st_mode),
                    kind,
                    now(),
                ),
            )
            previous = self.store.one(
                "SELECT * FROM archive_files WHERE path=? AND sha256=?", (relative, digest)
            )
        file_id = previous["id"]
        # Recover an interrupted index without leaving duplicate searchable chunks.
        for chunk in self.store.db.execute(
            "SELECT * FROM archive_chunks WHERE file_id=?", (file_id,)
        ):
            with self.store.transaction():
                text = zlib.decompress(chunk["text_zlib"]).decode()
                self.store.db.execute(
                    "INSERT INTO archive_search(archive_search,rowid,path,text) VALUES('delete',?,?,?)",
                    (chunk["id"], relative, text),
                )
                self.store.db.execute("DELETE FROM archive_chunks WHERE id=?", (chunk["id"],))
        with self.open_object(digest) as stream:
            texts = self.chunks(stream) if kind == "text" and before[2] else iter([""])
            for position, text in enumerate(texts):
                # Short transactions keep the live runtime's SQLite writer available.
                with self.store.transaction():
                    row = self.store.db.execute(
                        "INSERT INTO archive_chunks(file_id,position,text_zlib) VALUES(?,?,?)",
                        (file_id, position, zlib.compress(text.encode(), 6)),
                    )
                    self.store.db.execute(
                        "INSERT INTO archive_search(rowid,path,text) VALUES(?,?,?)",
                        (row.lastrowid, relative, text),
                    )
        self.store.db.execute("UPDATE archive_files SET indexed=1 WHERE id=?", (file_id,))
        return self.store.one("SELECT * FROM archive_files WHERE id=?", (file_id,))

    def offload(self, path, cutoff):
        if not self.eligible(path, cutoff, self.pins()) or self.open_file(path):
            return None
        digest, before = self.copy(path)
        row = self.index(path, digest, before)
        receipt = {
            k: row[k] for k in ("path", "sha256", "bytes", "mtime_ns", "mode", "kind", "copied_at")
        }
        receipt["source_root"] = str(self.config.root)
        with self.open_object(digest) as stream:
            receipt["encoding"] = "gzip" if isinstance(stream, gzip.GzipFile) else "identity"
        self.require_volume()
        key = hashlib.sha256((row["path"] + "\0" + digest).encode()).hexdigest()
        self.receipt(key, receipt)
        if self.open_file(path):
            return None
        # Never wait on removable-device I/O while holding the live SQLite writer.
        # The copy, hash check, search index and cold receipt are already durable.
        self.require_volume()
        with self.store.transaction():
            if not self.eligible(path, cutoff, self.pins()) or fingerprint(path) != before:
                return None
            path.unlink()
            sync_directory(path.parent)
            self.store.db.execute(
                "UPDATE archive_files SET removed_at=? WHERE id=?", (now(), row["id"])
            )
        return row["bytes"]

    def sweep(self):
        result = {"status": "disabled", "started_at": now(), "files": 0, "bytes": 0, "errors": []}
        if not self.settings.get("enabled"):
            return result
        with (self.config.workspace / "archive.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return dict(result, status="already_running")
            if not self.volume_ready():
                return dict(result, status="waiting_for_volume")
            cutoff = time.time() - self.settings.get("retention_days", 7) * 86400
            started = time.monotonic()
            result["status"] = "completed"
            for path in self.candidates(cutoff):
                if (
                    result["files"] >= self.settings.get("max_files_per_run", 256)
                    or result["bytes"] >= self.settings.get("max_bytes_per_run", 512 * 1024**2)
                    or time.monotonic() - started >= self.settings.get("max_seconds_per_run", 50)
                ):
                    result["status"] = "more_pending"
                    break
                try:
                    removed = self.offload(path, cutoff)
                    if removed is not None:
                        result["files"] += 1
                        result["bytes"] += removed
                except (OSError, RuntimeError) as exc:
                    if len(result["errors"]) < 20:
                        result["errors"].append(
                            {
                                "path": str(path.relative_to(self.config.root)),
                                "error": clean(str(exc)),
                            }
                        )
                    if not self.volume_ready():
                        result["status"] = "waiting_for_volume"
                        break
                    result["status"] = "completed_with_errors"
            return dict(result, completed_at=now())

    def status(self):
        latest = self.config.workspace / "archive-last-run.json"
        return {
            "enabled": self.settings.get("enabled", False),
            "retention_days": self.settings.get("retention_days", 7),
            "cold_root": self.settings.get("cold_root"),
            "local_search_database": str(self.store.path),
            "catalog": self.store.one(
                "SELECT count(*) AS originals,sum(bytes) AS bytes,sum(indexed) AS indexed FROM archive_files"
            ),
            "last_run": json.loads(latest.read_text()) if latest.exists() else None,
        }

    def restore(self, ident):
        row = self.store.one("SELECT * FROM archive_files WHERE id=? AND indexed=1", (ident,))
        if not row:
            raise ValueError("Unknown archive ID")
        path = self.config.root / row["path"]
        if self.store.one(
            "SELECT 1 FROM artifacts WHERE hash=?", (row["sha256"],)
        ) and path in self.store.artifact_paths(row["sha256"]):
            path = self.store.artifact_path(row["sha256"])
        if not path.resolve().is_relative_to(self.config.root):
            raise ValueError("Restore destination redirects outside this project")
        if path.exists() or path.is_symlink():
            raise FileExistsError("Restore will not overwrite a current file")
        self.require_volume()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".pending")
        digest = hashlib.sha256()
        try:
            with self.open_object(row["sha256"]) as source, temporary.open("xb") as output:
                os.chmod(temporary, row["mode"])
                for block in iter(lambda: source.read(BLOCK), b""):
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != row["sha256"]:
                raise RuntimeError("Restore failed its SHA-256 check")
            # Atomic no-replace publication protects a concurrent newer file.
            os.link(temporary, path)
            sync_directory(path.parent)
            self.store.db.execute(
                "UPDATE archive_files SET removed_at=NULL WHERE id=?", (row["id"],)
            )
            return {"restored": str(path), "sha256": row["sha256"]}
        finally:
            temporary.unlink(missing_ok=True)


def install(store, plan=None):
    from .service import installed_path, source_manifest, unload

    root = store.config.root
    if not store.config.data.get("storage", {}).get("enabled"):
        raise ValueError("Enable and configure storage before installing its maintenance job")
    plan = plan or json.loads((root / "workspace/service/candidate.json").read_text())
    if plan["root"] != str(root) or source_manifest(Path(plan["source"])) != plan["manifest"]:
        raise ValueError("Prepare a verified local source candidate before installing archival")
    interval = store.config.data["storage"].get("interval_seconds", 3600)
    if type(interval) is not int or interval < 300:
        raise ValueError("Archive interval must be at least 300 seconds")
    directory = root / "logs/archive-bootstrap"
    directory.mkdir(parents=True, exist_ok=True)
    path = installed_path(root, label=LABEL)
    unload(root, label=LABEL)
    spec = {
        "Label": LABEL,
        "ProgramArguments": [
            str(root / ".venv/bin/python"),
            "-m",
            "bloom.storage",
            "--root",
            str(root),
            "sweep",
        ],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "StartInterval": interval,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "EnvironmentVariables": {
            "BLOOM_ROOT": str(root),
            "BLOOM_SOURCE_ROOT": plan["source"],
            "PYTHONPATH": plan["source"] + "/src",
            "PYTHONDONTWRITEBYTECODE": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        },
        "StandardOutPath": str(directory / "stdout.log"),
        "StandardErrorPath": str(directory / "stderr.log"),
    }
    atomic(path, plistlib.dumps(spec))
    subprocess.run(["launchctl", "enable", f"gui/{os.getuid()}/" + LABEL], check=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
    return {
        "label": LABEL,
        "plist": str(path),
        "interval_seconds": interval,
        "source_commit": plan["commit"],
    }


def main():
    from .config import Config
    from .store import Store

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BLOOM_ROOT", str(Path(__file__).resolve().parents[2]))),
    )
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("sweep", "status", "install"):
        sub.add_parser(action)
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    read = sub.add_parser("restore")
    read.add_argument("id", type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    with contextlib.ExitStack() as stack:
        directory = None
        if args.action == "sweep":
            directory = (
                root
                / "logs/archive"
                / (time.strftime("local-%Y%m%d_%H%M%S") + "-" + str(os.getpid()))
            )
            directory.mkdir(parents=True, exist_ok=False)
            stream = stack.enter_context((directory / "worker.log").open("w"))
            stack.enter_context(contextlib.redirect_stdout(stream))
            stack.enter_context(contextlib.redirect_stderr(stream))
            from .service import process_identity

            atomic(
                directory / "launch.json",
                encoded(
                    {
                        "pid": os.getpid(),
                        "start_identity": process_identity(os.getpid()),
                        "root": str(root),
                        "source": os.environ.get("BLOOM_SOURCE_ROOT"),
                        "started_at": now(),
                    }
                ),
            )
        store = Store(Config(root))
        stack.callback(store.close)
        archive = Archive(store)
        if args.action == "search":
            result = archive.search(args.query, args.limit)
        elif args.action == "restore":
            result = archive.restore(args.id)
        elif args.action == "install":
            result = install(store)
        elif args.action == "status":
            result = archive.status()
        else:
            try:
                result = archive.sweep()
                if store.config.data.get("storage", {}).get("enabled"):
                    from .backups import Backups

                    result["weekly_backup"] = Backups(archive).run_if_due()
            except Exception as exc:
                result = {"status": "failed", "error": clean(str(exc)), "completed_at": now()}
            atomic(directory / "result.json", encoded(result))
            atomic(
                store.config.workspace / "archive-last-run.json",
                encoded({**result, "log_directory": str(directory)}),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get("status") == "failed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
