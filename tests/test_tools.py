import asyncio
import json
import sys

import pytest

from bloom41.agents import Actor, Agents
from bloom41.jobs import Jobs, TERMINAL
from bloom41.store import Store
from bloom41.tool import definition, discover


async def settled(jobs, ident):
    for _ in range(200):
        await jobs.pump()
        if jobs.get(ident)["status"] in TERMINAL:
            return jobs.get(ident)
        await asyncio.sleep(0.025)
    raise AssertionError("Tool did not settle")


async def test_real_tool_files_and_bash_and_idempotent_start(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    jobs = Jobs(store, trusted_tools=sys.platform != "darwin")
    await jobs.start()
    try:
        edit = jobs.submit(
            Actor("astra"),
            "edit_file",
            {"path": "hello.txt", "text": "first\r\nsecond\n"},
            job_id="edit1",
        )
        result = await settled(jobs, edit["id"])
        assert result["status"] == "completed", result
        (agents.path("astra") / "tools/edit_file.py").unlink()
        same = jobs.submit(Actor("astra"), "edit_file", edit["arguments"], job_id="edit1")
        assert same["status"] == "completed"
        read = jobs.submit(Actor("astra"), "read_file", {"path": "hello.txt", "start": 2})
        done = await settled(jobs, read["id"])
        assert store.json(done["result"])["text"] == "second\n"
        command = jobs.submit(
            Actor("astra"), "bash", {"command": "printf 'worked'; printf 'warning' >&2; exit 7"}
        )
        done = await settled(jobs, command["id"])
        assert done["status"] == "completed", done
        assert store.json(done["result"])["exit_code"] == 7
        assert store.json(done["result"])["stdout"] == "worked"
    finally:
        await jobs.close()
        store.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Actual macOS isolation check")
async def test_worker_cannot_read_control_or_other_agent_or_claim_identity(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    agents.create("markets", "sol", "Markets", principal=Actor("astra"))
    other = agents.path("astra") / "work/private.txt"
    other.write_text("private fixture, not a real secret")
    code = '''from pathlib import Path
async def run(tool, paths: list[str]):
    """Test real filesystem boundaries."""
    results = []
    for path in paths:
        try:
            Path(path).read_bytes()
            results.append("readable")
        except PermissionError:
            results.append("denied")
    observed = await tool.request("identity", claimed="astra")
    return {"access":results,"actor":observed}
'''
    (agents.path("markets") / "tools/probe.py").write_text(code)

    async def identity(actor, job, args):
        return actor.agent

    jobs = Jobs(store, {"identity": identity})
    await jobs.start()
    try:
        job = jobs.submit(
            Actor("markets"), "probe", {"paths": [str(store.control / "master.key"), str(other)]}
        )
        result = await settled(jobs, job["id"])
        assert result["status"] == "completed", result
        assert store.json(result["result"]) == {"access": ["denied", "denied"], "actor": "markets"}
    finally:
        await jobs.close()
        store.close()


def test_discovery_never_imports_tool_code(tmp_path):
    path = tmp_path / "example.py"
    path.write_text(
        'raise RuntimeError("must not execute on discovery")\nasync def run(tool, query: str, count: int = 2):\n    """Example tool."""\n    pass\n'
    )
    assert definition(path)["schema"]["parameters"]["required"] == ["query"]
    assert discover(tmp_path)[0]["name"] == "example"


async def test_nested_tools_share_actor_but_have_distinct_receipts(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    (agents.path("astra") / "tools/nested.py").write_text('''async def run(tool):
    """Prepare another tool result."""
    await tool.invoke("edit_file", {"path":"child.txt","text":"child result"})
    return await tool.invoke("read_file", {"path":"child.txt"})
''')
    jobs = Jobs(store, trusted_tools=sys.platform != "darwin")
    await jobs.start()
    try:
        job = jobs.submit(Actor("astra"), "nested", {})
        done = await settled(jobs, job["id"])
        assert done["status"] == "completed", done
        assert store.json(done["result"])["text"] == "child result"
        children = [j for j in jobs.states() if j.get("parent") == job["id"]]
        assert len(children) == 2 and all(j["status"] == "completed" for j in children)
    finally:
        await jobs.close()
        store.close()


async def test_cancel_owned_real_process_and_exclusive_runner(tmp_path):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    jobs = Jobs(store, trusted_tools=sys.platform != "darwin")
    await jobs.start()
    try:
        competing = Jobs(store)
        with pytest.raises(RuntimeError, match="already owns"):
            await competing.start()
        job = jobs.submit(Actor("astra"), "bash", {"command": "sleep 30"})
        await jobs.pump()
        await asyncio.sleep(0.15)
        assert jobs.get(job["id"])["status"] == "running"
        await jobs.cancel(job["id"])
        assert jobs.get(job["id"])["status"] == "cancelled"
        assert jobs.running[job["id"]][0].returncode is not None
    finally:
        await jobs.close()
        store.close()
