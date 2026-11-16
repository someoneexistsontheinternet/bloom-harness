import json
import asyncio

import httpx
import pytest

from bloom4.provider import ProviderFailure, Responses


@pytest.mark.asyncio
async def test_sse_preserves_native_reasoning_and_no_auth_in_body(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-credential")

    def handle(request):
        body = json.loads(request.content)
        assert "synthetic-test-credential" not in request.content.decode()
        assert body["max_output_tokens"] == 100
        output = [{"type": "reasoning", "id": "reason", "encrypted_content": "opaque-test-state"}]
        ev = {"type": "response.completed", "response": {"status": "completed", "output": output}}
        return httpx.Response(200, text="data: " + json.dumps(ev) + "\n\n")

    provider = Responses(
        kind="openai", output_tokens=100, http_transport=httpx.MockTransport(handle)
    )
    result = await provider.generate({"model": "gpt-6-astra", "input": [], "stream": True})
    assert result.response["output"][0]["encrypted_content"] == "opaque-test-state"


@pytest.mark.asyncio
async def test_partial_stream_never_becomes_a_completed_result(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-credential")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text='data: {"type":"response.created"}\n\n')
    )
    with pytest.raises(ProviderFailure, match="without response.completed"):
        await Responses(kind="openai", http_transport=transport).generate(
            {"model": "gpt-6-astra", "stream": True}
        )


@pytest.mark.asyncio
async def test_websocket_steering_keeps_native_order_and_exact_wire(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-credential")
    reasoning = {"type": "reasoning", "id": "r1", "encrypted_content": "opaque-state"}
    message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "UPDATED"}],
    }

    class Socket:
        def __init__(self):
            self.events = asyncio.Queue()
            self.sent = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def send(self, raw):
            data = json.loads(raw)
            self.sent.append(data)
            if data["type"] == "response.create":
                assert "stream" not in data and data["max_output_tokens"] == 100
                self.events.put_nowait({"type": "response.created", "response": {"id": "first"}})
                self.events.put_nowait({"type": "response.output_item.done", "item": reasoning})
            else:
                assert data == {
                    "type": "response.steer",
                    "previous_response_id": "first",
                    "input": "New direction",
                }
                for event in [
                    {"type": "response.steer.accepted", "steer": {"id": "s1"}},
                    {
                        "type": "response.incomplete",
                        "response": {
                            "id": "first",
                            "output": [reasoning],
                            "incomplete_details": {"reason": "steered"},
                        },
                    },
                    {"type": "response.created", "response": {"id": "second"}},
                    {"type": "response.output_item.done", "item": message},
                    {
                        "type": "response.completed",
                        "response": {"id": "second", "output": [message]},
                    },
                ]:
                    self.events.put_nowait(event)

        async def recv(self):
            return json.dumps(await self.events.get())

    socket = Socket()
    monkeypatch.setattr("bloom4.provider.connect", lambda *a, **k: socket)
    queue = asyncio.Queue()
    entry = {"id": "input1", "data": {"text": "New direction"}}
    queue.put_nowait(entry)
    reserved = []

    def reserve(event, response_id):
        reserved.append((event, response_id))
        return {"reserved": True}

    provider = Responses(kind="openai", transport="websocket", output_tokens=100)
    result = await provider.generate(
        {"model": "gpt-6-astra", "input": [], "stream": True}, steering=queue, reserve_steer=reserve
    )
    assert result.native_items == [reasoning, {"role": "user", "content": "New direction"}, message]
    assert result.applied_inputs == [entry] and len(result.continuations) == 1
    assert reserved == [(entry, "first")]


@pytest.mark.asyncio
async def test_runtime_records_exact_http_body(monkeypatch, tmp_path):
    from bloom4.store import Store
    from bloom4.agents import Agents
    from bloom4.budget import Budget
    from bloom4.runtime import Runtime

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-credential")
    sent = []

    def handle(request):
        sent.append(request.content)
        response = {"output": [], "usage": {"input_tokens": 1, "output_tokens": 1}}
        return httpx.Response(
            200,
            text="data: "
            + json.dumps({"type": "response.completed", "response": response})
            + "\n\n",
        )

    store = Store(tmp_path)
    Agents(store).bootstrap()
    Budget(store).grant(2)
    runtime = Runtime(
        store,
        Responses(kind="openai", output_tokens=100, http_transport=httpx.MockTransport(handle)),
    )
    await runtime.infer("astra", "test", {"model": "gpt-6-astra", "input": [], "stream": True})
    assert store.read(store.events("model.started")[0]["data"]["request"]) == sent[0]
