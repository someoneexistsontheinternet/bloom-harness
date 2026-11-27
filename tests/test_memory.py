import json
import sys

import pytest

from bloom41.agents import Actor
from bloom41.context import Context
from bloom41.loop import Loop
from bloom41.models import Models
from bloom41.store import digest, encoded
from test_delivery import done
from test_models_context import Scripted, reply


async def approve(loop, actor, ident, edits):
    request, tools = loop.context.request(actor, [])
    loop.models.provider.result = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "name": "edit_file",
                "call_id": str(n),
                "arguments": json.dumps(a),
            }
            for n, a in enumerate(edits)
        ],
    }
    response = await loop.models.call(actor, request, call_id=ident)
    loop.save_turn(
        {
            "id": ident,
            "scope": actor.scope,
            "request": loop.store.put(request),
            "tools": tools,
            "origin": "original-question",
            "input_ids": [],
        }
    )


async def test_exact_owning_memory_edits_and_shared_authority_in_real_workers(tmp_path):
    loop = Loop(tmp_path, Scripted(), trusted_tools=sys.platform != "darwin")
    loop.models.grant(8)
    loop.agents.create("worker", "sol", "Work on fixtures", principal=Actor("astra"))
    worker, astra = Actor("worker"), Actor("astra")
    arguments = {
        "path": "memory/L1.md",
        "text": "Remember the exact owner instruction",
        "expected_sha256": digest(b""),
    }
    shared_arguments = {**arguments, "path": "shared/memory/L1.md"}
    await approve(loop, worker, "worker-approval", [arguments, shared_arguments])
    await approve(loop, astra, "astra-approval", [shared_arguments])
    native_id = lambda turn, number: "job_" + digest(encoded([turn, str(number)]))[:24]
    await loop.jobs.start()
    try:
        arguments = {
            "path": "memory/L1.md",
            "text": "Remember the exact owner instruction",
            "expected_sha256": digest(b""),
        }
        job = loop.jobs.submit(
            worker,
            "edit_file",
            arguments,
            turn="worker-approval",
            job_id=native_id("worker-approval", 0),
        )
        result = await done(loop.jobs, job["id"])
        assert result["status"] == "completed", result
        approved = loop.store.events("memory.accepted", "worker")
        assert len(approved) == 1 and approved[0]["data"]["approval_turn"] == "worker-approval"
        forbidden = loop.jobs.submit(
            worker,
            "edit_file",
            {**arguments, "path": "shared/memory/L1.md"},
            turn="worker-approval",
            job_id=native_id("worker-approval", 1),
        )
        assert (await done(loop.jobs, forbidden["id"]))["status"] == "failed"
        shared = loop.jobs.submit(
            astra,
            "edit_file",
            {**arguments, "path": "shared/memory/L1.md"},
            turn="astra-approval",
            job_id=native_id("astra-approval", 0),
        )
        assert (await done(loop.jobs, shared["id"]))["status"] == "completed"
        read = loop.jobs.submit(worker, "read_file", {"path": "shared/memory/L1.md"})
        state = await done(loop.jobs, read["id"])
        assert loop.store.json(state["result"])["text"] == arguments["text"]
        # Nested/helper edits cannot substitute for a top-level native owning approval.
        (loop.agents.path(worker) / "tools/indirect.py").write_text('''async def run(tool):
    """Attempt an indirect memory publication."""
    return await tool.invoke("edit_file", {"path":"memory/L2/forbidden.md","text":"unapproved"})
''')
        indirect = loop.jobs.submit(worker, "indirect", {}, turn="worker-approval")
        assert (await done(loop.jobs, indirect["id"]))["status"] == "failed"
        assert not (loop.agents.path(worker) / "memory/L2/forbidden.md").exists()
        # Accepted source is authoritative if materialization was interrupted.
        (loop.agents.path(worker) / "memory/L1.md").unlink()
        assert loop.context.memory(worker)["private"]["L1.md"] == arguments["text"]
        assert (loop.agents.path(worker) / "memory/L1.md").read_text() == arguments["text"]
    finally:
        await loop.jobs.close()
        loop.store.close()


