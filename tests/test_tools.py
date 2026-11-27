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


def test_redaction_snapshots_jobs_once_per_value_and_refreshes_between_values(
    tmp_path, monkeypatch
):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    jobs = Jobs(store)
    first = jobs.submit(Actor("astra"), "read_file", {"path": "memory/L0.md"})
    token = jobs.token(first["id"])
    original, reads = jobs.states, []

    def states():
        reads.append(True)
        return original()

    monkeypatch.setattr(jobs, "states", states)
    value = {"nested": ["prefix " + token, {"text": token * 2}, None, 3, ""]}
    assert jobs.clean(value) == {
        "nested": ["prefix [REDACTED]", {"text": "[REDACTED][REDACTED]"}, None, 3, ""]
    }
    assert value["nested"][0] == "prefix " + token
    assert len(reads) == 1
    second = jobs.submit(Actor("astra"), "read_file", {"path": "memory/L1.md"})
    assert jobs.clean(jobs.token(second["id"]) + token) == "[REDACTED][REDACTED]"
    assert len(reads) == 2
    store.close()


async def test_long_tools_leave_room_for_interactive_file_delivery(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    (agents.path("astra") / "tools/slow.py").write_text('''async def run(tool):
    """A deterministic stand-in for a tool waiting on remote work."""
    return await tool.request("hold")
''')
    release = asyncio.Event()
    arrived = set()

    async def hold(actor, job, args):
        arrived.add(job["id"])
        await release.wait()
        return "settled"

    jobs = Jobs(store, {"hold": hold}, trusted_tools=sys.platform != "darwin", limit=4)
    await jobs.start()
    try:
        slow = [jobs.submit(Actor("astra"), "slow", {}) for _ in range(4)]
        for _ in range(100):
            await jobs.pump()
            if len(arrived) >= 3:
                break
            await asyncio.sleep(0.02)
        assert len(arrived) >= 3
        quick = jobs.submit(Actor("astra"), "read_file", {"path": "memory/L0.md"})
        for _ in range(100):
            await jobs.pump()
            assert len(jobs.running) <= 4
            if jobs.get(quick["id"])["status"] in TERMINAL:
                break
            await asyncio.sleep(0.02)
        assert jobs.get(quick["id"])["status"] == "completed"
        assert len(arrived) == 3
        assert jobs.get(slow[-1]["id"])["status"] == "prepared"
        release.set()
        assert (await settled(jobs, slow[-1]["id"]))["status"] == "completed"
    finally:
        release.set()
        await jobs.close()
        store.close()


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
        empty = jobs.submit(Actor("astra"), "read_file", {"path": "memory/L0.md"})
        done = await settled(jobs, empty["id"])
        assert done["status"] == "completed", done
        assert store.json(done["result"])["text"] == ""
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
    assert definition(path)["schema"]["parameters"]["properties"]["count"]["default"] == 2
    assert discover(tmp_path)[0]["name"] == "example"


def test_native_search_schema_exposes_valid_capture_sizes_and_defaults():
    import jsonschema
    from bloom41.agents import resource_directory

    schema = definition(resource_directory("bundled") / "search.py")["schema"]["parameters"]
    assert schema["properties"]["width"] == {"type": "integer", "enum": [512, 1024], "default": 512}
    assert schema["properties"]["height"]["default"] == 2048
    for height in (512, 1024, 2048, 4096):
        jsonschema.validate({"query": "fixture", "width": 1024, "height": height}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"query": "fixture", "width": 1400, "height": 900}, schema)


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


async def test_waiting_sender_does_not_starve_queued_dependency(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    (agents.path("astra") / "tools/waiting.py").write_text('''async def run(tool, dependency: str):
    """Wait for an existing result without holding an execution slot."""
    return await tool.wait_result(dependency)
''')
    jobs = Jobs(store, limit=1, trusted_tools=sys.platform != "darwin")
    await jobs.start()
    try:
        waiter = jobs.submit(Actor("astra"), "waiting", {"dependency": "queued"}, job_id="waiter")
        jobs.submit(Actor("astra"), "bash", {"command": "printf 'ready'"}, job_id="queued")
        result = await settled(jobs, waiter["id"])
        assert result["status"] == "completed", result
        assert store.json(result["result"])["output"]["stdout"] == "ready"
    finally:
        await jobs.close()
        store.close()


async def test_command_timeout_stops_shell_descendants_and_keeps_partial_output(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    jobs = Jobs(store, trusted_tools=sys.platform != "darwin")
    await jobs.start()
    try:
        job = jobs.submit(
            Actor("astra"), "bash", {"command": "printf 'partial'; sleep 30", "timeout": 1}
        )
        result = await settled(jobs, job["id"])
        assert result["status"] == "cancelled", result
        directory = agents.path("astra") / "work/command-output" / job["id"]
        assert (directory / "stdout.txt").read_text() == "partial"
        assert result.get("exit_code") is not None
        assert "Command timeout" in result["error"]
    finally:
        await jobs.close()
        store.close()


async def test_invalid_and_revoked_execution_credentials_are_rejected(tmp_path):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    jobs = Jobs(store, trusted_tools=sys.platform != "darwin")
    await jobs.start()

    async def request(credential):
        reader, writer = await asyncio.open_connection(*jobs.address)
        writer.write(
            json.dumps(
                {
                    "job": "probe",
                    "token": credential,
                    "action": "result",
                    "arguments": {"job_id": "probe"},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        result = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return result

    try:
        jobs.submit(Actor("astra"), "bash", {"command": "sleep 30"}, job_id="probe")
        await jobs.pump()
        assert "Invalid execution credential" in (await request("invalid-fixture"))["error"]
        await jobs.cancel("probe")
        assert "no longer active" in (await request(jobs.token("probe")))["error"]
    finally:
        await jobs.close()
        store.close()


async def test_frozen_tool_source_is_used_after_the_directory_changes(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    path = agents.path("astra") / "tools/version.py"
    path.write_text(
        'async def run(tool):\n    """Report the inspected version."""\n    return "original"\n'
    )
    frozen = {**definition(path), "source": store.put(path.read_bytes(), "text/x-python")}
    path.write_text(
        'async def run(tool):\n    """Report a newer version."""\n    return "changed"\n'
    )
    jobs = Jobs(store, trusted_tools=sys.platform != "darwin")
    await jobs.start()
    try:
        job = jobs.submit(Actor("astra"), "version", {}, frozen=frozen)
        result = await settled(jobs, job["id"])
        assert store.json(result["result"]) == "original"
    finally:
        await jobs.close()
        store.close()
