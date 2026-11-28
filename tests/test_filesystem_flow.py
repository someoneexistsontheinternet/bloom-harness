"""Transferable evidence references, private scratch and actual owner memory files."""

import sys
from pathlib import Path

import pytest

from bloom41.agents import Actor
from bloom41.loop import Loop
from bloom41.store import encoded
from bloom41.tool import Tool
from test_models_context import Scripted
from test_tools import settled


def client(loop, actor):
    return Tool(
        {
            "actor": actor.agent,
            "scope": actor.scope,
            "job": "fixture",
            "agent_root": str(loop.agents.path(actor)),
            "read_roots": [str(p) for p in loop.agents.read_roots(actor)],
            "memory_root": str(loop.agents.path(actor.agent) / "memory"),
            "shared_root": str(loop.store.root / "workspace/shared"),
        }
    )


def branch(loop, name="child", parent=Actor("astra"), packages=()):
    request, tools = loop.context.request(parent, packages)
    return loop.agents.fork(
        parent, name, {"request": loop.store.put(request), "tools": tools}, "Check evidence"
    )


async def test_inherited_attachments_are_readable_in_fork_and_monitor_real_workers(tmp_path):
    loop = Loop(tmp_path, Scripted(), trusted_tools=sys.platform != "darwin")
    loop.messages.deliver(
        "user",
        "astra",
        "Use this source",
        message_id="source",
        attachments=[
            {"name": "evidence.txt", "source": loop.store.put("Exact source", "text/plain")}
        ],
    )
    package = loop.messages.pending("astra")[0]
    child = branch(loop, packages=[package])
    path = Path("inbox/source/attachments") / package["attachments"][0]["name"]
    absolute = loop.agents.path("astra") / path
    assert "Attachment: " + str(absolute) in encoded(loop.context.content(package)).decode()
    parent_input = loop.store.json(
        loop.store.latest("branch.created", child.scope)["data"]["seed"]["request"]
    )["input"]
    child_request, _ = loop.context.request(child, [])
    assert child_request["input"][:-1] == parent_input
    loop.messages.deliver(
        child.scope,
        child.scope,
        "Branch evidence",
        message_id="branch-source",
        attachments=[
            {"name": "branch.txt", "source": loop.store.put("Branch capture", "text/plain")}
        ],
    )
    owned = loop.messages.pending(child)[0]
    branch_relative = Path("inbox/branch-source/attachments") / owned["attachments"][0]["name"]
    await loop.jobs.start()
    try:
        for actor, reference, expected in [
            (child, path, "Exact source"),
            (child, absolute, "Exact source"),
            (Actor("terra-astra"), path, "Exact source"),
            (Actor("terra-astra"), branch_relative, "Branch capture"),
        ]:
            job = loop.jobs.submit(actor, "read_file", {"path": str(reference)})
            done = await settled(loop.jobs, job["id"])
            assert done["status"] == "completed", done
            assert loop.store.json(done["result"])["text"] == expected
            assert loop.delivery.path(actor, str(reference)) == client(loop, actor).path(reference)
    finally:
        await loop.jobs.close()
        loop.store.close()


def test_legacy_paths_are_unambiguous_read_only_and_confined_to_ancestor_grants(tmp_path):
    loop = Loop(tmp_path, Scripted())
    parent = branch(loop, "parent")
    child = branch(loop, "child", parent)
    sibling = branch(loop, "sibling")
    assert loop.agents.path(child) in loop.agents.read_roots(Actor("astra"))
    loop.agents.create("unrelated", "sol", "Separate owner", principal=Actor("astra"))
    assert loop.agents.path(child) not in loop.agents.read_roots(Actor("unrelated"))
    legacy = Path("inbox/source/attachments/evidence.txt")
    source = loop.agents.path("astra") / legacy
    source.parent.mkdir(parents=True)
    source.write_text("original")
    tool = client(loop, child)
    assert tool.path(legacy) == source
    with pytest.raises(PermissionError):
        tool.path(legacy, write=True)
    second = loop.agents.path(parent) / legacy
    second.parent.mkdir(parents=True)
    second.write_text("ambiguous")
    with pytest.raises(ValueError, match="Ambiguous"):
        tool.path(legacy)
    assert tool.path(source) == source
    own = loop.agents.path(child) / legacy
    own.parent.mkdir(parents=True)
    own.write_text("own")
    assert tool.path(legacy) == own
    with pytest.raises(PermissionError):
        tool.path(loop.agents.path(sibling) / "scratch/private.txt")
    escaped = loop.agents.path("astra") / "inbox/escape"
    escaped.symlink_to(loop.agents.path(sibling))
    with pytest.raises(PermissionError, match="escapes"):
        tool.path("inbox/escape/identity.toml")
    loop.store.close()


