import asyncio
import sys
import pytest

from bloom41.agents import Actor
from bloom41.loop import Loop
from bloom41.models import ModelFailure
from test_models_context import Scripted, reply


async def child_started(loop, job):
    for _ in range(150):
        await loop.jobs.pump()
        children = [j for j in loop.jobs.states() if j.get("parent") == job["id"]]
        if children and children[0]["status"] == "running":
            await asyncio.sleep(0.05)
            return children[0]
        await asyncio.sleep(0.02)
    raise AssertionError("Child did not start")


def tools(loop, child_source):
    folder = loop.agents.path("astra") / "tools"
    (folder / "nested.py").write_text('''async def run(tool):
    """Run a controlled nested operation."""
    return await tool.invoke("child", {})
''')
    (folder / "child.py").write_text(child_source)


async def test_graceful_nested_cancel_records_acknowledged_terminal_children(tmp_path):
    loop = Loop(tmp_path, Scripted(), trusted_tools=sys.platform != "darwin")
    tools(
        loop,
        '''import asyncio
async def run(tool):
    """Wait locally without external effects."""
    await asyncio.Event().wait()
''',
    )
    await loop.jobs.start()
    try:
        root = loop.jobs.submit(Actor("astra"), "nested", {})
        child = await child_started(loop, root)
        result = await loop.jobs.cancel(root["id"])
        assert result["status"] == "cancelled"
        assert loop.jobs.get(child["id"])["status"] == "cancelled"
        assert loop.jobs.get(child["id"])["cancellation_acknowledged"]
        assert not loop.store.events("model.started")
    finally:
        await loop.jobs.close()
        loop.store.close()


@pytest.mark.parametrize("settles", [True, False])
async def test_cancelled_reader_preserves_pending_purchase_then_reconciles_after_receipt(
    tmp_path, settles
):
    started, release = asyncio.Event(), asyncio.Event()

    class Provider:
        simulated = True

        async def generate(self, request):
            started.set()
            await release.wait()
            if not settles:
                raise ModelFailure("Provider outcome is uncertain")
            return reply("Completed accepted read"), []

    loop = Loop(tmp_path, Provider(), trusted_tools=sys.platform != "darwin")
    loop.models.grant(1)
    tools(
        loop,
        '''async def run(tool):
    """An accepted model read may outlive this worker."""
    return await tool.model("Exact source question")
''',
    )
    await loop.jobs.start()
    try:
        root = loop.jobs.submit(Actor("astra"), "nested", {})
        child = await child_started(loop, root)
        await asyncio.wait_for(started.wait(), 2)
        assert (await loop.jobs.cancel(root["id"]))["status"] == "uncertain"
        assert loop.jobs.get(child["id"])["status"] == "uncertain"
        assert loop.jobs.get(child["id"])["cancellation_acknowledged"]
        assert not loop.store.events("model.completed")
        release.set()
        for _ in range(150):
            if not loop.jobs.pending_effects:
                break
            await asyncio.sleep(0.01)
        expected = "cancelled" if settles else "uncertain"
        assert loop.jobs.get(child["id"])["status"] == expected
        assert loop.jobs.get(root["id"])["status"] == expected
        assert len(loop.store.events("model.started")) == 1
        assert len(loop.store.events("model.completed")) == int(settles)
        assert len(loop.store.events("model.failed")) == int(not settles)
    finally:
        release.set()
        await loop.jobs.close()
        loop.store.close()


async def test_hard_killed_worker_without_ack_does_not_clean_up_uncertainty_cosmetically(tmp_path):
    loop = Loop(tmp_path, Scripted(), trusted_tools=sys.platform != "darwin")
    tools(
        loop,
        '''import asyncio, signal
async def run(tool):
    """Deliberately refuse graceful shutdown in this fixture."""
    asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    await asyncio.Event().wait()
''',
    )
    await loop.jobs.start()
    try:
        root = loop.jobs.submit(Actor("astra"), "nested", {})
        child = await child_started(loop, root)
        await loop.jobs.cancel(root["id"])
        assert loop.jobs.get(child["id"])["status"] == "uncertain"
        assert not loop.jobs.get(child["id"]).get("cancellation_acknowledged")
    finally:
        await loop.jobs.close()
        loop.store.close()


@pytest.mark.parametrize("settles", [True, False])
async def test_deadline_preserves_service_uncertainty_until_accepted_effect_settles(
    tmp_path, settles
):
    started, release = asyncio.Event(), asyncio.Event()

    async def service(actor, job, arguments):
        started.set()
        await release.wait()
        if not settles:
            raise RuntimeError("Transport failed after service acceptance")
        return {"status": "completed"}

    loop = Loop(
        tmp_path,
        Scripted(),
        services={"fixture": service},
        trusted_tools=sys.platform != "darwin",
    )
    tools(
        loop,
        '''async def run(tool):
    """Wait for a controlled external service."""
    return await tool.service("fixture")
''',
    )
    await loop.jobs.start()
    try:
        root = loop.jobs.submit(Actor("astra"), "nested", {})
        child = await child_started(loop, root)
        await asyncio.wait_for(started.wait(), 2)
        loop.jobs.timeout = 0.001
        await loop.jobs.pump()
        assert loop.jobs.get(root["id"])["status"] == "uncertain"
        assert loop.jobs.get(child["id"])["status"] == "uncertain"
        release.set()
        for _ in range(150):
            if not loop.jobs.pending_effects:
                break
            await asyncio.sleep(0.01)
        expected = "cancelled" if settles else "uncertain"
        assert loop.jobs.get(root["id"])["status"] == expected
        assert loop.jobs.get(child["id"])["status"] == expected
        assert len(loop.store.events("service.started")) == 1
        assert len(loop.store.events("service.completed")) == int(settles)
    finally:
        release.set()
        await loop.jobs.close()
        loop.store.close()
