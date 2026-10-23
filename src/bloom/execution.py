from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

from .privacy import clean
from .store import atomic, encoded, now


XCODE_LICENSE_RECEIPT = Path("/Library/Preferences/com.apple.dt.Xcode.plist")


class UncertainEffect(RuntimeError):
    """An effect may have happened; its acknowledgement is not conclusive."""


class ProcessPending(RuntimeError):
    """Keep the native call pending without another model decision."""

    def __init__(self, process_id=None):
        self.process_id = process_id
        super().__init__(
            "Waiting for process completion" if process_id else "Waiting for process capacity"
        )


def ssh_arguments(target):
    args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=yes",
    ]
    if target.get("known_hosts"):
        args += ["-o", "UserKnownHostsFile=" + target["known_hosts"]]
    if target.get("identity_file"):
        args += ["-i", target["identity_file"]]
    if target.get("port"):
        args += ["-p", str(target["port"])]
    return args + [target["host"]]


def developer_read_paths():
    """Read only the selected Apple toolchain, including a full Xcode bundle."""
    from .toolchain import developer_directory

    path = developer_directory()
    if path is None:
        return ()
    if path == Path("/Library/Developer/CommandLineTools"):
        return (path,)
    # xcrun loads libraries from Developer and sibling framework folders.
    # Grant this app only, never all of /Applications or another toolchain.
    return (path.parent.parent,)


def sandbox(mode: str, cwd: Path, network=False, extra_write=(), private_roots=(), extra_read=()):
    if mode == "full" and network:
        return []
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
        raise RuntimeError("Restricted execution requires an OS sandbox; unavailable on this host")
    if mode == "full":
        return ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny network*)"]
    quote = lambda path: json.dumps(str(Path(path).resolve()))
    # Descendants inherit the kernel sandbox. Deny secret/config directories on reads.
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(deny file-read*)",
        "(allow file-read-metadata)",
        "(deny network*)",
    ]
    ancestors = {Path("/")}
    write_denials = []
    for path in (
        "/System",
        "/usr",
        "/bin",
        "/sbin",
        "/dev",
        "/private/etc",
        "/private/var/select",
        "/opt/homebrew",
        "/Library/Developer/CommandLineTools",
        *developer_read_paths(),
        sys.prefix,
        sys.base_prefix,
        cwd,
        *extra_read,
    ):
        # Access to a venv's alias itself is needed before following its symlink.
        for spelling in {Path(path).absolute(), Path(path).resolve()}:
            rules.append(f"(allow file-read* (subpath {json.dumps(str(spelling))}))")
            ancestors.update(spelling.parents)
    # Apple's shims verify the existing system license receipt even for Git.
    # Reading this one file does not accept a license or expose other preferences.
    rules.append(f"(allow file-read* (literal {quote(XCODE_LICENSE_RECEIPT)}))")
    ancestors.update(XCODE_LICENSE_RECEIPT.resolve().parents)
    # dyld and getcwd must traverse directory entries, including /. This grants
    # traversal/listing of ancestors, not reads of files in sibling projects.
    for path in sorted(ancestors, key=str):
        rules.append(f"(allow file-read* (literal {quote(path)}))")
    for path in (
        Path.home() / ".codex",
        Path.home() / ".ssh",
        Path.home() / ".aws",
        Path.home() / ".config",
        Path.home() / "Library/Keychains",
    ):
        rules.append(f"(deny file-read* (subpath {quote(path)}))")
        write_denials.append(f"(deny file-write* (subpath {quote(path)}))")
    for path in private_roots:
        path = Path(path).resolve()
        grants = [
            Path(p).resolve()
            for p in (cwd, *extra_write, *extra_read)
            if Path(p).resolve().is_relative_to(path)
        ]
        if grants:
            exclusions = " ".join(f"(require-not (subpath {quote(p)}))" for p in grants)
            rules.append(f"(deny file-read* (require-all (subpath {quote(path)}) {exclusions}))")
        else:
            rules.append(f"(deny file-read* (subpath {quote(path)}))")
        writable = [
            Path(p).resolve() for p in (cwd, *extra_write) if Path(p).resolve().is_relative_to(path)
        ]
        if writable:
            exclusions = " ".join(f"(require-not (subpath {quote(p)}))" for p in writable)
            write_denials.append(
                f"(deny file-write* (require-all (subpath {quote(path)}) {exclusions}))"
            )
        else:
            write_denials.append(f"(deny file-write* (subpath {quote(path)}))")
    rules.append('(allow file-write* (literal "/dev/null"))')
    if mode == "workspace":
        rules.append(f"(allow file-write* (subpath {quote(cwd)}))")
    for path in extra_write:
        rules.append(f"(allow file-read* (subpath {quote(path)}))")
        rules.append(f"(allow file-write* (subpath {quote(path)}))")
    rules.extend(write_denials)
    if network:
        rules.append("(allow network*)")
    return ["/usr/bin/sandbox-exec", "-p", "\n".join(rules)]


