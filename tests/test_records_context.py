import json
import sqlite3

import pytest

from bloom.context import Context
from bloom.operations import export_calls
from bloom.store import now


def test_relational_history_and_single_main(store):
    task = store.new_task("Keep the original correction")
    one = store.item(
        "astra", {"role": "assistant", "phase": "commentary", "content": "Working"}, task
    )
    two = store.item(
        "astra", {"role": "assistant", "phase": "final_answer", "content": "Done"}, task
    )
    assert one < two
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute(
            "INSERT INTO agents VALUES('other','astra',NULL,'Other','full',1,'.','now')"
        )
    assert (
        store.readonly("SELECT phase FROM items WHERE id=?", (two,))[0]["phase"] == "final_answer"
    )
    with pytest.raises(sqlite3.OperationalError):
        store.readonly("DELETE FROM items")
    assert store.readonly(
        "SELECT i.text FROM search_items f JOIN items i ON i.id=f.rowid WHERE search_items MATCH 'correction'"
    )
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_memory_revision_conflict_and_history(store):
    context = Context(store, None)
    result = context.memory(
        "preferences/code",
        0,
        "Use project Python.",
        "Keep native tools.",
        "User corrected this.",
        ["item:1"],
    )
    assert result["revision"] == 1
    with pytest.raises(ValueError, match="Stale"):
        context.memory("preferences/code", 0, "Wrong overwrite", "", "", [])
    assert store.one("SELECT summary FROM memories")["summary"] == "Use project Python."
    context.memory(
        "preferences/code", 1, "Use the project venv.", "", "Clarify interpreter.", ["item:2"]
    )
    assert len(store.rows("SELECT * FROM memory_revisions")) == 2


def test_originals_survive_recent_export_pruning(store):
    store.config.data["runtime"]["recent_main_calls"] = 1
    for i in range(3):
        digest = store.put({"original": i})
        store.db.execute(
            "INSERT INTO model_calls(id,agent_id,purpose,model_requested,provider,status,request_hash,response_hash,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (f"call_{i}", "astra", "test", "requested", "test", "completed", digest, digest, now()),
        )
    assert export_calls(store)["recent_json_files"] == 1
    assert len(list((store.config.artifacts / "runtime/calls/main").rglob("receipt.json"))) == 1
    assert len(store.rows("SELECT * FROM model_calls")) == 3
    for row in store.rows("SELECT request_hash FROM model_calls"):
        assert "original" in store.get(row["request_hash"])


def test_context_configuration_preserves_user_correction(store):
    for role in ("astra", "sol", "luna"):
        assert store.config.profile(role)["context_limit"] == 1_000_000
    assert store.config.profile("astra")["compact_at"] == 400000
    assert store.config.profile("sol")["compact_at"] == 272000
    assert store.config.profile("luna")["compact_at"] == 512000
    assert store.config.profile("luna")["compact_to"] == 256000


async def test_long_single_user_turn_compacts_without_splitting_tool_pairs(store):
    store.config.data["memory"]["enabled"] = False  # Retained worker/legacy compaction path.
    from test_runtime_delivery import saved, message

    task = store.new_task("Preserve this original research question and correction")
    for i in range(20):
        call = saved(store, task, [])
        store.item("astra", {"type": "reasoning", "summary": []}, task, call)
        store.item(
            "astra",
            {
                "type": "function_call",
                "call_id": f"tool{i}",
                "name": "read_file",
                "arguments": "{}",
            },
            task,
            call,
        )
        store.item(
            "astra",
            {
                "type": "function_call_output",
                "call_id": f"tool{i}",
                "output": "Observation " + str(i) + ": " + "evidence " * 700,
            },
            task,
        )
    original_count = store.one("SELECT count(*) AS n FROM items")["n"]

    class Summarizer:
        async def infer(self, *args, **kwargs):
            output = [
                message(
                    "Original research question and correction preserved. Source item 1 remains authoritative. Recent evidence remains below."
                )
            ]
            call = saved(store, task, output)
            store.db.execute("UPDATE model_calls SET purpose='compaction' WHERE id=?", (call,))
            return call, {"output": output}

    context = Context(store, Summarizer())
    assert await context.compact("astra", task, target=5000)
    selected = context.selected("astra")
    assert len(selected) < original_count
    calls = {r["call_id"] for r in selected if r["kind"] == "function_call"}
    results = {r["call_id"] for r in selected if r["kind"] == "function_call_output"}
    assert calls == results
    assert store.one("SELECT count(*) AS n FROM items")["n"] == original_count + 1
    assert store.one("SELECT text FROM items WHERE id=1")["text"].startswith(
        "Preserve this original"
    )
