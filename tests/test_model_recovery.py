import asyncio
import json

import httpx
import pytest

from bloom.config import Config
from bloom.models import Models, ProviderError
from bloom.runtime import Runtime


def sse(event):
    return ("data: " + json.dumps(event) + "\n\n").encode()


def completed(text="Recovered answer"):
    return {
        "type": "response.completed",
        "response": {
            "id": "complete-response",
            "status": "completed",
            "model": "test",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": {"input_tokens": 12, "output_tokens": 4},
        },
    }


class CutStream(httpx.AsyncByteStream):
    def __init__(self, events=(), abrupt=True):
        self.events, self.abrupt = events, abrupt

    async def __aiter__(self):
        for event in self.events:
            yield sse(event)
        if self.abrupt:
            raise httpx.RemoteProtocolError("peer closed connection (incomplete chunked read)")


@pytest.fixture
def native(store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    store.config.data["profiles"]["astra"]["provider"] = "openai"
    store.config.data["providers"]["openai"]["retry_delay_seconds"] = 0
    return store


@pytest.mark.parametrize("abrupt", [True, False])
async def test_runtime_recovers_without_dispatching_partial_write(native, abrupt):
    task = native.new_task("Complete this task without duplicating partial work")
    partial = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "type": "function_call",
            "call_id": "never-dispatch",
            "name": "write_file",
            "arguments": json.dumps(
                {"path": "bad-partial", "text": "bad", "expected_sha256": "absent"}
            ),
        },
    }
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, stream=CutStream([partial], abrupt))
        return httpx.Response(200, content=sse(completed()))

    runtime = Runtime(native, models=Models(native, httpx.MockTransport(handler)))
    await runtime.step(task)
    assert native.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert not (native.config.root / "bad-partial").exists()
    assert not native.rows("SELECT * FROM tool_calls")
    assert not native.rows("SELECT * FROM operations")
    calls = native.rows("SELECT * FROM model_calls ORDER BY rowid")
    assert [c["status"] for c in calls] == ["failed", "completed"]
    assert calls[0]["input_tokens"] is None and calls[0]["output_tokens"] is None
    assert calls[1]["input_tokens"] == 12
    assert calls[0]["request_hash"] == calls[1]["request_hash"] and requests[0] == requests[1]
    memberships = [
        native.rows(
            "SELECT item_id,position FROM model_call_items WHERE call_id=? ORDER BY position",
            (c["id"],),
        )
        for c in calls
    ]
    assert memberships[0] == memberships[1] and memberships[0]
    stream = native.one(
        "SELECT stream_hash FROM model_call_streams WHERE call_id=?", (calls[0]["id"],)
    )
    assert native.get(stream["stream_hash"]) == [partial]
    event = native.one("SELECT detail_hash FROM events WHERE kind='model.retry_scheduled'")
    assert native.get(event["detail_hash"])["next_call"] == calls[1]["id"]
    assert len(native.rows("SELECT * FROM deliveries")) == 1
    # A retry's retained unknown usage must not re-block successful work on restart.
    runtime.recover()
    assert native.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"


@pytest.mark.parametrize("close_error", [False, True])
async def test_terminal_completion_does_not_wait_for_eof_or_retry(native, close_error):
    requests = []

    class CompletedStream(CutStream):
        async def aclose(self):
            if close_error:
                raise httpx.ReadError("connection cleanup failed after completion")

    def handler(request):
        requests.append(request)
        # Reading past response.completed would raise a transport error.
        return httpx.Response(200, stream=CompletedStream([completed()]))

    model = Models(native, httpx.MockTransport(handler))
    task = native.new_task("terminal event")
    call, result = await model.infer("astra", task, "test", [], [])
    assert result["status"] == "completed" and len(requests) == 1
    _, saved = await model.infer("astra", task, "test", [], [], call_id=call)
    assert saved == result and len(requests) == 1


