import json
from pathlib import Path
import subprocess
import sys


def test_real_cli_demo_and_read_only_reopen(tmp_path):
    root = tmp_path / "runtime"
    command = [sys.executable, "-m", "bloom41.cli", "--root", str(root)]
    if sys.platform != "darwin":
        command += ["--trusted-tools"]
    result = subprocess.run([*command, "demo"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "The checked result is 4" in result.stdout
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt["external_model_requests"] == 0 and receipt["verified_user_messages"] == 1
    status = subprocess.run([*command, "status"], capture_output=True, text=True, timeout=5)
    assert status.returncode == 0, status.stderr
    value = json.loads(status.stdout)
    assert value["requests"] == receipt["scripted_requests"]
    assert not [t for t in value["turns"] if t["status"] == "blocked"]
    assert (root / "workspace/agents/calculator/outbound").is_dir()
