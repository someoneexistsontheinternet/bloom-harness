"""Ordinary IO and transport preserve bytes and authority without buying inference."""

import hashlib
import json
import sys

from bloom41.agents import Actor
from bloom41.loop import Loop
from test_delivery import done, turn
from test_models_context import Scripted


async def test_large_file_pages_preserve_exact_requested_range_without_readers(tmp_path):
    loop = Loop(tmp_path, Scripted(), trusted_tools=sys.platform != "darwin")
    owner = Actor("astra")
    text = "outside prefix\r\n" + ('quoted "x" \\ café ' * 5000) + "\r\noutside suffix\n"
    path = loop.agents.path(owner) / "work/source.txt"
    path.write_bytes(text.encode())
    await loop.jobs.start()
    try:
        first = loop.jobs.submit(owner, "read_file", {"path": "source.txt", "start": 2, "end": 2})
        record = await done(loop.jobs, first["id"])
        assert record["status"] == "completed", record
        result = loop.store.json(record["result"])
        chunks = [result["text"]]
        assert result["next_start_char"] is not None
        while result["next_start_char"] is not None:
            job = loop.jobs.submit(
                owner,
                "read_file",
                {
                    "path": "source.txt",
                    "start_char": result["next_start_char"],
                    "end_char": result["requested_end_char"],
                },
            )
            record = await done(loop.jobs, job["id"])
            result = loop.store.json(record["result"])
            chunks.append(result["text"])
        assert "".join(chunks) == text.splitlines(keepends=True)[1]
        await loop.notify_jobs()
        assert not loop.store.events("model.started")
        assert {j["name"] for j in loop.jobs.states()} == {"read_file"}
        packages = loop.messages.pending(owner)
        assert packages and all(p["attachments"][0]["expose"] == "inline" for p in packages)
    finally:
        await loop.jobs.close()
        loop.store.close()


async def test_source_reference_forwarding_copies_dependencies_without_private_access(tmp_path):
    loop = Loop(tmp_path, Scripted(), trusted_tools=sys.platform != "darwin")
    owner = Actor("markets")
    loop.agents.create(owner.agent, "sol", "Research", principal=Actor("astra"))
    original = "Retained original.\r\n" * 4000
    path = loop.agents.path(owner) / "work/evidence.txt"
    path.write_bytes(original.encode())
    prep = loop.jobs.submit(owner, "read_file", {"path": "evidence.txt"})
    view = await loop.delivery.attach_value(
        owner,
        prep,
        {
            "name": "selection.json",
            "value": {"summary": "The selected evidence", "reference_files": [str(path)]},
        },
    )
    artifact = view["source_id"]
    raw_id = "source:" + hashlib.sha256(original.encode()).hexdigest()
    # No other owner's private directories or unpublished artifacts are granted.
    for reference in (str(path), artifact, raw_id):
        try:
            loop.delivery.path(Actor("astra"), reference)
        except (PermissionError, FileNotFoundError):
            pass
        else:
            raise AssertionError("Unpublished owner data was visible")
    text = "---\ntarget: astra\n---\nHere is the prepared evidence: " + artifact
    turn(loop.store, owner, "send", text)
    loop.save_turn(
        loop.turn("send"), input_ids=[], status="completed", agent=owner.agent, branch=None
    )
    await loop.jobs.start()
    try:
        send = loop.jobs.submit(owner, "send_message", {"recipient": "astra"}, turn="send")
        result = await done(loop.jobs, send["id"])
        assert result["status"] == "completed", result
        (packet,) = loop.messages.pending("astra")
        assert loop.store.read(packet["body"]).decode() == text
        assert loop.delivery.path(Actor("astra"), raw_id).read_bytes() == original.encode()
        assert artifact in loop.context.content(packet)["content"][1]["text"]
        published = json.loads(loop.delivery.path(Actor("astra"), artifact).read_bytes())
        assert published["reference_files"] == [raw_id]
        assert not loop.store.events("model.started")
        denied = loop.jobs.submit(
            Actor("astra"), "edit_file", {"path": raw_id, "text": "overwrite"}
        )
        assert (await done(loop.jobs, denied["id"]))["status"] == "failed"
        assert loop.delivery.path(Actor("astra"), raw_id).read_bytes() == original.encode()
        # A new file revision makes a new derived result identity, even though
        # its original tool value only named the same mutable path.
        path.write_text("revised")
        next_job = loop.jobs.submit(owner, "read_file", {"path": "evidence.txt"})
        revised = await loop.delivery.attach_value(
            owner,
            next_job,
            {
                "name": "selection.json",
                "value": {"summary": "The selected evidence", "reference_files": [str(path)]},
            },
        )
        assert revised["source_id"] != artifact
    finally:
        await loop.jobs.close()
        loop.store.close()


async def test_generic_large_result_and_plain_attachment_never_invoke_luna(tmp_path):
    loop = Loop(tmp_path, Scripted(), trusted_tools=sys.platform != "darwin")
    actor = Actor("astra")
    job = loop.jobs.submit(actor, "find", {"query": "x"})
    value = {"matches": ["x" * 2000] * 100, "summary": "100 exact matches retained"}
    loop.jobs.state(job["id"], "completed", result=loop.store.put(value))
    await loop.notify_jobs()
    (packet,) = loop.messages.pending(actor)
    assert packet["attachments"][0]["expose"] == "reference"
    assert packet["attachments"][0]["preview"] == value["summary"]
    assert not loop.store.events("model.started")
    assert [j["name"] for j in loop.jobs.states()] == ["find"]
    loop.store.close()
