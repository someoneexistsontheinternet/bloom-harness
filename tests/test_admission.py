import asyncio

import pytest

from bloom41.admission import Admission
from bloom41.agents import Actor
from bloom41.loop import Loop
from bloom41.models import Models
from bloom41.store import Store
from test_models_context import Scripted, reply


async def test_model_capacity_prioritizes_owners_and_steering_but_services_old_readers(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    class Provider(Scripted):
        async def generate(self, request):
            name = request["label"]
            order.append(name)
            if name == "running-reader":
                entered.set()
                await release.wait()
            await asyncio.sleep(0)
            return reply(name), []

    store = Store(tmp_path)
    models = Models(store, Provider(), concurrency=1)
    models.grant(20)

    def submit(name, **kwargs):
        return asyncio.create_task(
            models.call(Actor("astra"), {"input": [], "label": name}, call_id=name, **kwargs)
        )

    first = submit("running-reader", purpose="tool_reader")
    await entered.wait()
    tasks = [submit("old-reader", purpose="tool_reader"), submit("monitor", profile="terra")]
    tasks.extend(submit(f"owner-{n}", profile="sol") for n in range(5))
    tasks.append(submit("steering", priority=0))
    await asyncio.sleep(0)
    assert len(models.slots.waiting) == 8 and models.slots.active == 1
    assert len(store.events("model.started")) == 1
    release.set()
    await asyncio.gather(first, *tasks)
    assert order[:4] == ["running-reader", "steering", "owner-0", "old-reader"]
    assert order.index("monitor") < len(order) - 1
    assert len(order) == len(store.events("model.completed")) == 9
    assert models.slots.active == 0 and not models.slots.waiting
    store.close()


@pytest.mark.parametrize("after_grant", [False, True])
async def test_cancelled_admission_cannot_leak_or_double_release_a_slot(after_grant):
    slots = Admission(1)
    holder = slots.slot()
    await holder.__aenter__()
    purchased = []

    async def work():
        async with slots.slot():
            purchased.append(True)

    pending = asyncio.create_task(work())
    await asyncio.sleep(0)
    if after_grant:
        await holder.__aexit__(None, None, None)
        pending.cancel()
    else:
        pending.cancel()
        await holder.__aexit__(None, None, None)
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not purchased and slots.active == 0 and not slots.waiting
    async with slots.slot():
        assert slots.active == 1
    assert slots.active == 0


async def test_ready_owner_enters_actor_slots_ahead_of_older_optional_monitors(tmp_path):
    loop = Loop(tmp_path, Scripted(), heartbeat=0.01)
    for n in range(5):
        name = f"worker-{n}"
        loop.agents.create(name, "sol", "Independent responsibility", principal=Actor("astra"))
        loop.messages.deliver(
            "system",
            "terra-" + name,
            "Routine activity",
            message_id=f"activity-{n}",
            kind="activity",
            metadata={
                "context_request": {
                    "context_for": name,
                    "request_id": f"activity-{n}",
                    "activity_only": True,
                }
            },
        )
    loop.messages.deliver(
        "tool:read_file", "astra", "Ready evidence", message_id="ready", kind="tool_result"
    )
    order = []

    async def advance(actor, packages=None, pending_turn=None):
        order.append(actor.scope)
        await asyncio.sleep(0)
        loop.messages.consume(packages, "handled-" + actor.scope)

    loop.advance = advance
    await loop.run(seconds=1)
    assert order[0] == "astra" and len(order) == 6
    assert not loop.store.events("model.started")
    loop.store.close()
