import asyncio
from pathlib import Path
import sys
import json

import pytest

from bloom41.agents import Actor, Agents
from bloom41.delivery import Delivery
from bloom41.jobs import Jobs, TERMINAL
from bloom41.messages import Messages
from bloom41.store import Store


def turn(store, actor, ident, text):
    store.event(
        "turn.state",
        ident,
        {
            "id": ident,
            "scope": actor.scope,
            "final": store.put(text, "text/plain"),
            "origin": "owner-message",
        },
        key="fixture-turn:" + ident,
    )


async def done(jobs, ident):
    for _ in range(300):
        await jobs.pump()
        if jobs.get(ident)["status"] in TERMINAL:
            return jobs.get(ident)
        await asyncio.sleep(0.02)
    raise AssertionError("Preparation did not settle")


async def test_value_preparation_survives_reindex_and_reuses_frozen_evidence(tmp_path, monkeypatch):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    actor = Actor("astra")
    source = agents.path(actor) / "work/source.txt"
    source.write_text("first revision")
    jobs = Jobs(store)
    job = jobs.submit(actor, "read_file", {"path": "source.txt"})
    delivery = Delivery(store, jobs)
    args = {
        "name": "result.json",
        "value": {"text": "x" * 50_001, "files": [str(source)], "nested": [jobs.token(job["id"])]},
    }
    first = await delivery.attach_value(actor, job, args)
    assert first["expose"] == "reference"
    assert store.json(first["source"])["nested"] == ["[REDACTED]"]
    source.write_text("later revision")
    snapshot = Path(first["snapshot_path"])
    snapshot.write_text("edited local view")
    store.sync(rebuild=True)

    def repeated_clean(_):
        raise AssertionError("Unchanged pending extraction must not repeat preparation")

    monkeypatch.setattr(jobs, "clean", repeated_clean)
    reopened = Delivery(store, jobs)
    assert await reopened.attach_value(actor, job, args) == first
    assert snapshot.read_bytes() == store.read(first["source"])
    snapshot.unlink()
    assert await reopened.attach_value(actor, job, args) == first
    related = store.by_key(first["related"][0])["data"]
    assert store.read(related["source"]) == b"first revision"
    assert len(store.events("attachment.value")) == 1
    monkeypatch.undo()
    changed = await reopened.attach_value(actor, job, {"name": "result.json", "value": "different"})
    assert changed["id"] != first["id"]
    store.close()


