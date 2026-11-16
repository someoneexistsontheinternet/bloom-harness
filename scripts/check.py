"""Run explicit compile/test checks with retained, concise local receipts."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--label", required=True)
parser.add_argument("--tests", nargs="+", default=["tests"])
parser.add_argument("files", nargs="+")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
assert Path(sys.prefix).resolve() == (root / ".venv").resolve(), "Use project .venv"
run = "local-" + dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
log = root / "logs/codex-misc" / args.label / run
log.mkdir(parents=True)
commands = [
    [sys.executable, "-m", "py_compile", *args.files],
    [sys.executable, "-m", "pytest", "--basetemp", str(log / "fixtures"), *args.tests],
]
receipt = {
    "run": run,
    "cwd": str(root),
    "interpreter": sys.executable,
    "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
    "commands": [],
}
for i, command in enumerate(commands):
    with (log / f"{i}.log").open("w") as output:
        result = subprocess.run(
            command,
            cwd=root,
            stdout=output,
            stderr=subprocess.STDOUT,
            env={**os.environ, "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"},
        )
    receipt["commands"].append({"argv": command, "exit": result.returncode})
    (log / "receipt.json").write_text(json.dumps(receipt, indent=2))
    if result.returncode:
        print((log / f"{i}.log").read_text()[-12000:])
        print("FAILED", log)
        raise SystemExit(result.returncode)
print((log / "1.log").read_text().strip())
print("PASS", log)
