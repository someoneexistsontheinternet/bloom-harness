"""Durable asynchronous tool processes and a job-scoped capability broker."""

import asyncio
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import signal
import sys
import time
from datetime import datetime

import jsonschema

from .agents import Actor, Agents
from .store import atomic, encoded, identifier, now, uid
from .tool import definition


TERMINAL = {"completed", "failed", "cancelled", "uncertain"}


def sandbox_profile(read_paths, write_paths):
    quoted = lambda p: json.dumps(str(Path(p).resolve()))
    reads = " ".join("(subpath " + quoted(p) + ")" for p in read_paths)
    writes = " ".join("(subpath " + quoted(p) + ")" for p in write_paths)
    return f"""(version 1)
(deny default)
(import "system.sb")
(allow process-exec process-fork sysctl-read mach-lookup)
(allow signal (target same-sandbox))
(allow file-read-metadata)
(allow file-read* file-map-executable {reads})
(allow file-write* {writes} (literal "/dev/null") (literal "/dev/tty"))
(allow network-outbound)
"""


class Jobs:
    def __init__(self, store, handlers=None, *, trusted_tools=False, limit=4, timeout=900):
        self.store = store
        self.agents = Agents(store)
        self.handlers = handlers or {}
        self.trusted_tools = trusted_tools
        self.limit = limit
        self.timeout = timeout
        self.address = None
        self.server = None
        self.running = {}
        self.secret = (store.control / "master.key").read_bytes()
        self.lock = None
        self.connections = set()
        self.admit = None

    def get(self, job):
        row = self.store.latest("job.state", job)
        if not row:
            raise ValueError("Unknown job")
        return row["data"]

    def states(self):
        latest = {}
        for row in self.store.events("job.state"):
            latest[row["subject"]] = row["data"]
        return list(latest.values())

    def state(self, job, status, **extra):
        old = self.get(job)
        data = {**old, **extra, "status": status}
        if data == old:
            return data
        row = self.store.event("job.state", job, data, key=f"job-state:{job}:{uid('revision')}")
        atomic(self.directory(job) / "status.json", encoded(data))
        return row["data"]

    def directory(self, job):
        record = self.get(job)
        return self.store.root / "workspace/jobs" / self.actor(record).scope / identifier(job)

    def token(self, job):
        return hmac.new(self.secret, job.encode(), hashlib.sha256).hexdigest()

    def actor(self, job):
        return Actor(job["agent"], job.get("branch"))

    def submit(
        self, actor, name, arguments, *, job_id=None, turn=None, wake=True, parent=None, frozen=None
    ):
        identifier(name)
        identity = self.agents.get(actor)
        if name == "create_agent" and actor != Actor("astra"):
            raise PermissionError("Only continuous Astra may create persistent agents")
        if identity["role"] in ("terra", "luna") and name in (
            "bash",
            "edit_file",
            "fork_self",
            "create_agent",
        ):
            raise PermissionError("Context readers cannot perform substantive work")
        job_id = identifier(job_id or uid("job"))
        existing = self.store.latest("job.state", job_id)
        if existing:
            old = existing["data"]
            if (old["agent"], old.get("branch"), old["name"], old["arguments"]) != (
                actor.agent,
                actor.branch,
                name,
                arguments,
            ):
                raise ValueError("Job identity was reused for a different invocation")
            return old
        path = self.agents.path(actor) / "tools" / (name + ".py")
        if path.is_symlink() or not path.resolve().is_relative_to(
            self.agents.path(actor).resolve()
        ):
            raise PermissionError("Tool source escapes this agent's directory")
        meta = frozen or definition(path)
        if meta["name"] != name:
            raise ValueError("Frozen tool name differs from invocation")
        jsonschema.validate(arguments, meta["schema"]["parameters"])
        source = meta.get("source") or self.store.put(path.read_bytes(), "text/x-python")
        data = {
            "id": job_id,
            "agent": actor.agent,
            "branch": actor.branch,
            "name": name,
            "arguments": arguments,
            "source": source,
            "schema": meta["schema"],
            "status": "prepared",
            "turn": turn,
            "wake": wake,
            "parent": parent,
        }
        self.store.event("job.state", job_id, data, key="job:" + job_id)
        directory = self.directory(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        atomic(directory / "tool.py", self.store.read(source))
        atomic(directory / "request.json", encoded(data))
        return data

    async def start(self):
        self.lock = (self.store.control / "runtime.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise RuntimeError("A runner already owns this workspace") from None
        self.server = await asyncio.start_server(
            self._connection, "127.0.0.1", 0, limit=128 * 1024 * 1024
        )
        self.address = list(self.server.sockets[0].getsockname()[:2])
        atomic(
            self.store.control / "broker.json",
            encoded({"address": self.address, "pid": os.getpid()}),
        )
        for job in self.states():
            if job["status"] in ("running", "launching"):
                self.state(
                    job["id"],
                    "uncertain",
                    error="Previous executor stopped before a terminal receipt; inspect before retry",
                )

    async def _connection(self, reader, writer):
        connection = asyncio.current_task()
        self.connections.add(connection)
        token = ""
        try:
            packet = json.loads(await asyncio.wait_for(reader.readline(), 30))
            job = self.get(packet["job"])
            token = self.token(job["id"])
            if not hmac.compare_digest(str(packet.get("token", "")), token):
                raise PermissionError("Invalid execution credential")
            if job["status"] not in ("running", "launching"):
                raise PermissionError("Execution credential is no longer active")
            value = await self.api(job, packet["action"], packet.get("arguments", {}))
            response = {"value": value}
        except asyncio.CancelledError:
            writer.close()
            self.connections.discard(connection)
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise
        except Exception as exc:
            response = {
                "error": type(exc).__name__ + ": " + str(exc).replace(token, "[REDACTED]")
                if token
                else type(exc).__name__ + ": " + str(exc)
            }
        try:
            writer.write(encoded(response) + b"\n")
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self.connections.discard(connection)

    async def api(self, job, action, arguments):
        if action in ("finish", "finish_child"):
            if action == "finish_child" and not job.get("parent"):
                raise PermissionError("Not a child invocation")
            if "error" in arguments:
                return self.state(job["id"], "failed", error=self.clean(arguments["error"]))
            value = self.clean(arguments.get("value"))
            return self.state(job["id"], "completed", result=self.store.put(value))
        if action == "prepare_child":
            if self.admit and not self.admit(job):
                raise PermissionError("New owner input superseded this unstarted child action")
            ordinal = arguments["ordinal"]
            if type(ordinal) is not int or ordinal < 1:
                raise ValueError("Invalid child ordinal")
            child = self.submit(
                self.actor(job),
                arguments["name"],
                arguments["arguments"],
                job_id=job["id"] + "_c" + str(ordinal),
                turn=job["turn"],
                wake=False,
                parent=job["id"],
            )
            if child["status"] == "completed":
                return {"result": self.store.json(child["result"])}
            if child["status"] != "prepared":
                raise RuntimeError("Child invocation already started; inspect its receipt")
            self.state(child["id"], "running", executor=job["id"])
            return {
                "job": child["id"],
                "token": self.token(child["id"]),
                "script": str(self.directory(child["id"]) / "tool.py"),
            }
        if action in ("result", "cancel"):
            target = self.get(arguments["job_id"])
            if self.actor(target) != self.actor(job):
                raise PermissionError("Job belongs to another execution identity")
            if action == "cancel":
                result = await self.cancel(target["id"])
                if arguments.get("reason"):
                    latest = self.get(target["id"])
                    self.state(
                        target["id"], latest["status"], error=self.clean(str(arguments["reason"]))
                    )
                return result
            if target["id"] == job["id"]:
                raise ValueError("A job cannot wait for its own completion")
            if arguments.get("wait"):
                seen, cursor = {job["id"]}, target
                while cursor["status"] not in TERMINAL:
                    if cursor["id"] in seen:
                        raise ValueError("Cyclic tool dependency; no useful result can wake it")
                    seen.add(cursor["id"])
                    if not cursor.get("waiting_on"):
                        break
                    cursor = self.get(cursor["waiting_on"])
                self.state(
                    job["id"],
                    job["status"],
                    waiting_on=target["id"] if target["status"] not in TERMINAL else None,
                )
            return {
                "id": target["id"],
                "status": target["status"],
                "output": self.store.json(target["result"]) if target.get("result") else None,
                "error": target.get("error"),
            }
        handler = self.handlers.get(action)
        if handler is None:
            raise PermissionError("API capability is not available: " + action)
        return await handler(self.actor(job), job, arguments)

    def clean(self, value):
        if isinstance(value, str):
            for state in self.states():
                value = value.replace(self.token(state["id"]), "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        return value

    async def launch(self, job):
        actor = self.actor(job)
        root = self.agents.path(actor)
        temporary = root / "work/.tmp"
        temporary.mkdir(exist_ok=True)
        code_root = Path(__file__).resolve().parents[2]
        script = self.directory(job["id"]) / "tool.py"
        if not script.exists():
            atomic(script, self.store.read(job["source"]))
        read_paths = [
            "/System",
            "/usr",
            "/bin",
            "/sbin",
            "/dev",
            "/opt/homebrew",
            sys.base_prefix,
            Path(sys.prefix),
            code_root / "src",
            code_root / "lib",
            root,
            self.directory(job["id"]).parent,
        ]
        read_paths.extend(self.agents.read_roots(actor))
        write_paths = (
            [root / "work", root / "tools"]
            if self.agents.get(actor)["role"] in ("astra", "sol")
            else [temporary]
        )
        profile = sandbox_profile(read_paths, write_paths)
        command = [sys.executable, "-m", "bloom41.worker"]
        if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
            command = ["/usr/bin/sandbox-exec", "-p", profile, *command]
        elif not self.trusted_tools:
            self.state(
                job["id"],
                "failed",
                error="No OS tool sandbox available; use explicit trusted-tools mode only for trusted scripts",
            )
            return
        pipe_read, pipe_write = os.pipe()
        spec = {
            "actor": actor.agent,
            "scope": actor.scope,
            "agent_root": str(root),
            "job": job["id"],
            "address": self.address,
            "token": self.token(job["id"]),
            "script": str(script),
            "arguments": job["arguments"],
            "read_roots": [str(p) for p in self.agents.read_roots(actor)],
            "shared_root": str(self.store.root / "workspace/shared"),
        }
        env = {
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            "TMPDIR": str(temporary),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(code_root / "src") + os.pathsep + str(code_root / "lib"),
        }
        self.state(job["id"], "launching")
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                str(pipe_read),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=root / "work",
                env=env,
                pass_fds=(pipe_read,),
                start_new_session=True,
            )
            os.close(pipe_read)
            stamp = await self.process_stamp(proc.pid)
            self.state(job["id"], "running", pid=proc.pid, process_stamp=stamp, launched_at=now())
            self.running[job["id"]] = (
                proc,
                asyncio.create_task(self.finish_process(job["id"], proc)),
            )
            with os.fdopen(pipe_write, "wb") as stream:
                stream.write(encoded(spec))
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(pipe_read)
            with contextlib.suppress(OSError):
                os.close(pipe_write)
            if self.get(job["id"])["status"] == "launching":
                self.state(
                    job["id"],
                    "uncertain",
                    error="Process launch interrupted; inspect receipts before retry",
                )
            raise

    @staticmethod
    async def process_stamp(pid):
        proc = await asyncio.create_subprocess_exec(
            "/bin/ps", "-p", str(pid), "-o", "lstart=", stdout=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        return out.decode().strip()

    async def finish_process(self, job_id, proc):
        output, _ = await proc.communicate()
        cleaned = self.clean(output.decode(errors="replace"))
        atomic(self.directory(job_id) / "process.log", cleaned.encode())
        job = self.get(job_id)
        if job["status"] not in TERMINAL:
            self.state(
                job_id,
                "failed" if proc.returncode else "uncertain",
                error="Executor exited without a result receipt",
                exit_code=proc.returncode,
            )
        else:
            self.state(job_id, job["status"], exit_code=proc.returncode)
        pending = {job_id}
        while pending:
            children = [
                j
                for j in self.states()
                if j.get("parent") in pending and j["status"] not in TERMINAL
            ]
            pending = {j["id"] for j in children}
            for child in children:
                self.state(
                    child["id"],
                    "uncertain",
                    error="Parent executor stopped before the child result receipt",
                )

    async def pump(self):
        for ident, (_, task) in list(self.running.items()):
            if task.done():
                await task
                del self.running[ident]
            elif (
                self.timeout
                and time.time() - datetime.fromisoformat(self.get(ident)["launched_at"]).timestamp()
                > self.timeout
            ):
                await self.cancel(ident)
                self.state(
                    ident, "cancelled", error="Tool execution deadline expired", reason="deadline"
                )
        for job in self.states():
            occupied = sum(not self.get(ident).get("waiting_on") for ident in self.running)
            if occupied >= self.limit:
                break
            if job["status"] == "prepared" and not job.get("parent"):
                self.store.sync()
                if self.admit:
                    allowed = self.admit(job)
                    if allowed is False:
                        self.state(job["id"], "cancelled", reason="Superseded by newer owner input")
                        continue
                    if allowed is None:
                        continue
                await self.launch(job)

    async def cancel(self, job_id):
        job = self.get(job_id)
        if job["status"] in TERMINAL:
            return {"status": job["status"]}
        if job.get("parent"):
            # Children share one isolated executor. Stopping one stops that executor,
            # with explicit terminal/uncertain receipts for its siblings.
            return await self.cancel(job["parent"])
        if job["status"] == "prepared":
            self.state(job_id, "cancelled", reason="Cancelled before execution")
        elif job_id in self.running:
            proc, task = self.running[job_id]
            if proc.returncode is None and await self.process_stamp(proc.pid) == job.get(
                "process_stamp"
            ):
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(asyncio.shield(task), 3)
                except TimeoutError:
                    if proc.returncode is None and await self.process_stamp(proc.pid) == job.get(
                        "process_stamp"
                    ):
                        os.killpg(proc.pid, signal.SIGKILL)
                    await task
            self.state(job_id, "cancelled", reason="Owned process stopped")
        else:
            raise RuntimeError("Unknown process ownership; inspect instead of signalling")
        return {"status": self.get(job_id)["status"]}

    async def close(self):
        for ident in list(self.running):
            proc, task = self.running[ident]
            if self.get(ident)["status"] not in TERMINAL:
                await self.cancel(ident)
            else:
                try:
                    await asyncio.wait_for(asyncio.shield(task), 3)
                except TimeoutError:
                    job = self.get(ident)
                    if proc.returncode is None and await self.process_stamp(proc.pid) == job.get(
                        "process_stamp"
                    ):
                        os.killpg(proc.pid, signal.SIGTERM)
                    await task
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        connections = list(self.connections)
        for task in connections:
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        (self.store.control / "broker.json").unlink(missing_ok=True)
        if self.lock:
            self.lock.close()
            self.lock = None
