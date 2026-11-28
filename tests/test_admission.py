import asyncio

import pytest

from bloom41.agents import Actor
from bloom41.loop import Loop
from bloom41.models import BudgetExceeded, Models
from bloom41.store import Store
from test_models_context import Scripted, reply


async def test_distinct_models_overlap_and_cancellation_does_not_hold_other_calls(tmp_path):
    entered, later_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Provider(Scripted):
        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 9:
                entered.set()
            if request["label"] == "later-owner":
                later_entered.set()
            await release.wait()
            return reply(request["label"]), []

    store = Store(tmp_path)
    provider = Provider()
    models = Models(store, provider)
    models.grant(10)

    def submit(name, profile, purpose="agent"):
        return asyncio.create_task(
            models.call(
                Actor("astra" if profile == "luna" else profile),
                {"input": [], "label": name},
                call_id=name,
                profile=profile,
                purpose=purpose,
            )
        )

    tasks = [submit(f"reader-{n}", "luna", "tool_reader") for n in range(6)]
    tasks.extend(submit(profile, profile) for profile in ("astra", "sol", "terra"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert len(store.events("model.started")) == 9
        assert not store.events("model.completed")
        assert all(not task.done() for task in tasks)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert store.by_key("model-failed:reader-0")["data"]["uncertain"]
        tasks.append(submit("later-owner", "astra"))
        await asyncio.wait_for(later_entered.wait(), timeout=5)
        assert len(provider.requests) == 10 and not store.events("model.completed")
        release.set()
        await asyncio.gather(*tasks[1:])
        assert len(store.events("model.completed")) == 9
        assert len(store.events("model.failed")) == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        store.close()


async def test_overlapping_requests_do_not_overspend_the_call_allowance(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Provider(Scripted):
        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 3:
                entered.set()
            await release.wait()
            return reply("finished"), []

    store = Store(tmp_path)
    provider = Provider()
    models = Models(store, provider)
    models.grant(3)
    tasks = [
        asyncio.create_task(models.call(Actor("astra"), {"input": []}, call_id=f"call-{n}"))
        for n in range(8)
    ]
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert len(store.events("model.started")) == 3 and models.allowance() == 0
        assert not store.events("model.completed")
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(isinstance(result, BudgetExceeded) for result in results) == 5
        assert len(provider.requests) == len(store.events("model.completed")) == 3
        assert not store.events("model.failed")
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        store.close()


async def test_ready_owner_runs_ahead_of_ready_older_optional_monitors(tmp_path):
    loop = Loop(tmp_path, Scripted(), heartbeat=0.01, activity_delay=0)
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
