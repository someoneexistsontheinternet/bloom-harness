import asyncio
import json

import httpx
import pytest

from bloom41.agents import Actor, Agents
from bloom41.context import Context
from bloom41.models import BudgetExceeded, ModelFailure, Models, Responses, final_text
from bloom41.store import Store


def reply(text):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }


class Scripted:
    simulated = True

    def __init__(self, result=None):
        self.requests = []
        self.result = result or reply("A checked response")

    async def generate(self, request):
        self.requests.append(request)
        await asyncio.sleep(0)
        return self.result, []


async def test_model_receipt_reuse_concurrency_budget_and_native_phase(tmp_path):
    store = Store(tmp_path)
    provider = Scripted(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "progress"}],
                },
                *reply("final only")["output"],
            ],
        }
    )
    models = Models(store, provider)
    request = {"model": "fixture", "input": []}
    with pytest.raises(BudgetExceeded):
        await models.call(Actor("astra"), request, call_id="c1")
    models.grant(1)
    a, b = await asyncio.gather(
        models.call(Actor("astra"), request, call_id="c1"),
        models.call(Actor("astra"), request, call_id="c1"),
    )
    assert a == b and final_text(a) == "final only" and len(provider.requests) == 1
    assert models.allowance() == 0
    with pytest.raises(ValueError):
        await models.call(Actor("astra"), {**request, "input": ["different"]}, call_id="c1")
    store.close()


async def test_uncertain_stream_is_never_rebought(tmp_path):
    class Interrupted(Scripted):
        async def generate(self, request):
            self.requests.append(request)
            raise ModelFailure("connection interrupted", trace=[{"type": "response.created"}])

    store = Store(tmp_path)
    provider = Interrupted()
    models = Models(store, provider)
    models.grant(3)
    for _ in range(2):
        with pytest.raises(ModelFailure):
            await models.call(Actor("astra"), {"input": []}, call_id="c1")
    assert len(provider.requests) == 1 and len(store.events("model.failed")) == 1
    store.close()


async def test_cancelled_inference_has_a_terminal_receipt(tmp_path):
    began = asyncio.Event()

    class Pending(Scripted):
        async def generate(self, request):
            began.set()
            await asyncio.Event().wait()

    store = Store(tmp_path)
    models = Models(store, Pending())
    models.grant(1)
    pending = asyncio.create_task(models.call(Actor("astra"), {"input": []}, call_id="interrupted"))
    await began.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    failed = store.by_key("model-failed:interrupted")
    assert failed and failed["data"]["uncertain"] is True
    assert not store.events("model.completed")
    store.close()


async def test_recovery_settles_an_interrupted_purchase_without_replaying(tmp_path):
    store = Store(tmp_path)
    provider = Scripted()
    models = Models(store, provider)
    models.grant(2)
    store.event(
        "model.started",
        "astra",
        {"id": "lost", "request": store.put({"input": []})},
        key="model-started:lost",
    )
    models.recover()
    assert store.by_key("model-failed:lost")["data"]["uncertain"]
    with pytest.raises(ModelFailure):
        await models.call(Actor("astra"), {"input": []}, call_id="lost")
    assert not provider.requests
    store.close()


async def test_real_http_adapter_uses_fake_transport_without_credentials(tmp_path):
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        event = {
            "type": "response.completed",
            "response": {
                **reply("fixture answer"),
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(event) + "\n\n",
        )

    provider = Responses("openai", transport=httpx.MockTransport(handle))
    provider.headers = lambda: {"Authorization": "Bearer fixture-credential"}
    result, trace = await provider.generate(
        provider.prepare({"model": "fixture", "input": [], "stream": True})
    )
    assert final_text(result) == "fixture answer" and trace
    assert requests[0]["max_output_tokens"] == 32768


async def test_active_stream_can_outlast_idle_timeout_but_has_an_overall_deadline():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(4):
                await asyncio.sleep(0.03)
                yield b'data: {"type":"response.in_progress"}\n\n'
            yield (
                "data: "
                + json.dumps({"type": "response.completed", "response": reply("finished stream")})
                + "\n\n"
            ).encode()

    async def handle(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

    provider = Responses("openai", timeout=0.05, deadline=1, transport=httpx.MockTransport(handle))
    provider.headers = lambda: {}
    response, trace = await provider.generate({"input": [], "stream": True})
    assert final_text(response) == "finished stream" and len(trace) == 5
    provider.deadline = 0.06
    with pytest.raises(ModelFailure, match="TimeoutError") as failed:
        await provider.generate({"input": [], "stream": True})
    assert failed.value.trace


def test_context_keeps_native_items_and_image_bytes_and_fork_seed(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    ctx = Context(store)
    actor = Actor("astra")
    ctx.append(
        actor,
        [{"type": "reasoning", "encrypted_content": "opaque fixture state", "summary": []}],
        key="native1",
    )
    image = b"\x89PNG\r\n\x1a\nfixture"
    ref = store.put(image, "image/png")
    package = {
        "id": "m1",
        "sender": "user",
        "recipient": "astra",
        "kind": "message",
        "reply_to": None,
        "body": store.put("Read this", "text/plain"),
        "attachments": [{"name": "chart.png", "media": "image/png", "source": ref}],
    }
    request, definitions = ctx.request(actor, [package])
    assert request["input"][1]["encrypted_content"] == "opaque fixture state"
    assert request["input"][-1]["content"][-1]["type"] == "input_image"
    seed = {"request": store.put(request)}
    fork = agents.fork(actor, "branch1", seed, "Check the chart")
    branch = ctx.history(fork)
    assert branch[:-1] == request["input"]
    assert ctx.snapshot(actor)["data"]["window"] == 1_000_000
    store.close()
