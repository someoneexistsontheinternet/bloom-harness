"""Detached local process supervisor. It writes receipts and never buys model calls."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from .store import atomic, encoded, now


def identity(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def main():
    directory = Path(sys.argv[1]).resolve()
    # O_EXCL prevents accidental duplicate supervisors for one job identity.
    try:
        fd = os.open(directory / "claimed", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        raise SystemExit("Job already claimed; inspect its receipt")
    spec = json.loads((directory / "spec.json").read_text())
    atomic(
        directory / "started.json",
        encoded({"pid": os.getpid(), "identity": identity(os.getpid()), "at": now()}),
    )
    child = None

    def stop(signum, frame):
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    try:
        environment = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
        environment.update(OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", PYTHONUNBUFFERED="1")
        with (
            (directory / "stdout.txt").open("wb") as out,
            (directory / "stderr.txt").open("wb") as err,
        ):
            child = subprocess.Popen(
                spec["argv"],
                cwd=spec["cwd"],
                env=environment,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            code = child.wait()
        result = {"status": "completed", "exit_code": code, "at": now()}
    except Exception as exc:
        result = {"status": "failed", "error": type(exc).__name__, "at": now()}
    atomic(directory / "done.json", encoded(result))


if __name__ == "__main__":
    main()
