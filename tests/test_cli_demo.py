import json
from pathlib import Path
import subprocess
import sys

import pytest

from bloom4.provider import Responses
from bloom4.runtime import Runtime
from bloom4.store import Store
from bloom4.tools import Tools


def test_cli_demo_inspection_and_simulation_separation(tmp_path):
    workspace = tmp_path / "demo"

    def cli(*args):
        p = subprocess.run(
            [sys.executable, "-m", "bloom4.cli", "--workspace", str(workspace), *args],
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)

    demo = cli("demo")
    assert demo["simulation"] and demo["api_calls"] == 0 and demo["verified"]
    assert Path(demo["file"]).read_text().startswith("def add")
    assert all(t["status"] == "completed" for t in demo["tasks"])
    timeline = cli("timeline", "--kind", "model.started", "--limit", "1")
    assert len(timeline) == 1 and "data" not in timeline[0]
    exported = cli("export-call", timeline[0]["id"], "--to", str(tmp_path / "export"))
    assert exported["response_available"]
    assert (tmp_path / "export/request.json").is_file()
    store = Store(workspace)
    with pytest.raises(ValueError, match="simulated"):
        Runtime(store, Responses())
    store.close()


def test_empty_file_and_bad_arguments_are_not_effects(tmp_path):
    from bloom4.agents import Agents
    from bloom4.tools import arguments

    store = Store(tmp_path)
    Agents(store).bootstrap()
    (tmp_path / "work/astra/empty.txt").write_text("")
    call = {"name": "read_file", "call_id": "empty", "arguments": '{"path":"empty.txt"}'}
    assert Tools(store).execute("astra", "test", call, None)["text"] == ""
    with pytest.raises(ValueError):
        arguments("astra", {"name": "run_process", "arguments": '{"argv":[],"intent":"x"}'})
    with pytest.raises(ValueError):
        arguments("astra", {"name": "read_file", "arguments": "[]"})
