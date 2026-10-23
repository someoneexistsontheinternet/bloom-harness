import json

import httpx
import pytest

from bloom.models import Models, ProviderError


async def test_sse_done_items_survive_empty_final_output(store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    item = {
        "type": "function_call",
        "id": "fc1",
        "call_id": "native1",
        "name": "read_file",
        "arguments": '{"path":"source.txt"}',
    }
    events = [
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {
            "type": "response.completed",
            "response": {
                "id": "r1",
                "status": "completed",
                "model": "gpt-6-luna",
                "output": [],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        },
    ]
    payload = "".join("data: " + json.dumps(x) + "\n\n" for x in events)
    model = Models(store, httpx.MockTransport(lambda request: httpx.Response(200, text=payload)))
    task = store.new_task("stream check")
    call, result = await model.infer(
        "astra", task, "test", [], [], profile_override={"provider": "openai"}
    )
    assert result["output"] == [item]
    record = store.one("SELECT stream_hash FROM model_call_streams WHERE call_id=?", (call,))
    assert store.get(record["stream_hash"]) == events
    # Reuse returns the settled generation, no second HTTP request.
    _, repeated = await model.infer(
        "astra", task, "test", [], [], call_id=call, profile_override={"provider": "openai"}
    )
    assert repeated == result


async def test_incomplete_stream_does_not_dispatch_partial_call(store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    events = [
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "x",
                "name": "write_file",
                "arguments": "{}",
            },
        },
        {"type": "response.incomplete", "response": {"status": "incomplete"}},
    ]
    payload = "".join("data: " + json.dumps(x) + "\n\n" for x in events)
    model = Models(store, httpx.MockTransport(lambda request: httpx.Response(200, text=payload)))
    with pytest.raises(ProviderError, match="did not complete"):
        await model.infer(
            "astra",
            store.new_task("incomplete"),
            "test",
            [],
            [],
            profile_override={"provider": "openai"},
        )
    assert not store.rows("SELECT * FROM tool_calls")
    assert store.one("SELECT status FROM model_calls")["status"] == "failed"
    assert store.one("SELECT stream_hash FROM model_call_streams")
