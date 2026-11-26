import asyncio
import sys

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