async def test_private_scratch_and_read_only_owner_memory_in_real_fork(tmp_path):
    loop = Loop(tmp_path, Scripted(), trusted_tools=sys.platform != "darwin")
    owner = loop.agents.path("astra")
    (owner / "memory/L1.md").write_text("Durable owner knowledge")
    child = branch(loop)
    root = loop.agents.path(child)
    assert (root / "memory").is_symlink()
    assert (root / "memory/L1.md").read_text() == "Durable owner knowledge"
    assert client(loop, child).scratch == root / "scratch"
    await loop.jobs.start()
    try:
        edit = loop.jobs.submit(child, "edit_file", {"path": "~/scratch/draft.md", "text": "draft"})
        assert (await settled(loop.jobs, edit["id"]))["status"] == "completed"
        assert (root / "scratch/draft.md").read_text() == "draft"
        assert not (owner / "scratch/draft.md").exists()
        shell = loop.jobs.submit(
            child,
            "bash",
            {"command": 'printf shell > "$BLOOM_SCRATCH/shell.txt"; cat ../memory/L1.md'},
        )
        done = await settled(loop.jobs, shell["id"])
        assert done["status"] == "completed", done
        assert loop.store.json(done["result"])["stdout"] == "Durable owner knowledge"
        assert (root / "scratch/shell.txt").read_text() == "shell"
        denied = loop.jobs.submit(child, "edit_file", {"path": "memory/L1.md", "text": "forbidden"})
        assert (await settled(loop.jobs, denied["id"]))["status"] == "failed"
        if sys.platform == "darwin":
            denied = loop.jobs.submit(
                child, "bash", {"command": "printf forbidden > ../memory/L1.md"}
            )
            done = await settled(loop.jobs, denied["id"])
            assert loop.store.json(done["result"])["exit_code"] != 0
        assert (owner / "memory/L1.md").read_text() == "Durable owner knowledge"
    finally:
        await loop.jobs.close()
        loop.store.close()


async def test_history_uses_exact_prepared_inputs_even_if_renderer_changes(tmp_path, monkeypatch):
    loop = Loop(tmp_path, Scripted({"status": "completed", "output": []}))
    loop.models.grant(1)
    loop.messages.deliver(
        "tool:fixture", "astra", "Exact input", message_id="input", kind="tool_result"
    )
    packages = loop.messages.pending("astra")
    original = loop.dispatch

    def dispatch(actor, turn):
        monkeypatch.setattr(
            loop.context, "content", lambda *a, **k: {"content": "changed renderer"}
        )
        original(actor, turn)

    monkeypatch.setattr(loop, "dispatch", dispatch)
    await loop.advance(Actor("astra"), packages)
    (turn,) = loop.turns()
    assert turn["status"] == "completed"
    request = loop.store.json(turn["request"])
    history = loop.store.by_key("turn-history:" + turn["id"])
    assert loop.store.json(history["data"]["items"]) == request["input"][turn["input_start"] :]
    # Re-dispatch after an interrupted adoption reuses the same history receipt.
    original(Actor("astra"), turn)
    assert len(loop.store.events("history.appended")) == 1
    loop.store.close()


async def test_legacy_empty_response_resumes_with_original_relative_inputs_and_no_repurchase(
    tmp_path,
):
    provider = Scripted({"status": "completed", "output": []})
    loop = Loop(tmp_path, provider)
    actor = Actor("astra")
    loop.messages.deliver(
        "user",
        "astra",
        "Original request",
        message_id="legacy-input",
        attachments=[
            {"name": "source.txt", "source": loop.store.put("Retained source", "text/plain")}
        ],
    )
    (package,) = loop.messages.pending(actor)
    request, definitions = loop.context.request(actor, [package])
    original_input = loop.context.content(package, legacy_paths=True)
    request["input"][-1] = original_input
    loop.models.grant(1)
    response = await loop.models.call(actor, request, call_id="legacy-turn")
    loop.save_turn(
        {
            "id": "legacy-turn",
            "agent": "astra",
            "branch": None,
            "scope": "astra",
            "status": "blocked",
            "input_ids": [package["id"]],
            "request": loop.store.put(request),
            "tools": definitions,
            "origin": package["id"],
            "reply_to": "user",
            "context_request": None,
            "response": loop.store.put(response),
            "final": loop.store.put("", "text/plain"),
        }
    )
    loop.resume(actor, "Inspected the completed empty response")
    await loop.advance(actor, pending_turn=loop.turn("legacy-turn"))
    assert loop.turn("legacy-turn")["status"] == "completed"
    history = loop.store.by_key("turn-history:legacy-turn")
    assert loop.store.json(history["data"]["items"]) == [original_input]
    assert "Attachment: inbox/legacy-input/" in encoded(original_input).decode()
    assert len(provider.requests) == 1
    loop.dispatch(actor, loop.turn("legacy-turn"))
    assert len(loop.store.events("history.appended")) == 1
    loop.store.close()
