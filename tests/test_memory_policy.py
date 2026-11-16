import asyncio

import pytest

from bloom4.agents import Agents, PROFILES
from bloom4.budget import Budget
from bloom4.context import Context
from bloom4.mailbox import Mailbox
from bloom4.provider import Generation, ProviderFailure
from bloom4.runtime import Runtime
from bloom4.store import Store


def answer(text):
    return Generation({"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
                       "usage": {"input_tokens": 100, "output_tokens": 10}})


def base(tmp_path):
    store = Store(tmp_path); Agents(store).bootstrap(); Budget(store).grant(5)
    return store


@pytest.mark.asyncio
async def test_idle_needs_both_time_and_uncompressed_volume(tmp_path):
    store = base(tmp_path); context = Context(store); context.snapshot("astra")
    box = Mailbox(store); box.post("Previous work"); event = box.ingest()[0]; box.consume("astra", [event["id"]])
    class Provider:
        output_tokens = 1000
        calls = 0
        async def generate(self, request, **kwargs):
            self.calls += 1; return answer("Chronological summary; original events remain available.")
    provider = Provider(); runtime = Runtime(store, provider, idle_seconds=0)
    runtime.idle_maintenance()
    assert not runtime.tasks()
    context.append("astra", [{"role": "user", "content": "old event " * 31000}])
    Runtime(store, provider, idle_seconds=1200).idle_maintenance()
    assert not runtime.tasks()
    await runtime.run(seconds=2)
    assert provider.calls == 1
    assert context.tail_tokens("astra") < 1000
    assert not store.events("memory.accepted")


@pytest.mark.asyncio
async def test_idle_compaction_yields_to_owner_input(tmp_path):
    store = base(tmp_path); context = Context(store); context.snapshot("astra")
    box = Mailbox(store); box.post("Previous work"); event = box.ingest()[0]; box.consume("astra", [event["id"]])
    context.append("astra", [{"role": "user", "content": "old event " * 31000}])
    class Provider:
        output_tokens = 1000
        calls = 0
        async def generate(self, request, **kwargs):
            self.calls += 1
            if self.calls == 1:
                box.post("Urgent new request", urgent=True)
                await asyncio.sleep(10)
                raise AssertionError("Maintenance should have been preempted")
            return answer("Urgent request handled")
    provider = Provider(); runtime = Runtime(store, provider, idle_seconds=0, auto_recall=False)
    await runtime.run(seconds=3)
    assert provider.calls == 2
    assert len(store.events("context.snapshot", target="astra")) == 1
    assert store.events("answer")[0]["data"]["text"] == "Urgent request handled"
    assert store.events("compaction.deferred")


def test_controller_and_owner_context_policies_are_distinct(tmp_path):
    store = base(tmp_path); context = Context(store)
    assert PROFILES["terra"]["target"] == 10_000
    assert PROFILES["terra"]["compact_at"] == 20_000
    assert PROFILES["astra"]["compact_at"] == 400_000
    assert PROFILES["luna"]["target"] >= 256_000
    assert context.snapshot("terra")["data"]["contract"]["window"] == 1_000_000


@pytest.mark.asyncio
async def test_curation_failure_unblocks_owner_with_original_reference(tmp_path):
    import json
    store = base(tmp_path)
    (store.root / "work/astra/big.txt").write_text("Exact evidence\n" * 5000)
    class Provider:
        output_tokens = 1000
        astra_calls = 0
        async def generate(self, request, **kwargs):
            if request["model"] == "gpt-6-luna": raise ProviderFailure("synthetic curator failure")
            self.astra_calls += 1
            if self.astra_calls == 1:
                return Generation({"output": [{"type": "function_call", "call_id": "big", "name": "read_file",
                    "arguments": json.dumps({"path": "big.txt"})}], "usage": {"input_tokens": 100, "output_tokens": 10}})
            assert "Background evidence failed" in json.dumps(request)
            return answer("Curation unavailable; original retained for exact retrieval")
    Mailbox(store).post("Read the evidence")
    provider = Provider(); await Runtime(store, provider, auto_recall=False).run(seconds=3)
    assert provider.astra_calls == 2
    assert store.events("curation.result")[0]["data"]["status"] == "unable"
    assert len(store.events("answer")) == 1