async def test_compaction_uses_owner_and_current_memory_retains_native_originals(tmp_path):
    provider = Scripted(
        reply("Retain the literal user constraints and the exact evidence locations.")
    )
    loop = Loop(tmp_path, provider, trusted_tools=sys.platform != "darwin")
    loop.models.grant(4)
    actor = Actor("astra")
    first = loop.context.snapshot(actor)
    loop.messages.deliver(
        "user", "astra", "Exact owner correction: preserve whole evidence.", message_id="direction"
    )
    (direction,) = loop.messages.pending(actor)
    loop.store.event(
        "turn.received",
        actor.scope,
        {"inputs": ["direction"], "turn": "old-turn"},
        key="turn-received:old-turn",
    )
    original = [
        {"type": "reasoning", "encrypted_content": "opaque original", "summary": []},
        loop.context.content(direction),
    ]
    loop.context.append(actor, original, key="old-native")
    args = {
        "path": "memory/L1.md",
        "text": "New approved commitments",
        "expected_sha256": digest(b""),
    }
    loop.context.publish_memory(
        actor,
        {"id": "approved-edit", "name": "edit_file", "turn": "owning-turn", "arguments": args},
        args,
    )
    assert "New approved commitments" not in json.dumps(loop.context.history(actor))
    assert await loop.context.compact(actor, loop.models, force=True)
    snap = loop.context.snapshot(actor)
    history = loop.context.history(actor)
    assert snap["seq"] != first["seq"]
    assert "New approved commitments" in history[0]["content"]
    assert original[-1] in history
    assert loop.store.json(snap["data"]["original"])[1:] == original
    receipt = loop.store.by_key(snap["data"]["approval"])
    started = loop.store.by_key(
        snap["data"]["approval"].replace("model-completed:", "model-started:")
    )
    assert receipt["subject"] == "astra" and started["data"]["purpose"] == "compact"
    assert loop.store.read(snap["data"]["original"])
    assert not await loop.context.compact(actor, loop.models)
    assert len(provider.requests) == 1
    loop.store.close()


async def test_branch_compaction_keeps_owner_seed_and_does_not_publish_memory(tmp_path):
    loop = Loop(tmp_path, Scripted(reply("Branch working memo with unresolved question.")))
    loop.models.grant(2)
    request, definitions = loop.context.request(Actor("astra"), [])
    seed = {"request": loop.store.put(request), "tools": definitions}
    branch = loop.agents.fork(Actor("astra"), "branch-fixture", seed, "Check retained evidence")
    before = loop.context.history(Actor("astra"))
    assert await loop.context.compact(branch, loop.models, force=True)
    assert loop.context.history(branch)[0] == request["input"][0]
    assert "Temporary self-fork." in json.dumps(loop.context.history(branch))
    assert loop.context.history(Actor("astra")) == before
    assert not loop.store.events("memory.accepted")
    with pytest.raises(PermissionError):
        loop.agents.create("intruder", "sol", "No authority", principal=branch)
    loop.store.close()


async def test_empty_compaction_keeps_history_and_postpones_optional_retry(tmp_path):
    provider = Scripted(reply(""))
    loop = Loop(tmp_path, provider)
    loop.models.grant(3)
    actor = Actor("astra")
    loop.context.append(
        actor, [{"role": "user", "content": "retained evidence " * 12_000}], key="large-source"
    )
    original = loop.context.history(actor)
    snapshot = loop.context.snapshot(actor)
    assert not await loop.context.compact(actor, loop.models, force=True)
    assert loop.context.history(actor) == original
    assert loop.context.snapshot(actor) == snapshot
    assert loop.store.latest("context.compaction_rejected", actor.scope)
    loop.context.append(
        actor,
        [{"role": "user", "content": "Use the original evidence for the next request."}],
        key="new-input",
    )
    assert not await loop.context.compact(actor, loop.models)
    assert len(provider.requests) == 1
    assert "Use the original evidence" in loop.context.history(actor)[-1]["content"]
    loop.store.close()


def receive(loop, actor, sender, text, ident, *, kind="message", attachments=()):
    loop.messages.deliver(
        sender, actor.scope, text, message_id=ident, kind=kind, attachments=attachments
    )
    package = loop.store.by_key("message-prepare:" + ident)["data"]
    loop.store.event(
        "turn.received",
        actor.scope,
        {"inputs": [ident], "turn": ident},
        key="turn-received:" + ident,
    )
    loop.context.append(actor, [loop.context.content(package)], key="turn-history:" + ident)
    loop.messages.consume([package], ident)
    return package


