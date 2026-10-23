import json
from pathlib import Path

import pytest

from bloom import reset, rewind
from bloom.context import Context
from bloom.memory import Memory
from bloom.runtime import Runtime
from bloom.store import Store, now
from test_memory import Curator, approve_fixture, fact


async def branch(store):
    prior = store.new_task("Owner preference: retain the orchid marker.")
    await approve_fixture(Memory(store, Curator(store, lambda data: [fact(data)])), task=prior)
    Context(store, None).memory(
        "global", 0, "Prior accepted summary", "Prior accepted detail", "fixture", []
    )
    store.status(prior, "completed")
    prefix = Context(store, None).selected("astra", prior)
    question = store.new_task("Original question to reroll", channel="discord", address="123")
    owner = store.one("SELECT id FROM items WHERE task_id=?", (question,))["id"]
    digest = store.put({})
    store.db.execute(
        "INSERT INTO model_calls(id,task_id,agent_id,purpose,model_requested,provider,status,request_hash,response_hash,started_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "original-first",
            question,
            "astra",
            "turn",
            "fixture",
            "fixture",
            "completed",
            digest,
            digest,
            now(),
            now(),
        ),
    )
    store.db.executemany(
        "INSERT INTO model_call_items VALUES(?,?,?)",
        [
            ("original-first", r["id"], i)
            for i, r in enumerate(Context(store, None).selected("astra", question))
        ],
    )
    answer = store.item(
        "astra", {"role": "assistant", "content": "The later branch answer"}, question
    )
    delivery = Runtime(store).delivery.queue(answer, question, "The later branch answer")
    store.db.execute(
        "UPDATE deliveries SET status='sent',external_id='sent-once',sent_at=?", (now(),)
    )
    store.db.execute(
        "INSERT INTO inbound_messages VALUES(?,?,?,?,?,?)",
        ("discord", "owner-once", "123", store.put({"original": True}), question, now()),
    )
    store.db.execute(
        "INSERT INTO discord_cursors VALUES(?,?,?)", ("123", "current-high-water", now())
    )
    store.db.execute(
        "INSERT INTO owner_requests VALUES(?,?,?,?,?,?,?,?)",
        ("old-request", digest, "message", "completed", question, digest, "source", now()),
    )
    Context(store, None).memory(
        "global", 1, "Later summary must be archived", "Later detail", "fixture", []
    )
    store.status(question, "completed")
    later = store.new_task("Later follow-up must not replay")
    store.status(later, "blocked", "Retained original failure")
    marker = store.config.root / "existing-result.txt"
    marker.write_text("An actual external/file effect remains")
    originals = {
        r["hash"]: store.bytes(r["hash"]) for r in store.rows("SELECT hash FROM artifacts")
    }
    expected = [{"id": r["id"], "envelope_hash": r["envelope_hash"]} for r in prefix]
    return owner, expected, originals


async def test_rewind_exact_prefix_memory_originals_and_delivery_dedup(store):
    item, prefix, originals = await branch(store)
    config = store.config
    facts = store.rows("SELECT * FROM memory_facts")
    store.close()
    state = rewind.apply(config, item, "same-reroll")
    assert state["rewind"]["prefix"] == prefix
    assert reset.verify_bundle(Path(state["backup"]["path"]))["verified"]
    with reset.reader(config) as restored:
        rewind.verify_database(restored, state["rewind"])
        assert restored.one("SELECT summary FROM memories")["summary"] == "Prior accepted summary"
        assert restored.rows("SELECT * FROM memory_facts") == facts
        assert restored.one("SELECT * FROM deliveries")["external_id"] == "sent-once"
        assert restored.one("SELECT * FROM deliveries")["task_id"] is None
        assert restored.one("SELECT * FROM inbound_messages")["task_id"] is None
        assert restored.one("SELECT * FROM owner_requests")["task_id"] is None
        assert (
            restored.one("SELECT message_id FROM discord_cursors")["message_id"]
            == "current-high-water"
        )
        for digest, data in originals.items():
            assert restored.bytes(digest, touch=False, cache=False) == data
        assert not restored.rows(
            "SELECT * FROM search_items WHERE search_items MATCH ?", ('"follow-up"',)
        )
    assert (config.root / "existing-result.txt").exists()
    first = rewind.submit(config, state)
    second = rewind.submit(config, rewind.apply(config, item, "same-reroll"))
    assert first == second
    with reset.reader(config) as restored:
        assert restored.one("SELECT count(*) n FROM tasks WHERE id=?", (first["task"],))["n"] == 1
        assert restored.one("SELECT count(*) n FROM deliveries")["n"] == 1
        assert (
            restored.one(
                "SELECT count(*) n FROM owner_requests WHERE id=?", ("rewind-same-reroll",)
            )["n"]
            == 1
        )
        assert not restored.rows("PRAGMA foreign_key_check")


@pytest.mark.parametrize("boundary", ["retire", "install"])
async def test_rewind_resumes_publication_without_second_backup_or_enqueue(
    store, monkeypatch, boundary
):
    item, _, _ = await branch(store)
    config = store.config
    store.close()
    replace = reset.os.replace

    def crash(source, target):
        replace(source, target)
        if (
            boundary == "retire"
            and "retired" in Path(target).parts
            and Path(source).name == "bloom.sqlite3"
        ) or (boundary == "install" and Path(source).name == "database-fresh.sqlite3"):
            raise OSError("simulated publication interruption")

    with monkeypatch.context() as patch:
        patch.setattr(reset.os, "replace", crash)
        with pytest.raises(OSError, match="interruption"):
            rewind.apply(config, item, "resume-reroll")
    before = json.loads((config.workspace / "reset-pending.json").read_text())
    with pytest.raises(RuntimeError, match="reset is incomplete"):
        Store(config)
    after = rewind.apply(config, item, "resume-reroll")
    assert after["id"] == before["id"] and after["backup"] == before["backup"]
    rewind.submit(config, after)
    assert rewind.submit(config, after) == rewind.submit(config, after)


async def test_rewind_refuses_changed_prior_task_and_active_effect(store):
    item, _, _ = await branch(store)
    prior = store.one("SELECT id FROM tasks ORDER BY created_at LIMIT 1")["id"]
    store.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now(), prior))
    with pytest.raises(ValueError, match="earlier task changed"):
        rewind.specification(store.config, item, "unsafe")
