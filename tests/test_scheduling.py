"""Independent identities progress; waiting consumes no extra model decisions."""

import asyncio
import sys

from bloom41.agents import Actor, Agents
from bloom41.jobs import Jobs, TERMINAL
from bloom41.loop import Loop
from bloom41.store import Store
from test_models_context import Scripted
from test_tools import settled


async def test_all_ready_actors_start_and_each_identity_has_only_one_active_turn(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    class Provider:
        simulated = True

        async def generate(self, request):
            requests.append(request)
            if len(requests) == 6:
                entered.set()
            await release.wait()
            return {"status": "completed", "output": []}, []

    loop = Loop(tmp_path, Provider(), heartbeat=0.01)
    actors = ["astra", *[f"sol-{n}" for n in range(5)]]
    for name in actors[1:]:
        loop.agents.create(name, "sol", "Independent work", principal=Actor("astra"))
    for name in actors:
        loop.messages.deliver("tool:test", name, "Ready", kind="tool_result", message_id=name)
    loop.models.grant(20)
    task = asyncio.create_task(loop.run(seconds=5))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        loop.messages.deliver(
            "tool:test", "astra", "Later evidence", kind="tool_result", message_id="later"
        )
        await asyncio.sleep(0.08)
        assert len(requests) == 6
        assert len(loop.active) == 6
        release.set()
        await task
        assert len(requests) == 7
        assert all(t["status"] == "completed" and t["idle"] for t in loop.turns())
        assert not loop.messages.pending("astra")
        await loop.run()
        assert len(requests) == 7
    finally:
        release.set()
        await task
        loop.store.close()


async def test_budget_grant_resumes_only_unstarted_turn_and_empty_response_parks(tmp_path):
    provider = Scripted({"status": "completed", "output": []})
    loop = Loop(tmp_path, provider, heartbeat=0.01)
    loop.messages.deliver(
        "tool:test", "astra", "Evidence", kind="tool_result", message_id="evidence"
    )
    await loop.run(seconds=2)
    (blocked,) = loop.turns(Actor("astra"))
    assert blocked["budget"] and blocked["status"] == "blocked"
    assert not provider.requests
    loop.models.grant(1)
    await loop.run(seconds=2)
    assert loop.turn(blocked["id"])["status"] == "completed"
    assert len(provider.requests) == len(loop.store.events("model.started")) == 1
    assert not loop.messages.pending("astra")
    await loop.run()
    assert len(provider.requests) == 1
    loop.store.close()


async def test_nested_model_and_browser_waits_release_local_tool_launch_budget(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    scripts = agents.path("astra") / "tools"
    (scripts / "remote.py").write_text('''import asyncio
async def run(tool):
    """Two independent external waits in one nested tool."""
    return await asyncio.gather(tool.model({}), tool.service("fixture"))
''')
    (scripts / "nested.py").write_text('''async def run(tool):
    """A root executor delegates to a nested reader."""
    return await tool.invoke("remote", {})
''')
    arrived, release = set(), asyncio.Event()

    async def remote(actor, job, arguments):
        arrived.add((job["id"], "model" if "request" in arguments else "service"))
        await release.wait()
        return "done"

    jobs = Jobs(
        store,
        {"model": remote, "service": remote},
        trusted_tools=sys.platform != "darwin",
        limit=1,
    )
    await jobs.start()
    try:
        roots = [jobs.submit(Actor("astra"), "nested", {}) for _ in range(3)]
        for _ in range(150):
            await jobs.pump()
            if len(arrived) == 6:
                break
            await asyncio.sleep(0.02)
        assert len(arrived) == 6
        assert jobs.external_waits == {j["id"]: 2 for j in roots}
        local = jobs.submit(Actor("astra"), "bash", {"command": "printf local-work"})
        done = await settled(jobs, local["id"])
        assert done["status"] == "completed"
        assert store.json(done["result"])["stdout"] == "local-work"
        release.set()
        for job in roots:
            assert (await settled(jobs, job["id"]))["status"] == "completed"
        assert not jobs.external_waits
    finally:
        release.set()
        await jobs.close()
        assert not jobs.external_waits
        assert all(j["status"] in TERMINAL for j in jobs.states())
        store.close()


async def test_cancelled_external_wait_releases_accounting(tmp_path):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    entered = asyncio.Event()

    async def hold(*args):
        entered.set()
        await asyncio.Event().wait()

    jobs = Jobs(store, {"model": hold})
    job = jobs.submit(Actor("astra"), "read_file", {"path": "memory/L0.md"})
    task = asyncio.create_task(jobs.api(job, "model", {}))
    await entered.wait()
    assert jobs.external_waits == {job["id"]: 1}
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not jobs.external_waits
    store.close()