class Processes:
    def __init__(self, store):
        self.store = store
        self.config = store.config

    async def start(self, agent, operation, command, cwd=None, target="local", wait_seconds=1):
        self.check_wait(wait_seconds)
        record = self.store.one("SELECT * FROM agents WHERE id=?", (agent,))
        spec = self.config.data["targets"].get(target)
        if spec is None:
            raise ValueError("Unknown execution target")
        old = self.store.one("SELECT * FROM processes WHERE operation_id=?", (operation,))
        if old:
            return await self.observe(agent, old["id"], wait_seconds=wait_seconds)
        current = self.store.rows("SELECT id FROM processes WHERE status='running'")
        for row in current:
            await self._observe(row["id"])
        count = self.store.one(
            "SELECT count(*) AS n FROM processes WHERE status IN ('prepared','running')"
        )["n"]
        if count >= self.config.data["runtime"]["max_local_processes"]:
            raise ProcessPending()
        ident = "proc_" + hashlib.sha256(operation.encode()).hexdigest()[:20]
        directory = self.config.workspace / "processes" / ident
        directory.mkdir(parents=True, exist_ok=False)
        cwd = (
            Path(cwd or spec["root"])
            if spec["kind"] == "ssh"
            else Path(cwd or record["cwd"]).expanduser().resolve()
        )
        permitted = Path(record["cwd"]).resolve()
        if record["mode"] != "full" and not cwd.is_relative_to(permitted):
            raise PermissionError("Process cwd is outside assigned workspace")
        env = self.config.child_env(record["mode"])
        if record["mode"] == "full":
            env = {
                k: env[k]
                for k in ("PATH", "VIRTUAL_ENV", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS")
            }
        # Secrets stay in the supervisor's inherited environment, never in its spec file.
        env = {
            k: v
            for k, v in env.items()
            if not re.search(r"(KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL)", k)
        }
        environment_names = [
            r["name"]
            for r in self.store.rows(
                "SELECT name FROM agent_environment WHERE agent_id=?", (agent,)
            )
        ]
        # A project venv is mandatory for Python workflows; make its interpreter first in PATH.
        worktree = self.store.one(
            "SELECT w.repository FROM worktrees w JOIN tasks t ON t.id=w.task_id WHERE t.agent_id=?",
            (agent,),
        )
        project_python = self.config.project_python(
            permitted, worktree["repository"] if worktree else None
        )
        env["PATH"] = str(project_python.parent) + ":" + env["PATH"]
        env["VIRTUAL_ENV"] = str(project_python.parent.parent)
        if spec["kind"] == "ssh":
            if record["mode"] != "full":
                raise PermissionError(
                    "Restricted SSH needs an installed remote sandbox; only full agents may use this target"
                )
            # The remote agent uses the same durable supervisor; transport failure never retries launch.
            remote_root = spec["root"]
            remote_dir = remote_root + "/workspace/processes/" + ident
            remote_env = {
                "PATH": str(Path(spec["python"]).parent) + ":/usr/local/bin:/usr/bin:/bin",
                "OMP_NUM_THREADS": "1",
            }
            remote_spec = {
                "argv": ["/bin/sh", "-c", command],
                "cwd": str(cwd) if cwd else remote_root,
                "environment": remote_env,
            }
            launch = "import pathlib,json,subprocess,sys; p=pathlib.Path(sys.argv[1]); p.mkdir(parents=True,exist_ok=False); (p/'spec.json').write_bytes(sys.stdin.buffer.read()); f=(p/'supervisor.log').open('ab'); subprocess.Popen([sys.executable,'-m','bloom.process_runner',str(p)],stdin=subprocess.DEVNULL,stdout=f,stderr=f,start_new_session=True)"
            argv = ssh_arguments(spec) + [shlex.join([spec["python"], "-c", launch, remote_dir])]
            self.store.db.execute(
                "INSERT INTO processes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    operation,
                    agent,
                    target,
                    str(cwd),
                    self.store.put(command),
                    None,
                    None,
                    "prepared",
                    None,
                    remote_dir,
                    now(),
                ),
            )
            self.store.db.execute(
                "INSERT INTO process_targets VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    *(
                        spec.get(k)
                        for k in (
                            "kind",
                            "host",
                            "port",
                            "root",
                            "python",
                            "identity_file",
                            "known_hosts",
                        )
                    ),
                ),
            )
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate(encoded(remote_spec))
            if proc.returncode:
                self.store.db.execute(
                    "UPDATE processes SET status='uncertain' WHERE id=?", (ident,)
                )
                raise UncertainEffect(
                    "SSH launch outcome needs reconciliation: " + clean(err.decode())[:1000]
                )
        else:
            private = (
                self.config.workspace,
                self.config.artifacts,
                self.config.root / "logs",
                project_python.parent.parent,
                project_python.resolve().parent.parent,
            )
            controls = self.config.workspace / "authoring" / agent
            if controls.resolve() != controls:
                raise PermissionError(
                    "Agent control directory must not redirect outside its assigned path"
                )
            controls.mkdir(parents=True, exist_ok=True)
            scratch = directory / "scratch"
            scratch.mkdir()
            env.update(
                TMPDIR=str(scratch),
                TMP=str(scratch),
                TEMP=str(scratch),
                TMPPREFIX=str(scratch / "bloom-heredoc"),
                PYTHONDONTWRITEBYTECODE="1",
            )
            reads = [project_python.parent.parent, project_python.resolve().parent.parent]
            from .toolchain import directory as tools_directory

            reads.append(tools_directory(self.config.root))
            if record["mode"] != "full":
                env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
                metadata = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(permitted),
                        "rev-parse",
                        "--path-format=absolute",
                        "--git-common-dir",
                    ],
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "GIT_CONFIG_GLOBAL": "/dev/null",
                        "GIT_CONFIG_NOSYSTEM": "1",
                    },
                )
                if metadata.returncode == 0:
                    reads.append(Path(metadata.stdout.strip()))
            writes = (scratch, controls) if record["mode"] == "workspace" else (scratch,)
            # POSIX emulation preserves ordinary shell commands. zsh honors the
            # explicit TMPPREFIX grant for heredocs; macOS /bin/sh did not honor
            # the permitted TMPDIR for them. No login/user startup files.
            argv = sandbox(
                record["mode"],
                permitted,
                bool(record["network"]),
                extra_write=writes,
                private_roots=private,
                extra_read=reads,
            ) + ["/bin/zsh", "--emulate", "sh", "-f", "-o", "pipefail", "-c", command]
            process_spec = {
                "argv": argv,
                "cwd": str(cwd),
                "environment": env,
                "environment_names": environment_names,
                "inherit_environment": record["mode"] == "full",
            }
            if self.store.one(
                "SELECT 1 FROM evidence_workers w JOIN tasks t ON t.id=w.task_id WHERE t.agent_id=?",
                (agent,),
            ):
                process_spec["timeout_seconds"] = self.config.data["runtime"].get(
                    "evidence_process_seconds", 20
                )
            atomic(directory / "spec.json", encoded(process_spec))
            self.store.db.execute(
                "INSERT INTO processes(id,operation_id,agent_id,target,cwd,command_hash,status,directory,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    operation,
                    agent,
                    target,
                    str(cwd),
                    self.store.put(command),
                    "prepared",
                    str(directory),
                    now(),
                ),
            )
            with (directory / "supervisor.log").open("ab") as log:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "bloom.process_runner", str(directory)],
                    cwd=self.config.root,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    env=self.config.child_env("full"),
                )
            self.store.db.execute("UPDATE processes SET pid=? WHERE id=?", (proc.pid, ident))
        self.store.db.execute("UPDATE processes SET status='running' WHERE id=?", (ident,))
        return await self.observe(agent, ident, wait_seconds=wait_seconds)

    def authorize(self, agent, process):
        row = self.store.one("SELECT * FROM processes WHERE id=?", (process,))
        if not row:
            raise ValueError("Unknown process")
        owner = row["agent_id"]
        while owner and owner != agent:
            parent = self.store.one("SELECT parent_id FROM agents WHERE id=?", (owner,))
            owner = parent["parent_id"] if parent else None
        if owner != agent:
            raise PermissionError("Process is outside this agent tree")
        return row

    async def remote(self, row, action, payload=None):
        target = self.store.one("SELECT * FROM process_targets WHERE process_id=?", (row["id"],))
        if not target:
            raise RuntimeError(
                "Remote handle lacks a frozen target identity; reconcile it before access"
            )
        program = "from bloom.execution import remote_control; import sys,json; print(json.dumps(remote_control(sys.argv[1],sys.argv[2],json.loads(sys.argv[3]))))"
        argv = ssh_arguments(target) + [
            shlex.join(
                [
                    target["python"],
                    "-c",
                    program,
                    row["directory"],
                    action,
                    json.dumps(payload or {}),
                ]
            )
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        if proc.returncode:
            if action != "observe":
                raise UncertainEffect(
                    "SSH control acknowledgement lost; inspect the existing handle before retrying input or cancellation"
                )
            raise RuntimeError(
                "SSH observation failed; original process has not been relaunched: "
                + clean(err.decode())[:500]
            )
        try:
            return json.loads(out)
        except ValueError as exc:
            if action != "observe":
                raise UncertainEffect(
                    "SSH control response was incomplete; its effect may have occurred"
                ) from exc
            raise

    async def file(self, agent, name, arguments):
        record = self.store.one("SELECT * FROM agents WHERE id=?", (agent,))
        if record["mode"] != "full":
            raise PermissionError(
                "Restricted remote files require a scoped remote executor; this target is full-access only"
            )
        target = self.config.data["targets"].get(arguments["target"])
        if not target or target["kind"] != "ssh":
            raise ValueError("Unknown SSH file target")
        program = Path(__file__).with_name("remote_files.py").read_text()
        proc = await asyncio.create_subprocess_exec(
            *(ssh_arguments(target) + [shlex.join([target["python"], "-c", program])]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(
            encoded({"name": name, "root": target["root"], **arguments})
        )
        if proc.returncode:
            error = clean(err.decode())[:1000]
            if name != "read_file":
                raise UncertainEffect("SSH file acknowledgement lost: " + error)
            raise RuntimeError(error)
        result = json.loads(out)
        if result.get("uncertain"):
            raise UncertainEffect(result["error"])
        if result.get("error"):
            raise ValueError(result["error"])
        return clean(result["result"])

    async def _observe(self, process, offset=0, limit=16000):
        row = self.store.one("SELECT * FROM processes WHERE id=?", (process,))
        result = (
            await self.remote(row, "observe", {"offset": offset, "limit": limit})
            if row["target"] != "local"
            else remote_control(row["directory"], "observe", {"offset": offset, "limit": limit})
        )
        if (
            row["target"] == "local"
            and row["status"] in ("completed", "failed")
            and "output" not in result
        ):
            from .storage import Archive, ColdStorageUnavailable

            path = Path(row["directory"]) / "output.log"
            if path.is_relative_to(self.config.root):
                archive = Archive(self.store)
                relative = str(path.relative_to(self.config.root))
                try:
                    archived = archive.range(relative, offset, min(limit, 1_000_000))
                    if archived:
                        result.update(archived)
                except ColdStorageUnavailable as exc:
                    result.update(
                        output_available=False,
                        output_error=str(exc),
                        archive_id=archive.record(relative)["id"],
                    )
        if result.get("result"):
            code = result["result"].get("exit_code")
            self.store.db.execute(
                "UPDATE processes SET status=?,exit_code=? WHERE id=?",
                ("completed" if code == 0 else "failed", code, process),
            )
        elif result.get("supervisor_lost") or (
            not result.get("identity")
            and time.time() - dt.datetime.fromisoformat(row["created_at"]).timestamp() > 15
        ):
            self.store.db.execute("UPDATE processes SET status='uncertain' WHERE id=?", (process,))
        if result.get("identity"):
            self.store.db.execute(
                "UPDATE processes SET pid=?,start_identity=? WHERE id=?",
                (result["identity"]["pid"], result["identity"]["start_identity"], process),
            )
        return {"process_id": process, **result}

    @staticmethod
    def check_wait(seconds):
        if type(seconds) not in (int, float) or not 0 <= seconds <= 60:
            raise ValueError("wait_seconds must be between 0 and 60")

    async def observe(self, agent, process, offset=0, limit=16000, *, wait_seconds=0):
        self.check_wait(wait_seconds)
        self.authorize(agent, process)
        deadline = time.monotonic() + wait_seconds
        while True:
            result = await self._observe(process, offset, limit)
            row = self.store.one("SELECT status FROM processes WHERE id=?", (process,))
            if (
                result.get("result")
                or result.get("supervisor_lost")
                or row["status"] == "uncertain"
                or time.monotonic() >= deadline
            ):
                return result
            # A bounded asynchronous wait remains interruptible by owner control.
            await asyncio.sleep(min(0.25, max(0, deadline - time.monotonic())))

    async def control(self, agent, process, action, text=""):
        row = self.authorize(agent, process)
        if row["target"] != "local":
            return await self.remote(row, action, {"text": text})
        return remote_control(row["directory"], action, {"text": text})


def remote_control(directory, action, payload):
    directory = Path(directory)
    if action == "observe":
        result = {}
        for name in ("identity", "result"):
            path = directory / (name + ".json")
            if path.exists():
                result[name] = json.loads(path.read_text())
        if result.get("identity") and not result.get("result"):
            actual = subprocess.run(
                ["ps", "-p", str(result["identity"]["pid"]), "-o", "lstart="],
                capture_output=True,
                text=True,
            ).stdout.strip()
            result["supervisor_lost"] = actual != result["identity"]["start_identity"]
        path = directory / "output.log"
        offset, limit = (
            int(payload.get("offset", 0)),
            min(int(payload.get("limit", 16000)), 1_000_000),
        )
        if offset < 0 or limit < 1:
            raise ValueError("Invalid output range")
        if path.exists():
            with path.open("rb") as file:
                file.seek(offset)
                data = file.read(limit)
                result.update(
                    output=data.decode(errors="replace"),
                    next_offset=file.tell(),
                    total_bytes=path.stat().st_size,
                )
        return result
    identity = json.loads((directory / "identity.json").read_text())
    actual = subprocess.run(
        ["ps", "-p", str(identity["pid"]), "-o", "lstart="], capture_output=True, text=True
    ).stdout.strip()
    if actual != identity["start_identity"]:
        raise RuntimeError("Supervisor identity changed; no process was signalled")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(3)
        address = (
            "/tmp/bloom-v3-"
            + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24]
            + ".sock"
        )
        client.connect(address)
        try:
            client.sendall(encoded({"action": action, **payload}) + b"\n")
            return json.loads(client.recv(65536))
        except (OSError, ValueError) as exc:
            raise UncertainEffect(
                "Process control acknowledgement lost; inspect the existing handle before retrying input or cancellation"
            ) from exc