async def test_retry_ceiling_keeps_every_attempt_and_unknown_usage(native):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=CutStream())

    model = Models(native, httpx.MockTransport(handler))
    task = native.new_task("persistent outage")
    with pytest.raises(httpx.RemoteProtocolError):
        await model.infer("astra", task, "test", [], [])
    calls = native.rows("SELECT * FROM model_calls ORDER BY rowid")
    assert len(requests) == 3 and [c["status"] for c in calls] == ["failed", "failed", "uncertain"]
    assert all(c["input_tokens"] is None and c["output_tokens"] is None for c in calls)
    assert len(native.rows("SELECT * FROM events WHERE kind='model.retry_scheduled'")) == 2
    # Re-entering the same uncertain physical call is still explicitly forbidden.
    with pytest.raises(ProviderError, match="explicit reconciliation"):
        await model.infer("astra", task, "test", [], [], call_id=calls[-1]["id"])
    assert len(requests) == 3
    Runtime(native).recover()
    assert native.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "blocked"


@pytest.mark.parametrize(
    "error", ["http503", "http429", "sse_nested", "sse_flat", "response_failed"]
)
async def test_transient_provider_errors_retry(native, error):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) > 1:
            return httpx.Response(200, content=sse(completed()))
        if error.startswith("http"):
            return httpx.Response(
                int(error[4:]), headers={"Retry-After": "0"}, text="temporarily unavailable"
            )
        detail = {"code": "server_error", "message": "temporary failure"}
        event = (
            {"type": "response.failed", "response": {"status": "failed", "error": detail}}
            if error == "response_failed"
            else {"type": "error", **({"error": detail} if error == "sse_nested" else detail)}
        )
        return httpx.Response(200, content=sse(event))

    await Models(native, httpx.MockTransport(handler)).infer(
        "astra", native.new_task("transient"), "test", [], []
    )
    assert len(requests) == 2


@pytest.mark.parametrize("failure", ["unauthorized", "invalid", "incomplete", "long_retry_after"])
async def test_permanent_or_output_limit_failures_are_not_retried(native, failure):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "incomplete":
            return httpx.Response(
                200,
                content=sse(
                    {
                        "type": "response.incomplete",
                        "response": {
                            "status": "incomplete",
                            "incomplete_details": {"reason": "max_output_tokens"},
                        },
                    }
                ),
            )
        if failure == "long_retry_after":
            return httpx.Response(429, headers={"Retry-After": "120"}, text="wait longer")
        return httpx.Response(401 if failure == "unauthorized" else 400, text="request rejected")

    with pytest.raises(ProviderError):
        await Models(native, httpx.MockTransport(handler)).infer(
            "astra", native.new_task("permanent"), "test", [], []
        )
    assert len(requests) == 1


@pytest.mark.parametrize("control", ["pause", "steer", "cancel"])
async def test_owner_control_during_backoff_prevents_another_request(native, monkeypatch, control):
    task = native.new_task("owner control")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=CutStream())

    entered = asyncio.Event()

    async def sleep(delay):
        entered.set()
        if control == "cancel":
            await asyncio.Event().wait()
        elif control == "pause":
            Runtime(native).pause(task)
        else:
            Runtime(native).steer(task, "Change direction")

    monkeypatch.setattr("bloom.models.asyncio.sleep", sleep)
    model = Models(native, httpx.MockTransport(handler))
    job = asyncio.create_task(model.infer("astra", task, "test", [], []))
    await asyncio.wait_for(entered.wait(), 1)
    if control == "cancel":
        job.cancel()
    with pytest.raises(asyncio.CancelledError if control == "cancel" else InterruptedError):
        await job
    assert len(requests) == 1
    assert native.one("SELECT COUNT(*) AS n FROM model_calls")["n"] == 1


async def test_hosted_mutation_tools_do_not_get_automatic_replay(native):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=CutStream())

    with pytest.raises(httpx.RemoteProtocolError):
        await Models(native, httpx.MockTransport(handler)).infer(
            "astra",
            native.new_task("hosted effect"),
            "test",
            [],
            [{"type": "mcp", "server_label": "external"}],
        )
    assert len(requests) == 1


@pytest.mark.parametrize(
    "options", [{"max_retries": -1}, {"max_retries": 6}, {"retry_delay_seconds": float("nan")}]
)
def test_retry_configuration_is_bounded(native, options):
    with pytest.raises(ValueError, match="Provider"):
        Config(native.config.root, {"providers": {"openai": options}})
