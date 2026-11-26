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
    original = [
        {"type": "reasoning", "encrypted_content": "opaque original", "summary": []},
        {"role": "user", "content": "Exact owner correction: preserve whole evidence."},
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
    assert any(i.get("content") == original[-1]["content"] for i in history)
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