async def test_send_freezes_files_waits_selected_jobs_and_preserves_final_text(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    agents.create("markets", "sol", "Market research", principal=Actor("astra"))
    actor = Actor("markets")
    root = agents.path(actor)
    file = root / "work/filing.txt"
    file.write_bytes(b"original\r\n")
    (root / "tools/prepare.py").write_text('''import asyncio
async def run(tool):
    """Prepare a cited fixture."""
    await asyncio.sleep(.2)
    return {"text":"context for Astra"}
''')
    jobs = Jobs(store, trusted_tools=sys.platform != "darwin")
    delivery = Delivery(store, jobs)
    jobs.handlers.update(delivery.handlers())
    await jobs.start()
    try:
        text = "---\ntarget: astra\n---\nMy exact report.\n"
        turn(store, actor, "t1", text)
        existing = jobs.submit(actor, "bash", {"command": "sleep .25; printf evidence"})
        unrelated = jobs.submit(actor, "bash", {"command": "sleep 20"})
        send = jobs.submit(
            actor,
            "send_message",
            {
                "recipient": "astra",
                "additional_content": {
                    "files": ["filing.txt"],
                    "results": [existing["id"]],
                    "tools": [{"name": "prepare", "args": {}}],
                },
            },
            turn="t1",
        )
        for _ in range(100):
            await jobs.pump()
            await asyncio.sleep(0.01)
            if store.events("attachment.file"):
                break
        assert not Messages(store).pending("astra")
        file.write_text("new revision")
        result = await done(jobs, send["id"])
        assert result["status"] == "completed", result
        assert jobs.get(unrelated["id"])["status"] == "running"
        (package,) = Messages(store).pending("astra")
        assert store.read(package["body"]).decode() == text
        assert store.read(package["attachments"][0]["source"]) == b"original\r\n"
        assert len(package["attachments"]) == 3
        assert any(b"context for Astra" in store.read(a["source"]) for a in package["attachments"])
        repeat = await delivery.publish_message(
            actor, jobs.get(send["id"]), {"recipient": "astra", "attachments": []}
        )
        assert repeat["status"] == "delivered" and len(Messages(store).pending("astra")) == 1
        assert not store.events("model.started")
    finally:
        await jobs.close()
        store.close()


async def test_prepared_context_survives_tool_only_response_and_missing_extraction(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    actor = Actor("astra")
    (agents.path(actor) / "tools/broken.py").write_text('''async def run(tool):
    """An unavailable evidence reader."""
    raise ValueError("source unavailable")
''')
    jobs = Jobs(store, trusted_tools=sys.platform != "darwin")
    delivery = Delivery(store, jobs)
    jobs.handlers.update(delivery.handlers())
    await jobs.start()
    try:
        turn(store, actor, "t1", "")
        first = jobs.submit(
            actor,
            "send_message",
            {
                "recipient": "user",
                "additional_content": {"tools": [{"name": "broken", "args": {}}]},
            },
            turn="t1",
        )
        result = await done(jobs, first["id"])
        assert store.json(result["result"])["status"] == "needs_final_message"
        assert not Messages(store).pending("user")
        turn(store, actor, "t2", "---\ntarget: user\n---\nThe source was unavailable.")
        second = jobs.submit(actor, "send_message", {"recipient": "user"}, turn="t2")
        result = await done(jobs, second["id"])
        assert result["status"] == "completed", result
        (package,) = Messages(store).pending("user")
        assert len(package["attachments"]) == 1
        assert b"source unavailable" in store.read(package["attachments"][0]["source"])
        assert len([j for j in jobs.states() if j["name"] == "broken"]) == 1
    finally:
        await jobs.close()
        store.close()


@pytest.mark.parametrize("mode", ["missing-purpose", "purpose", "reference", "failed-reader"])
async def test_large_sender_evidence_never_uses_an_empty_question_or_blocks_delivery(
    tmp_path, mode
):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    actor = Actor("astra")
    root = agents.path(actor)
    original = "Original source evidence. " * 3000
    (root / "work/source.txt").write_text(original)
    (root / "tools/extract.py").write_text('''async def run(tool, path: str, question: str):
    """Return an exact purpose or a scoped reader failure for this fixture."""
    assert "Verify the dated condition" in question
    if "simulate failure" in question:
        raise ValueError("fixture source unavailable")
    return {"summary": "Verified condition", "question": question}
''')
    jobs = Jobs(store, trusted_tools=sys.platform != "darwin")
    delivery = Delivery(store, jobs)
    jobs.handlers.update(delivery.handlers())
    await jobs.start()
    try:
        turn(store, actor, "prepare", "")
        extra = {"references" if mode == "reference" else "files": ["source.txt"]}
        if mode in ("purpose", "failed-reader"):
            extra["question"] = "Verify the dated condition" + (
                "; simulate failure" if mode == "failed-reader" else ""
            )
        job = jobs.submit(
            actor,
            "send_message",
            {"recipient": "user", "additional_content": extra},
            turn="prepare",
        )
        prepared = await done(jobs, job["id"])
        assert prepared["status"] == "completed", prepared
        outcome = store.json(prepared["result"])
        assert outcome["status"] == "needs_final_message"
        assert not Messages(store).pending("user")
        readers = [j for j in jobs.states() if j["name"] == "extract"]
        assert not readers  # A question on a transport call does not authorize hidden inference.
        # Publication reuses the exact already prepared revision, even if the
        # source has changed before the addressed final message arrives.
        (root / "work/source.txt").write_text("Later unselected source revision")
        text = "---\ntarget: user\n---\nMy exact final message."
        turn(store, actor, "final", text)
        send = jobs.submit(actor, "send_message", {"recipient": "user"}, turn="final")
        assert (await done(jobs, send["id"]))["status"] == "completed"
        (package,) = Messages(store).pending("user")
        assert store.read(package["body"]).decode() == text
        assert store.read(package["attachments"][0]["source"]).decode() == original
        assert package["attachments"][0]["expose"] == "reference"
        assert len(package["attachments"]) == 1
        assert not store.events("model.started")
    finally:
        await jobs.close()
        store.close()
