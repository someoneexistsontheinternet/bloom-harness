"""Small offline CI checks; models are scripted and Safari is never launched."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    expected = root / ".venv/bin/python"
    if Path(sys.prefix).resolve() != (root / ".venv").resolve():
        raise SystemExit("Run this check with the project's .venv/bin/python")
    run = datetime.now(timezone.utc).strftime("local-%Y%m%d_%H%M%S_%f")
    logs = root / "logs/codex-misc/check" / run
    logs.mkdir(parents=True)
    commands = [
        [str(expected.parent / "ruff"), "check", "src", "lib", "tools", "tests", "scripts"],
        [
            str(expected.parent / "ruff"),
            "format",
            "--check",
            "src",
            "lib",
            "tools",
            "tests",
            "scripts",
        ],
        [str(expected), "-m", "pytest", "--basetemp", str(logs / "fixtures")],
    ]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    changes = subprocess.check_output(["git", "status", "--short"], cwd=root, text=True)
    receipts = {
        "revision": revision,
        "changes": changes,
        "interpreter": sys.executable,
        "root": str(root),
        "external_model_requests": 0,
        "commands": [],
    }
    for index, command in enumerate(commands):
        path = logs / f"{index:02d}.log"
        with path.open("w") as stream:
            result = subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT)
        receipts["commands"].append(
            {"argv": command, "exit_code": result.returncode, "log": str(path)}
        )
        (logs / "run.json").write_text(json.dumps(receipts, indent=2))
        print(
            json.dumps({"check": index, "exit_code": result.returncode, "log": str(path)}),
            flush=True,
        )
        if result.returncode:
            print(path.read_text()[-3500:])
            return result.returncode
    print("Offline checks passed; no live inference or browser launch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
