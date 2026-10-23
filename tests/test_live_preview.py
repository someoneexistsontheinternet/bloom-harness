import asyncio
import json

import httpx
import pytest

from bloom.delivery import Delivery, Discord
from bloom.live_preview import PublicStream
from bloom.models import Models
from bloom.runtime import Runtime
from test_model_recovery import completed, sse


async def test_public_preview_arrives_before_completion_and_final_is_sent_once(store, monkeypatch):
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "fixture")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    store.config.data["discord"]["enabled"] = True
    store.config.profile("astra")["provider"] = "openai"
    task = store.new_task("stream my response", channel="discord", address="123")
    ready = asyncio.Event()
    finish = asyncio.Event()
    sent = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse(
                {"type": "response.reasoning_summary_text.delta", "delta": "PRIVATE REASONING"}
            )
            yield sse(
                {
                    "type": "response.output_text.delta",
                    "output_index": 1,
                    "delta": "First public paragraph.\n\nSecond paragraph.",
                }
            )
            ready.set()
            await finish.wait()
            yield sse(completed("Final public answer."))

    def wire(request):
        body = json.loads(request.content)
        sent.append((request.method, str(request.url), body))
        return httpx.Response(200, json={"id": "message-" + str(len(sent))})

    models = Models(
        store, httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
    )
    runtime = Runtime(store, models)
    runtime.delivery = Delivery(store, Discord(store, httpx.MockTransport(wire)))
    job = asyncio.create_task(runtime.step(task))
    await asyncio.wait_for(ready.wait(), 2)
    await runtime.delivery.flush()
    assert any(
        method == "PATCH" and "First public paragraph." in body.get("content", "")
        for method, _, body in sent
    )
    assert not store.one("SELECT final_item FROM tasks")["final_item"]
    assert "PRIVATE REASONING" not in json.dumps(sent)
    finish.set()
    await job
    store.db.execute("UPDATE live_previews SET retry_at=0")
    await runtime.delivery.flush()
    await runtime.delivery.flush()
    previews = [
        body
        for method, _, body in sent
        if method == "POST" and "provisional draft" in body.get("content", "")
    ]
    finals = [
        body
        for method, _, body in sent
        if method == "POST" and body.get("content") == "Final public answer."
    ]
    assert len(previews) == len(finals) == 1
    assert any(
        method == "PATCH" and "response received" in body.get("content", "")
        for method, _, body in sent
    )
    posts = [body for method, _, body in sent if method == "POST"]
    assert next(i for i, p in enumerate(posts) if p.get("embeds")) < posts.index(finals[0])
    resumed = Runtime(store)
    resumed.recover()
    resumed.delivery = runtime.delivery
    await resumed.delivery.flush()
    assert (
        len([p for m, _, p in sent if m == "POST" and p.get("content") == "Final public answer."])
        == 1
    )


async def test_uncertain_preview_post_never_reposts_and_cannot_block_final(store, monkeypatch):
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "fixture")
    store.config.data["discord"]["enabled"] = True
    from test_runtime_delivery import saved, message

    task = store.new_task("Interrupted draft", channel="discord", address="123")
    call = saved(store, task, [message()])
    store.db.execute("UPDATE model_calls SET status='running' WHERE id=?", (call,))
    public = PublicStream(store, "astra", task, call, "turn")
    public.event({"type": "response.output_text.delta", "delta": "Draft"})
    posts = []

    def wire(request):
        if request.method == "POST":
            posts.append(json.loads(request.content))
            raise httpx.ReadTimeout("ambiguous preview send")
        return httpx.Response(200, json={"id": "fixture"})

    delivery = Delivery(store, Discord(store, httpx.MockTransport(wire)))
    await delivery.flush()
    await delivery.flush()
    store.db.execute("UPDATE model_calls SET status='uncertain' WHERE id=?", (call,))
    public.write("uncertain")
    await delivery.flush()
    assert len(posts) == 1 and store.one("SELECT status FROM deliveries")["status"] == "uncertain"
    assert not store.rows("SELECT * FROM tool_calls")


@pytest.mark.parametrize("purpose", ["compaction", "memory_upkeep", "web_review", "curation"])
def test_internal_helper_text_is_never_a_public_live_preview(store, purpose):
    task = store.new_task("Private maintenance is not a reply")
    public = PublicStream(store, "astra", task, "nonexistent", purpose)
    public.event({"type": "response.output_text.delta", "delta": "Internal proposal"})
    public.write("completed")
    assert not store.rows("SELECT * FROM model_text_streams")