async def test_compaction_counts_real_directions_and_does_not_pin_tool_or_peer_payloads(tmp_path):
    loop = Loop(
        tmp_path, Scripted(reply("Keep the three original directions and source references."))
    )
    loop.models.grant(2)
    astra = Actor("astra")
    loop.agents.create("worker", "sol", "Investigate", principal=astra)
    loop.context.snapshot(astra)
    evidence = "large evidence row " * 40_000
    ref = loop.store.put(evidence, "text/plain")
    receive(
        loop,
        astra,
        "user",
        "First literal instruction",
        "first",
        attachments=[{"name": "evidence.txt", "source": ref}],
    )
    receive(loop, astra, "worker", "Peer report " + evidence, "report")
    receive(loop, astra, "tool:read_file", "Tool result", "result", kind="tool_result")
    assert len(loop.context.instruction_inputs(astra)) == 1
    # Below the pressure threshold, tool and peer inputs cannot supply the
    # required three substantive directions for an automatic durable rebuild.
    from bloom41.models import visible
    from bloom41.tokens import count

    assert 250_000 < count(encoded(visible(loop.context.history(astra))).decode()) < 450_000
    assert not await loop.context.compact(astra, loop.models)
    receive(loop, astra, "user", "Second literal instruction", "second")
    receive(loop, astra, "user", "Third literal instruction", "third")
    assert await loop.context.compact(astra, loop.models)
    history = json.dumps(loop.context.history(astra))
    assert all(
        s in history
        for s in (
            "First literal instruction",
            "Second literal instruction",
            "Third literal instruction",
        )
    )
    assert evidence not in history and "Peer report" not in history
    assert "evidence.txt" in history and ref["sha256"] in history
    original = loop.store.json(loop.context.snapshot(astra)["data"]["original"])
    assert evidence in json.dumps(original)
    for n in range(5):
        receive(loop, astra, "tool:read_file", "Another result", f"result-{n}", kind="tool_result")
        assert not await loop.context.compact(astra, loop.models)
    assert len(loop.models.provider.requests) == 1
    loop.store.close()


async def test_completed_compaction_is_reused_after_interrupted_adoption_and_operator_resume(
    tmp_path, monkeypatch
):
    provider = Scripted(reply("An approved source-linked memo."))
    loop = Loop(tmp_path, provider)
    loop.models.grant(2)
    actor = Actor("astra")
    receive(loop, actor, "user", "Keep this exact instruction", "original")
    write = loop.store.event

    def interrupt(kind, *args, **kwargs):
        if kind == "context.snapshot" and args[1].get("approval"):
            raise RuntimeError("Exit after the completed purchase")
        return write(kind, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(loop.store, "event", interrupt)
        with pytest.raises(RuntimeError, match="Exit after"):
            await loop.context.compact(actor, loop.models, force=True)
    (purchase,) = loop.store.events("model.started")
    loop.store.event(
        "actor.blocked", actor.scope, {"error": "Interrupted adoption"}, key="blocked-adoption"
    )
    loop.store.close()
    reopened = Loop(tmp_path, provider)
    reopened.resume(actor, "Completed receipt checked; finish adoption")
    assert await reopened.context.compact(actor, reopened.models, force=True)
    assert len(provider.requests) == 1
    assert (
        reopened.context.snapshot(actor)["data"]["approval"]
        == "model-completed:" + purchase["data"]["id"]
    )
    assert "Keep this exact instruction" in json.dumps(reopened.context.history(actor))
    reopened.store.close()


async def test_nonproductive_compaction_is_deferred_despite_pressure_and_tool_turns(tmp_path):
    loop = Loop(tmp_path, Scripted(reply("A memo that adds no reduction.")))
    loop.models.grant(3)
    actor = Actor("astra")
    # Exact owner instructions are mandatory, even when they alone exceed the
    # pressure boundary. Repeated purchases cannot make them disappear.
    receive(loop, actor, "user", "exact owner constraint " * 160_000, "huge-direction")
    assert not await loop.context.compact(actor, loop.models)
    failure = loop.store.latest("context.compaction_rejected", actor.scope)
    assert failure["data"]["reason"] == "working context did not shrink"
    for n in range(4):
        receive(
            loop, actor, "tool:read_file", "small tool update", f"update-{n}", kind="tool_result"
        )
        assert not await loop.context.compact(actor, loop.models)
    assert len(loop.models.provider.requests) == 1
    loop.store.close()


async def test_branch_protects_only_instructions_in_its_exact_parent_seed(tmp_path):
    loop = Loop(tmp_path, Scripted(reply("Branch evidence memo.")))
    loop.models.grant(2)
    astra = Actor("astra")
    loop.agents.create("worker", "sol", "Investigate", principal=astra)
    worker = Actor("worker")
    receive(loop, worker, "astra", "Original parent direction", "assignment")
    request, tools = loop.context.request(worker, [])
    branch = loop.agents.fork(
        worker,
        "scoped-branch",
        {"request": loop.store.put(request), "turn": "assignment", "tools": tools},
        "Check this subquestion",
    )
    receive(loop, worker, "astra", "Later parent-only correction", "later")
    receive(loop, branch, "worker", "Exact branch direction", "branch-direction")
    assert await loop.context.compact(branch, loop.models, force=True)
    history = json.dumps(loop.context.history(branch))
    assert "Original parent direction" in history and "Exact branch direction" in history
    assert "Later parent-only correction" not in history
    loop.store.close()
