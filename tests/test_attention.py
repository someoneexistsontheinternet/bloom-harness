"""Owner progress and narrow Terra context preparation share durable messages."""

from bloom41.agents import Actor
from bloom41.loop import Loop
from test_models_context import Scripted


def result(loop, ident="result"):
    loop.messages.deliver(
        "tool:read_file", "astra", "The requested result", message_id=ident, kind="tool_result"
    )


def context_note(loop, request):
    loop.messages.deliver(
        "terra-astra",
        "astra",
        "Use the original instructions and prepared evidence.",
        message_id="note",
        kind="context",
        metadata={**request["data"], "context_for": "astra", "disposition": "deliver"},
    )


def test_ready_tool_result_needs_no_context_purchase_but_new_instructions_are_not_skipped(tmp_path):
    loop = Loop(tmp_path, Scripted())
    result(loop)
    loop.request_contexts()
    assert not loop.store.events("context.requested")
    assert [p["id"] for p in loop.ready(Actor("astra"))] == ["result"]

    loop.messages.deliver("user", "astra", "Use the amended numbers", message_id="correction")
    assert loop.ready(Actor("astra")) == []
    loop.request_contexts()
    (request,) = loop.store.events("context.requested")
    assert request["data"]["original_ids"] == ["correction"]
    context_note(loop, request)
    ready = loop.ready(Actor("astra"))
    assert {p["id"] for p in ready} == {"result", "correction", "note"}
    assert loop.store.read(next(p for p in ready if p["id"] == "correction")["body"])
    loop.store.close()


def test_result_with_previously_accepted_context_preparation_settles_on_reopen(tmp_path):
    loop = Loop(tmp_path, Scripted())
    result(loop)
    request = loop.store.event(
        "context.requested",
        "astra",
        {"request_id": "previous-context", "original_ids": ["result"]},
        key="context-request:previous-context",
    )
    loop.store.close()
    reopened = Loop(tmp_path, Scripted())
    reopened.request_contexts()
    assert len(reopened.store.events("context.requested")) == 1
    assert reopened.ready(Actor("astra")) == []
    context_note(reopened, request)
    assert {p["id"] for p in reopened.ready(Actor("astra"))} == {"result", "note"}
    assert not reopened.store.events("model.started")
    reopened.store.close()
