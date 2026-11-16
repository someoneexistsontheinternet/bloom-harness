import json

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
    provider = Responses(kind="openai", output_tokens=100, http_transport=httpx.MockTransport(handle))
    result = await provider.generate({"model": "gpt-6-astra", "input": [], "stream": True})
    assert result.response["output"][0]["encrypted_content"] == "opaque-test-state"


@pytest.mark.asyncio
async def test_partial_stream_never_becomes_a_completed_result(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-credential")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text='data: {"type":"response.created"}\n\n'))
    with pytest.raises(ProviderFailure, match="without response.completed"):
        await Responses(kind="openai", http_transport=transport).generate({"model": "gpt-6-astra", "stream": True})
