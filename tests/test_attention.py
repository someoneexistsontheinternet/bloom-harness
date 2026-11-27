"""Owner progress and narrow Terra context preparation share durable messages."""

from bloom41.agents import Actor
from bloom41.loop import Loop
from bloom41.store import encoded
from bloom41.tokens import count
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


def test_context_attachments_stay_retrievable_without_duplicating_them_in_terra_input(tmp_path):
    loop = Loop(tmp_path, Scripted())
    original = "Exact selected evidence. " * 4000
    source = loop.store.put(original, "text/plain")
    loop.messages.deliver(
        "user",
        "astra",
        "Read the relevant evidence",
        message_id="question",
        attachments=[{"name": "evidence.txt", "source": source}],
    )
    loop.request_contexts()
    terra = Actor("terra-astra")
    (context,) = loop.ready(terra)
    assert context["attachments"][0]["expose"] == "reference"
    request, _ = loop.context.request(terra, [context])
    assert original not in encoded(request).decode()
    attachment = context["attachments"][0]
    assert (
        loop.agents.path(terra) / "inbox" / context["id"] / "attachments" / attachment["name"]
    ).read_text() == original
    owner_request, _ = loop.context.request(Actor("astra"), loop.messages.pending("astra"))
    assert original in encoded(owner_request).decode()
    loop.store.close()


def test_activity_batches_within_one_work_scope_and_required_context_goes_first(tmp_path):
    loop = Loop(tmp_path, Scripted())
    terra = Actor("terra-astra")
    for ident, target in [("first", "astra"), ("second", "astra"), ("branch", "a-branch")]:
        loop.messages.deliver(
            "system",
            terra.scope,
            "Recorded activity",
            message_id=ident,
            kind="activity",
            metadata={
                "context_request": {
                    "context_for": target,
                    "request_id": ident,
                    "activity_only": True,
                }
            },
        )
    assert [p["id"] for p in loop.ready(terra)] == ["first", "second"]
    loop.messages.deliver(
        "system",
        terra.scope,
        "A new instruction needs context",
        message_id="required",
        kind="context_request",
        metadata={"context_for": "astra", "request_id": "required"},
    )
    assert [p["id"] for p in loop.ready(terra)] == ["required"]
    loop.store.close()


async def test_terra_working_notes_are_bounded_and_current_native_tools_survive_reopen(tmp_path):
    provider = Scripted()
    loop = Loop(tmp_path, provider)
    terra = Actor("terra-astra")
    bulk = "archived large attachment " * 22_000
    for n in range(12):
        turn = {
            "id": f"old-{n}",
            "origin": "question",
            "context_request": {"request_id": f"episode-{n}"},
            "final": loop.store.put(
                f"---\ntarget: astra\n---\nFinding {n}. " + "brief " * 700, "text/plain"
            ),
        }
        history = loop.context.append(
            terra,
            [{"role": "user", "content": bulk}],
            key="turn-history:" + turn["id"],
            episode=f"episode-{n}",
        )
        loop.context.remember_note(terra, turn, history)
    assert len(loop.store.events("context.note")) == 12
    assert count(encoded(loop.context.working_history(terra)).decode()) < 5000
    loop.models.grant(1)
    assert not await loop.context.compact(terra, loop.models)
    assert not provider.requests
    assert bulk in encoded(loop.context.history(terra)).decode()

    native = [
        {"type": "reasoning", "encrypted_content": "original opaque state", "summary": []},
        {
            "type": "function_call",
            "call_id": "active-call",
            "name": "read_file",
            "arguments": '{"path":"relevant.txt"}',
        },
        {"type": "function_call_output", "call_id": "active-call", "output": "accepted"},
    ]
    loop.context.append(terra, native, key="turn-history:active", episode="current")
    loop.messages.deliver(
        "tool:read_file",
        terra.scope,
        "Exact tool result",
        message_id="continuation",
        kind="tool_result",
        metadata={"context_request": {"context_for": "astra", "request_id": "current"}},
    )
    loop.store.close()
    reopened = Loop(tmp_path, provider)
    request, _ = reopened.context.request(terra, reopened.ready(terra))
    assert request["input"][-4:-1] == native
    assert bulk not in encoded(request).decode()
    assert "Finding 0." not in encoded(request).decode()
    assert "Finding 11." in encoded(request).decode()
    assert bulk in encoded(reopened.context.history(terra)).decode()
    assert not reopened.store.events("model.started")
    reopened.store.close()


def test_terra_request_prefix_is_frozen_while_other_context_notes_arrive(tmp_path):
    loop = Loop(tmp_path, Scripted())
    terra = Actor("terra-astra")
    loop.messages.deliver(
        "system",
        terra.scope,
        "Gather only context useful to astra",
        message_id="request",
        kind="context_request",
        metadata={"context_for": "astra", "request_id": "episode"},
    )
    packages = loop.ready(terra)
    first, _ = loop.context.request(terra, packages)
    original = [loop.context.content(p) for p in packages]
    history = loop.context.append(terra, original, key="turn-history:original")
    # Recovery after an old runner wrote history, before it consumed the input.
    assert (
        loop.context.append(terra, original, key="turn-history:original", episode="episode")[
            "digest"
        ]
        == history["digest"]
    )
    loop.save_turn(
        {"id": "original", "context_request": {"request_id": "episode"}, "origin": "request"}
    )
    loop.context.remember_note(
        terra,
        {
            "id": "unrelated",
            "origin": "other",
            "final": loop.store.put("A later unrelated note", "text/plain"),
        },
        history,
    )
    loop.messages.consume(packages, "original")
    loop.messages.deliver(
        "tool:read_file",
        terra.scope,
        "New evidence",
        message_id="result",
        kind="tool_result",
        metadata={"context_request": {"context_for": "astra", "request_id": "episode"}},
    )
    second, _ = loop.context.request(terra, loop.ready(terra))
    assert second["input"][: len(first["input"])] == first["input"]
    assert "A later unrelated note" not in encoded(second).decode()
    assert len(loop.store.events("context.working")) == 1
    loop.store.close()
