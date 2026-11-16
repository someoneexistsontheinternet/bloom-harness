import json
from pathlib import Path
import sys
import time

import pytest

from bloom4.agents import Agents
from bloom4.source import outline
from bloom4.store import Store
from bloom4.tools import Tools


def setup(tmp_path):
    store = Store(tmp_path); agents = Agents(store); agents.bootstrap()
    agents.create("builder", "sol", "Build a tiny example")
    agents.create("reader", "luna", "Read the builder's work", parent="builder")
    return store, Tools(store, allow_processes=True)


def call(name, args, ident):
    return {"name": name, "arguments": json.dumps(args), "call_id": ident}


def test_outline_keeps_top_level_and_decorated_locations():
    text = 'setup()\n@wrapper\ndef work(x):\n    """Do useful work."""\n    return x\n'
    result = outline(text)
    assert result["symbols"][0]["start"] == 2
    assert result["symbols"][0]["description"] == "Do useful work."
    assert result["top_level"][0]["start"] == 1


def test_scopes_hashes_and_receipts(tmp_path):
    store, tools = setup(tmp_path)
    write = call("write_file", {"path": "demo.py", "text": "x=1\n", "intent": "Create demo"}, "write1")
    first = tools.execute("builder", "task", write, None)
    assert tools.execute("builder", "task", write, None) == first
    assert len(store.events("tool.intent")) == 1
    assert tools.execute("reader", "task", call("read_file", {"path": "demo.py"}, "read1"), None)["text"] == "x=1\n"
    with pytest.raises(PermissionError):
        tools.execute("reader", "task", write, None)
    with pytest.raises(PermissionError):
        tools.path("builder", "../../../outside")
    with pytest.raises(ValueError, match="revision"):
        tools.execute("builder", "task", call("write_file", {"path": "demo.py", "text": "x=2", "intent": "Change"}, "write2"), None)


def test_process_survives_caller_and_has_exact_exit(tmp_path):
    store, tools = setup(tmp_path)
    result = tools.execute("builder", "task", call("run_process", {
        "argv": [sys.executable, "-c", "print('durable output')"], "intent": "Verify process receipt"}, "process1"), None)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = tools.process_status(result["handle"])
        if state["status"] != "running": break
        time.sleep(.02)
    assert state["status"] == "completed" and state["exit_code"] == 0
    assert Path(state["stdout"]).read_text() == "durable output\n"
    store.close()
    recovered = Tools(Store(tmp_path))
    assert recovered.process_status(result["handle"])["exit_code"] == 0


def test_large_read_preserves_source_without_raw_main_context(tmp_path):
    store, tools = setup(tmp_path)
    p = tmp_path / "work/builder/large.txt"; p.write_text("evidence\n" * 10000)
    result = tools.execute("builder", "task", call("read_file", {"path": "large.txt"}, "large"), None)
    assert result["curation_required"] and "text" not in result
    assert len(store.read(result["source"])) == p.stat().st_size
    assert len(tools.execute("reader", "task", call("read_file", {"path": "large.txt"}, "luna-large"), None)["text"]) == p.stat().st_size
