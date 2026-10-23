"""Detached process supervisor. No network calls; one explicitly supplied spec."""

from __future__ import annotations

import json
import contextlib
import hashlib
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .privacy import clean, StreamRedactor
from .store import atomic, encoded


def main():
    directory = Path(sys.argv[1]).resolve()
    spec = json.loads((directory / "spec.json").read_text())
    server = socket.socket(socket.AF_UNIX)
    # AF_UNIX has a short pathname limit on macOS. This socket is expendable;
    # all process identity, output and results remain in the project directory.
    address = "/tmp/bloom-v3-" + hashlib.sha256(str(directory).encode()).hexdigest()[:24] + ".sock"
    server.bind(address)
    os.chmod(address, 0o600)
    server.listen(4)
    server.setblocking(False)
    proc = None

    def stop(signum, frame):
        if proc and proc.poll() is None:
            proc.terminate()

    signal.signal(signal.SIGTERM, stop)
    start = subprocess.check_output(
        ["ps", "-p", str(os.getpid()), "-o", "lstart="], text=True
    ).strip()
    atomic(
        directory / "identity.json",
        encoded({"pid": os.getpid(), "start_identity": start, "started_at": time.time()}),
    )
    try:
        environment = dict(os.environ) if spec.get("inherit_environment") else {}
        # Bloom's frozen service import path must not make candidate tests import
        # the deployed code instead of their assigned checkout.
        environment.pop("PYTHONPATH", None)
        environment.pop("BLOOM_SOURCE_ROOT", None)
        for name in spec.get("environment_names", []):
            if name in os.environ:
                environment[name] = os.environ[name]
        environment.update(spec["environment"])
        proc = subprocess.Popen(
            spec["argv"],
            cwd=spec["cwd"],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        atomic(directory / "child.json", encoded({"pid": proc.pid}))
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, "output")
        selector.register(server, selectors.EVENT_READ, "control")
        redactor = StreamRedactor()
        deadline = (
            time.monotonic() + spec["timeout_seconds"] if spec.get("timeout_seconds") else None
        )
        timed_out = False
        terminate_at = None
        with (directory / "output.log").open("ab", buffering=0) as log:
            while proc.poll() is None or selector.get_map().get(proc.stdout.fileno()):
                if deadline and time.monotonic() >= deadline and proc.poll() is None:
                    if terminate_at is None:
                        timed_out = True
                        terminate_at = time.monotonic()
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGTERM)
                    elif time.monotonic() - terminate_at >= 2:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGKILL)
                for key, _ in selector.select(0.2):
                    if key.data == "output":
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk:
                            selector.unregister(proc.stdout)
                        log.write(redactor.feed(chunk, final=not chunk))
                    else:
                        client, _ = server.accept()
                        client.settimeout(2)
                        with client:
                            payload = b""
                            while b"\n" not in payload and len(payload) < 65536:
                                data = client.recv(4096)
                                if not data:
                                    break
                                payload += data
                            try:
                                request = json.loads(payload)
                                if request["action"] == "input":
                                    proc.stdin.write(request["text"].encode())
                                    proc.stdin.flush()
                                elif request["action"] == "eof":
                                    proc.stdin.close()
                                elif request["action"] == "cancel":
                                    # Child PID belongs to this still-running Popen, its group is task-owned.
                                    if proc.poll() is None:
                                        os.killpg(proc.pid, signal.SIGTERM)
                                else:
                                    raise ValueError("Unknown process action")
                                client.sendall(b'{"ok":true}\n')
                            except (ValueError, OSError) as exc:
                                client.sendall(encoded({"error": clean(str(exc))}) + b"\n")
            code = proc.wait()
            result = {"exit_code": code, "finished_at": time.time()}
            if timed_out:
                result.update(
                    exit_code=124,
                    process_exit_code=code,
                    timed_out=True,
                    timeout_seconds=spec["timeout_seconds"],
                )
            atomic(directory / "result.json", encoded(result))
    except BaseException as exc:
        atomic(
            directory / "result.json",
            encoded({"exit_code": None, "error": clean(str(exc)), "finished_at": time.time()}),
        )
        raise
    finally:
        if proc and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        server.close()
        Path(address).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
