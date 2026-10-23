import asyncio
import json
import os

import pytest

from bloom.context import Context
from bloom.runtime import Runtime
from bloom.store import atomic
from test_runtime_delivery import message, saved


@pytest.mark.parametrize("steering", [False, True])
async def test_final_generated_before_new_input_is_reconsidered_after_restart(store, steering):
    settings = store.config.data["discord"]
    task = store.new_task("Original request", channel="discord", address=settings["channel_id"])
    runtime = Runtime(store)

    async def stale(*args, **kwargs):
        if steering:
            runtime.steer(task, "Correction received during inference")
        else:
            runtime.receive_discord(
                {
                    "id": "late-owner",
                    "channel_id": settings["channel_id"],
                    "author": {"id": settings["owner_id"]},
                    "content": "Ordinary follow-up during inference",
                }
            )
        output = [message("Answer that did not see the new input")]
        return saved(store, task, output, kwargs["item_ids"]), {"output": output}

    runtime.models.infer = stale
    await runtime.step(task)
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "ready"
    assert not store.rows("SELECT * FROM deliveries")
    assert len(store.rows("SELECT id FROM tasks")) == 1
    runtime = Runtime(store)
    runtime.recover()

    async def fresh(agent, selected, instructions, items, tools, **kwargs):
        assert any(("during inference" in json.dumps(i)) for i in items)
        output = [message("Revised answer includes the new input")]
        return saved(store, task, output, kwargs["item_ids"]), {"output": output}

    runtime.models.infer = fresh
    await runtime.step(task)
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert (
        store.one(
            "SELECT text FROM items WHERE id=(SELECT final_item FROM tasks WHERE id=?)", (task,)
        )["text"]
        == "Revised answer includes the new input"
    )
    assert len(store.rows("SELECT id FROM model_calls")) == 2


async def test_compaction_excludes_queued_task_but_preserves_it_for_its_turn(store):
    store.config.data["memory"]["enabled"] = (
        False  # New full-compaction coverage is in test_memory.
    )
    task = store.new_task("Active owner objective")
    queued = store.new_task("Queued private objective must wait")
    for number in range(15):
        store.item("astra", message("Active evidence " + "detail " * 500, "commentary"), task)

    class Summarizer:
        async def infer(self, agent, selected, instructions, items, tools, **kwargs):
            assert "Queued private objective" not in json.dumps(items)
            output = [message("Active owner objective and supporting evidence summarized.")]
            assert (
                kwargs.get("item_ids") is None
            )  # One derived envelope; sources are recorded separately.
            call = saved(store, task, output, [])
            store.db.execute("UPDATE model_calls SET purpose='compaction' WHERE id=?", (call,))
            return call, {"output": output}

    context = Context(store, Summarizer())
    await context.compact("astra", task, target=2500)
    assert all("Queued private objective" not in r["text"] for r in context.selected("astra", task))
    store.status(task, "completed")
    rows = context.selected("astra", queued)
    assert sum(r["text"] == "Queued private objective must wait" for r in rows) == 1


async def test_invalid_tool_arguments_return_native_error_and_allow_next_turn(store):
    task = store.new_task("Recover from malformed provider JSON")
    runtime = Runtime(store)
    output = [
        {
            "type": "function_call",
            "call_id": "bad-json",
            "name": "write_file",
            "arguments": '{"path":',
        }
    ]
    saved(store, task, output)

    async def infer(agent, selected, instructions, items, tools, **kwargs):
        errors = [i for i in items if i.get("type") == "function_call_output"]
        assert len(errors) == 1 and "Invalid tool arguments" in errors[0]["output"]
        response = [message("The malformed call was not executed.")]
        return saved(store, task, response, kwargs["item_ids"]), {"output": response}

    runtime.models.infer = infer
    await runtime.step(task)
    assert store.one("SELECT status FROM tasks")["status"] == "completed"
    assert store.one("SELECT status FROM operations")["status"] == "failed"
    assert store.one("SELECT is_error FROM tool_results")["is_error"] == 1


async def test_owner_requests_survive_commit_before_unlink_and_conflicting_retry(
    store, monkeypatch
):
    runtime = Runtime(store)
    directory = store.config.workspace / "requests"
    raw = b'operation = "message"\nrequest_id = "logical-1"\ntext = "Exactly once"\n'
    atomic(directory / "message.toml", raw)
    from pathlib import Path

    unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        if path.parent.name == "processing":
            raise OSError("Interrupted after database commit")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted)
        with pytest.raises(OSError, match="after database commit"):
            await runtime.inbox()
    assert len(store.rows("SELECT id FROM tasks")) == 1
    await Runtime(store).inbox()
    assert not list(directory.glob("*.toml")) and not list(
        (directory / "processing").glob("*.toml")
    )
    atomic(directory / "retry.toml", raw)
    await runtime.inbox()
    atomic(directory / "conflict.toml", raw.replace(b"Exactly once", b"Conflicting instruction"))
    await runtime.inbox()
    assert len(store.rows("SELECT id FROM tasks")) == 1
    assert store.one("SELECT status FROM owner_requests")["status"] == "completed"
    conflict = store.one("SELECT detail_hash FROM events WHERE kind='owner.request_conflict'")
    assert b"Conflicting instruction" in store.bytes(
        store.get(conflict["detail_hash"])["rejected_source"]
    )


async def test_owner_request_effect_and_receipt_roll_back_together_and_preserve_order(store):
    directory = store.config.workspace / "requests"
    # Opposite lexical order; publication order controls owner intent.
    for name, number in [("z.toml", 1), ("a.toml", 2)]:
        atomic(
            directory / name,
            f'operation="message"\nrequest_id="{number}"\ntext="Message {number}"\n'.encode(),
        )
        os.utime(directory / name, ns=(number * 1_000_000_000, number * 1_000_000_000))
    runtime = Runtime(store)
    store.db.execute(
        "CREATE TRIGGER interrupt_receipt BEFORE INSERT ON owner_requests BEGIN SELECT RAISE(ABORT,'receipt unavailable'); END"
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="receipt unavailable"):
        await runtime.inbox()
    assert not store.rows("SELECT * FROM tasks")
    store.db.execute("DROP TRIGGER interrupt_receipt")
    await runtime.inbox()
    assert [r["objective"] for r in store.rows("SELECT objective FROM tasks ORDER BY rowid")] == [
        "Message 1",
        "Message 2",
    ]


async def test_malformed_owner_request_is_archived_and_does_not_block_next_request(store):
    directory = store.config.workspace / "requests"
    atomic(directory / "bad.toml", b"this is not valid TOML")
    atomic(directory / "good.toml", b'operation="message"\ntext="Proceed"')
    await Runtime(store).inbox()
    assert store.one("SELECT objective FROM tasks")["objective"] == "Proceed"
    assert {r["status"] for r in store.rows("SELECT status FROM owner_requests")} == {
        "failed",
        "completed",
    }
    assert not list(directory.glob("*.toml"))


async def test_invalid_utf8_owner_request_is_retained_as_failed_evidence(store):
    raw = b'operation="message"\ntext="\xff"'
    atomic(store.config.workspace / "requests" / "invalid.toml", raw)
    await Runtime(store).inbox()
    receipt = store.one("SELECT * FROM owner_requests")
    assert receipt["status"] == "failed" and store.bytes(receipt["source_hash"]) == raw
