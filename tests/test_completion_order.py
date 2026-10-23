"""Terminal delivery uses the real durable outbox and Discord HTTP dispatcher."""

import json

import httpx
import pytest

from bloom import turns
from bloom.delivery import Delivery, Discord, chunks
from bloom.gateway import Gateway
from bloom.store import now
from bloom.runtime import Runtime
from test_runtime_delivery import message, saved
from test_usage_breakdown import record_call


@pytest.fixture
def dispatch(store, monkeypatch):
    store.config.data["discord"]["enabled"] = True
    store.config.data["discord"]["activity_channel_id"] = ""
    monkeypatch.setenv("BLOOM_DISCORD_TOKEN", "fixture-completion-order")
    sent, attempts = [], []
    fault = {"card": None, "text": None}

    def handler(request):
        body = json.loads(request.content) if request.content else {}
        if "nonce" not in body:
            return httpx.Response(200, json={"id": "cosmetic"})
        attempts.append(body)
        kind = "card" if body.get("embeds") else "text"
        failure = fault[kind]
        if failure:
            if isinstance(failure, int):
                return httpx.Response(
                    failure, json={"message": "fixture rejection", "retry_after": 1}
                )
            raise failure
        sent.append(body)
        return httpx.Response(200, json={"id": "sent-" + str(len(sent))})

    delivery = Delivery(store, Discord(store, httpx.MockTransport(handler)))
    return delivery, sent, attempts, fault


async def complete(store, runtime, task, text="The verified answer", phase="final_answer"):
    output = [message(text, phase)]
    call = saved(store, task, output)
    runtime.ingest_response(task, call, {"output": output})
    await runtime.finish(task)
    return store.one("SELECT final_item FROM tasks WHERE id=?", (task,))["final_item"]


def card_row(store):
    return store.one("SELECT * FROM deliveries WHERE kind='completion' ORDER BY rowid DESC")


def final_rows(store, final):
    return store.rows(
        "SELECT * FROM deliveries WHERE item_id=? AND kind='message' ORDER BY part", (final,)
    )


async def test_tool_progress_then_existing_mention_card_then_all_final_chunks(store, dispatch):
    delivery, sent, _, _ = dispatch
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("Inspect the source", channel="discord", address="main")
    update = {
        "observed": "The source is saved.",
        "next": "Inspect its contents.",
        "why": "Verify the answer.",
    }
    output = [
        {
            "type": "function_call",
            "call_id": "read",
            "name": "read_file",
            "arguments": json.dumps({"path": "README.md", "progress": update}),
        },
        message("Native tool-round prose", "final_answer"),
    ]
    call = saved(store, task, output)
    runtime.ingest_response(task, call, {"output": output})
    await delivery.flush()  # Progress is immediate, before terminal finalization.
    expected_progress = "Tools: `read_file`\n\n**Observed:** The source is saved.\n**Next:** Inspect its contents.\n**Why:** Verify the answer."
    assert [b["content"] for b in sent] == [expected_progress]
    assert not card_row(store)
    text = "Intro\n\n```python\n" + 'print("verified")\n' * 230 + "```\nAnswer"
    final = await complete(store, runtime, task, text)
    card = card_row(store)
    frozen = delivery.payload(card)
    await delivery.flush()
    assert sent[1]["embeds"] == frozen["body"]["embeds"]
    assert sent[1]["content"] == "<@" + delivery.discord.settings["owner_id"] + ">"
    assert sent[1]["allowed_mentions"]["users"] == [delivery.discord.settings["owner_id"]]
    assert [b["content"] for b in sent[2:]] == [part.rstrip("\n") for part in chunks(text)]
    assert len(final_rows(store, final)) > 1
    assert all(not b["allowed_mentions"].get("users") for b in sent[2:])
    assert store.one("SELECT phase FROM items WHERE id=?", (final,))["phase"] == "final_answer"
    assert (
        store.get(
            store.one("SELECT envelope_hash FROM items WHERE source_key=?", (call + ":1",))[
                "envelope_hash"
            ]
        )
        == output[1]
    )
    await runtime.finish(task)
    await delivery.flush()
    assert len(sent) == 2 + len(chunks(text))  # No duplicate card/prose.
    assert delivery.payload(card_row(store)) == frozen


@pytest.mark.parametrize("failure", [httpx.ConnectError("unavailable"), 429, 403])
async def test_card_failure_holds_every_final_chunk_until_saved_card_retry(
    store, dispatch, failure
):
    delivery, sent, attempts, fault = dispatch
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("A durable answer", channel="discord", address="main")
    text = "Retained answer.\n\n" * 220
    final = await complete(store, runtime, task, text)
    fault["card"] = failure
    await delivery.flush()
    card = card_row(store)
    assert card["status"] == ("failed" if failure == 403 else "prepared")
    assert not sent and len(attempts) == 1
    assert all(r["status"] == "prepared" for r in final_rows(store, final))
    await delivery.flush()
    assert len(attempts) == 1  # Backoff / definite rejection is not blindly replayed.
    fault["card"] = None
    if failure == 403:
        delivery.retry_failed(card["id"])  # Supported operator action, same ID/nonce.
    store.db.execute("UPDATE delivery_retries SET retry_at=0")
    await delivery.flush()
    assert attempts[0]["nonce"] == attempts[1]["nonce"]
    assert sent[0]["embeds"]
    assert [b["content"] for b in sent[1:]] == [part.rstrip("\n") for part in chunks(text)]
    await delivery.flush()
    assert len(attempts) == 2 + len(chunks(text))


@pytest.mark.parametrize("interruption", ["timeout", "sending", "server-error"])
async def test_restart_retains_ambiguous_card_barrier_until_matching_ack(
    store, dispatch, interruption
):
    delivery, sent, attempts, fault = dispatch
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("Recover without replay", channel="discord", address="main")
    final = await complete(store, runtime, task)
    if interruption == "sending":
        store.db.execute("UPDATE deliveries SET status='sending' WHERE kind='completion'")
    else:
        fault["card"] = 503 if interruption == "server-error" else httpx.ReadTimeout("ack lost")
        await delivery.flush()
    card = card_row(store)
    frozen = delivery.payload(card)
    # Reopen the fixture SQLite connection, not the canonical runtime database.
    store.db.close()
    store._initialize()
    recovered = Delivery(store, delivery.discord)
    recovered.discord.store = store
    runtime = Runtime(store, delivery=recovered)
    runtime.recover()
    assert card_row(store)["status"] == "uncertain"
    await runtime.finish(task)  # Repeated finalization must reuse group/identities.
    await recovered.flush()
    assert not sent
    assert len(attempts) == (0 if interruption == "sending" else 1)
    assert recovered.payload(card) == frozen
    ack = dict(
        frozen["body"],
        id="remote-card",
        channel_id="main",
        nonce=card["nonce"],
        content="<@" + delivery.discord.settings["owner_id"] + ">",
        author={"id": delivery.discord.bot_id},
    )
    wrong = dict(ack, embeds=[{"title": "wrong card"}])
    recovered.acknowledge(wrong)
    await recovered.flush()
    assert not sent and card_row(store)["status"] == "uncertain"
    recovered.acknowledge(ack)
    await recovered.flush()
    assert [b["content"] for b in sent] == ["The verified answer"]
    assert len(store.rows("SELECT * FROM deliveries WHERE kind='completion'")) == 1
    assert all(r["status"] == "sent" for r in final_rows(store, final))
    await recovered.flush()
    assert len(sent) == 1  # Never re-POST the acknowledged card.


async def test_final_chunk_uncertainty_holds_remaining_chunks_without_replaying_card(
    store, dispatch
):
    delivery, sent, _, fault = dispatch
    task = store.new_task("Chunk recovery", channel="discord", address="main")
    runtime = Runtime(store, delivery=delivery)
    text = "Evidence paragraph.\n\n" * 200
    final = await complete(store, runtime, task, text)
    fault["text"] = httpx.ReadTimeout("first chunk ack lost")
    await delivery.flush()
    rows = final_rows(store, final)
    assert len(sent) == 1 and sent[0]["embeds"]
    assert rows[0]["status"] == "uncertain"
    assert all(r["status"] == "prepared" for r in rows[1:])
    runtime.recover()
    await runtime.finish(task)
    fault["text"] = None
    delivery.acknowledge(
        {
            "id": "first-chunk",
            "channel_id": "main",
            "nonce": rows[0]["nonce"],
            "content": rows[0]["body"],
            "author": {"id": delivery.discord.bot_id},
        }
    )
    await delivery.flush()
    assert [b["content"] for b in sent[1:]] == [part.rstrip("\n") for part in chunks(text)[1:]]
    assert all(r["status"] == "sent" for r in final_rows(store, final))


async def test_failed_earlier_progress_holds_card_but_new_progress_is_immediate(store, dispatch):
    delivery, sent, _, fault = dispatch
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("Snapshot boundaries", channel="discord", address="main")
    item = store.item("astra", message("Earlier progress", "commentary"), task)
    delivery.queue(item, task, "Earlier progress")
    fault["text"] = httpx.ReadTimeout("progress ack lost")
    await delivery.flush()
    await complete(store, runtime, task)
    fault["text"] = None
    await delivery.flush()
    assert not sent
    later = store.item("astra", message("New immediate progress", "commentary"), task)
    delivery.queue(later, task, "New immediate progress")
    await delivery.flush()
    assert [b["content"] for b in sent] == ["New immediate progress"]
    progress = store.one("SELECT * FROM deliveries WHERE item_id=?", (item,))
    delivery.acknowledge(
        {
            "id": "progress",
            "channel_id": "main",
            "nonce": progress["nonce"],
            "content": progress["body"],
            "author": {"id": delivery.discord.bot_id},
        }
    )
    await delivery.flush()
    assert sent[1]["embeds"] and sent[2]["content"] == "The verified answer"


async def test_multiple_owner_turns_usage_attribution_and_later_answer_do_not_cycle(
    store, dispatch
):
    delivery, sent, _, fault = dispatch
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("First question", channel="discord", address="main")
    first_turn = turns.current(store, task)
    record_call(store, task, "astra", (100, 80, 10))
    record_call(store, task, "luna", (None, None, None))
    worker = await runtime.request(
        "astra", task, "worker", {"operation": "delegate", "role": "sol", "objective": "Inspect"}
    )
    record_call(store, worker["task_id"], "sol", (50, 40, 8))
    store.status(worker["task_id"], "completed")
    await delivery.flush()  # Existing worker-thread announcement is not a final answer.
    sent.clear()
    runtime.steer(task, "Second question")  # Actual owner steering, not a parent-to-worker control.
    second_turn = turns.current(store, task)
    record_call(store, task, "astra", (200, 150, 20))
    final = await complete(store, runtime, task, "Answer to both")
    assert store.rows("SELECT DISTINCT reply_item FROM owner_turns") == [{"reply_item": final}]
    fault["card"] = httpx.ConnectError("retry later")
    await delivery.flush()
    assert not sent  # Second card/mention cannot overtake first card.
    # Accept another owner turn while the first answer's card is in backoff.
    store.item("astra", {"role": "user", "content": "Third question"}, task, origin="owner")
    store.status(task, "ready")
    third = await complete(store, runtime, task, "Third answer")
    fault["card"] = None
    store.db.execute("UPDATE delivery_retries SET retry_at=0")
    await delivery.flush()
    assert [bool(b.get("embeds")) for b in sent] == [True, True, False, True, False]
    assert [b["content"] for b in sent if not b.get("embeds")] == ["Answer to both", "Third answer"]
    cards = [b for b in sent if b.get("embeds")]
    assert [b["embeds"][0]["description"] for b in cards] == [
        "First question",
        "Owner/parent steering:\nSecond question",
        "Third question",
    ]
    assert "content" not in cards[0] and "<@" in cards[1]["content"]
    fields = {f["name"]: f["value"] for f in cards[0]["embeds"][0]["fields"]}
    assert "**80** cached input tokens" in fields["Astra"]
    assert "**8** output tokens" in fields["Sol"]
    assert "Unreported" in fields["Luna"] and "Incomplete usage: 1 calls" in fields["Luna"]
    assert turns.usage(store, first_turn)["calls"] == 3
    assert turns.usage(store, second_turn)["calls"] == 2
    assert (
        store.one("SELECT reply_item FROM owner_turns ORDER BY source_item DESC")["reply_item"]
        == third
    )


@pytest.mark.parametrize("status", ["completed", "blocked", "cancelled"])
async def test_explicit_terminal_outcomes_keep_card_before_native_or_fallback_final(
    store, dispatch, status
):
    delivery, sent, _, _ = dispatch
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("Outcome", channel="discord", address="main")
    await runtime.request(
        "astra",
        task,
        "outcome",
        {"operation": "outcome", "status": status, "reason": "Fixture outcome"},
    )
    final = await complete(store, runtime, task, phase=None)  # Phase-less provider fallback.
    await delivery.flush()
    assert sent[0]["embeds"][0]["title"] == status.title() + " · Turn usage"
    assert sent[1]["content"] == "The verified answer"
    assert store.one("SELECT phase FROM items WHERE id=?", (final,))["phase"] is None


@pytest.mark.parametrize("status", ["cancelled", "paused"])
async def test_owner_stop_before_finish_does_not_publish_a_terminal_group(store, dispatch, status):
    delivery, sent, _, _ = dispatch
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("Stopped", channel="discord", address="main")
    call = saved(store, task, [message()])
    runtime.ingest_response(task, call, {"output": [message()]})
    store.status(task, status)
    await runtime.finish(task)
    await delivery.flush()
    assert not sent and not store.rows("SELECT * FROM deliveries")
    assert store.one("SELECT replied_at FROM owner_turns")["replied_at"] is None


async def test_terminal_publication_rolls_back_as_one_group_and_can_recover(
    store, dispatch, monkeypatch
):
    delivery, sent, _, _ = dispatch
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("Atomic completion", channel="discord", address="main")
    call = saved(store, task, [message()])
    runtime.ingest_response(task, call, {"output": [message()]})
    original = delivery.queue

    def interrupted(*args, **kwargs):
        raise RuntimeError("fixture interrupted after card creation")

    monkeypatch.setattr(delivery, "queue", interrupted)
    with pytest.raises(RuntimeError, match="fixture interrupted"):
        await runtime.finish(task)
    assert store.one("SELECT status,final_item FROM tasks") == {
        "status": "ready",
        "final_item": None,
    }
    assert store.one("SELECT replied_at FROM owner_turns")["replied_at"] is None
    assert not store.rows("SELECT * FROM deliveries")
    assert not store.rows("SELECT * FROM delivery_ordering")
    monkeypatch.setattr(delivery, "queue", original)
    runtime.recover()
    await runtime.step(task)  # Saved final is reused, no model request.
    await delivery.flush()
    assert sent[0]["embeds"] and sent[1]["content"] == "done"
    assert len(store.rows("SELECT * FROM model_calls")) == 1


async def test_legacy_sent_or_uncertain_finals_are_never_reordered_or_replayed(store, dispatch):
    delivery, sent, attempts, _ = dispatch
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("Legacy answer", channel="discord", address="main")
    call = saved(store, task, [message("Historical answer")])
    runtime.ingest_response(task, call, {"output": [message("Historical answer")]})
    final = store.one("SELECT id FROM items WHERE role='assistant'")["id"]
    delivery.queue(final, task, "Historical answer")  # Pre-change outbox row.
    store.db.execute("UPDATE deliveries SET status='uncertain'")
    await runtime.finish(task)
    await delivery.flush()
    assert not attempts
    assert not store.rows("SELECT * FROM delivery_ordering")
    legacy = final_rows(store, final)[0]
    delivery.acknowledge(
        {
            "id": "old-final",
            "channel_id": "main",
            "nonce": legacy["nonce"],
            "content": legacy["body"],
            "author": {"id": delivery.discord.bot_id},
        }
    )
    await runtime.finish(task)
    await delivery.flush()
    assert len(sent) == 1 and sent[0]["embeds"]  # Only the unsent legacy card.
    await runtime.finish(task)
    await delivery.flush()
    assert len(attempts) == 1
    assert final_rows(store, final)[0]["external_id"] == "old-final"


async def test_cosmetic_ambiguous_post_is_not_repeated_inside_terminal_group(
    store, dispatch, monkeypatch
):
    delivery, sent, _, _ = dispatch
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("No tools needed", channel="discord", address="main")
    await complete(store, runtime, task)
    original = delivery.discord.request
    visible = []

    async def request(method, path, body=None, **kwargs):
        if body and "nonce" not in body:
            visible.append("cosmetic accepted")
            raise httpx.ReadTimeout("cosmetic acknowledgement lost")
        visible.append("card" if body.get("embeds") else "final")
        return await original(method, path, body, **kwargs)

    monkeypatch.setattr(delivery.discord, "request", request)
    await delivery.flush()
    runtime.recover()
    await runtime.finish(task)
    await delivery.flush()
    assert visible == ["cosmetic accepted", "card", "final"]
    assert len(sent) == 2
    assert store.one("SELECT 1 FROM events WHERE kind='activity.error'")


async def test_retry_action_refuses_uncertain_sending_sent_or_unknown_deliveries(store, dispatch):
    delivery, _, _, _ = dispatch
    task = store.new_task("Safe retry only", channel="discord", address="main")
    runtime = Runtime(store, delivery=delivery)
    await complete(store, runtime, task)
    card = card_row(store)
    for status in ("prepared", "sending", "uncertain", "sent"):
        store.db.execute("UPDATE deliveries SET status=? WHERE id=?", (status, card["id"]))
        with pytest.raises(ValueError, match="definitely failed"):
            delivery.retry_failed(card["id"])
        assert card_row(store)["status"] == status
    with pytest.raises(ValueError, match="definitely failed"):
        delivery.retry_failed("unknown")
    assert not store.rows("SELECT * FROM events WHERE kind='delivery.retry_requested'")


@pytest.mark.parametrize("route", ["gateway-catchup", "readback"])
async def test_real_recovery_routes_release_card_barrier_without_reposting(
    store, dispatch, monkeypatch, route
):
    delivery, sent, attempts, fault = dispatch
    delivery.discord.settings["channel_id"] = "main"
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("Recover through ingress", channel="discord", address="main")
    await complete(store, runtime, task)
    fault["card"] = httpx.ReadTimeout("card acknowledgement lost")
    await delivery.flush()
    card = card_row(store)
    remote = dict(
        delivery.payload(card)["body"],
        id="12",
        channel_id="main",
        nonce=card["nonce"],
        content="<@" + delivery.discord.settings["owner_id"] + ">",
        author={"id": delivery.discord.bot_id},
    )
    original = delivery.discord.request
    reads = []

    async def request(method, path, body=None, **kwargs):
        if method == "GET":
            reads.append(path)
            return [remote] if route == "gateway-catchup" else remote
        return await original(method, path, body, **kwargs)

    monkeypatch.setattr(delivery.discord, "request", request)
    runtime.recover()
    if route == "gateway-catchup":
        store.db.execute("INSERT INTO discord_cursors VALUES(?,?,?)", ("main", "10", now()))
        gateway = Gateway(store, delivery.discord, runtime.receive_discord, catch_up=True)
        await gateway.reconcile_history()
        assert store.one("SELECT message_id FROM discord_cursors")["message_id"] == "12"
    else:
        await delivery.reconcile(card["id"], "12")
    await delivery.flush()
    assert reads and card_row(store)["external_id"] == "12"
    assert [b["content"] for b in sent] == ["The verified answer"]
    assert len(attempts) == 2  # One ambiguous card attempt, one final. No card replay.
    assert len(store.rows("SELECT * FROM tasks")) == 1


async def test_paused_terminal_group_waits_then_resumes_in_order(store, dispatch):
    delivery, sent, _, _ = dispatch
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("Pause delivery", channel="discord", address="main")
    await complete(store, runtime, task)
    store.status(task, "paused")
    await delivery.flush()
    assert not sent
    runtime.resume(task)
    await delivery.flush()
    assert sent[0]["embeds"] and sent[1]["content"] == "The verified answer"


async def test_delayed_worker_thread_group_keeps_order_and_does_not_replay_after_routing(
    store, dispatch, monkeypatch
):
    delivery, sent, _, _ = dispatch
    runtime = Runtime(store, delivery=delivery)
    parent = store.new_task("Inspect evidence", channel="discord", address="main")
    worker = await runtime.request(
        "astra", parent, "worker", {"operation": "delegate", "role": "sol", "objective": "Inspect"}
    )
    task = worker["task_id"]
    call = saved(store, task, [message("Worker report")])
    runtime.ingest_response(task, call, {"output": [message("Worker report")]})
    await runtime.finish(task)
    assert store.one("SELECT address FROM tasks WHERE id=?", (task,))["address"] == ""
    assert not sent
    original = delivery.discord.request
    posts = []

    async def request(method, path, body=None, **kwargs):
        posts.append((path, body or {}))
        return await original(method, path, body, **kwargs)

    monkeypatch.setattr(delivery.discord, "request", request)
    await delivery.flush()
    worker_posts = [
        body for path, body in posts if path == "/channels/cosmetic/messages" and "nonce" in body
    ]
    assert worker_posts[0]["embeds"] and worker_posts[1]["content"] == "Worker report"
    assert all(not b["allowed_mentions"].get("users") for b in worker_posts)
    before = len(sent)
    runtime.recover()
    await runtime.finish(task)
    await delivery.flush()
    assert len(sent) == before  # Route address is not a fresh delivery identity.
    assert (
        len(store.rows("SELECT * FROM deliveries WHERE task_id=? AND kind='completion'", (task,)))
        == 1
    )


async def test_legacy_card_does_not_deadlock_with_a_later_new_ordered_group(store, dispatch):
    delivery, sent, _, _ = dispatch
    runtime = Runtime(store, delivery=delivery)
    task = store.new_task("Legacy question", channel="discord", address="main")
    call = saved(store, task, [message("Already delivered")])
    runtime.ingest_response(task, call, {"output": [message("Already delivered")]})
    final = store.one("SELECT id FROM items WHERE role='assistant'")["id"]
    delivery.queue(final, task, "Already delivered")
    store.db.execute("UPDATE deliveries SET status='sent',external_id='legacy-final'")
    store.db.execute("UPDATE tasks SET final_item=? WHERE id=?", (final, task))
    turns.close(store, task, final)
    delivery.usage_cards(store.one("SELECT * FROM tasks WHERE id=?", (task,)))
    legacy = card_row(store)
    assert not store.one("SELECT 1 FROM delivery_ordering WHERE delivery_id=?", (legacy["id"],))
    store.item("astra", {"role": "user", "content": "New question"}, task, origin="owner")
    await complete(store, runtime, task, "New answer")
    await delivery.flush()
    assert [bool(b.get("embeds")) for b in sent] == [True, True, False]
    assert sent[-1]["content"] == "New answer"


async def test_error_attention_and_no_prose_completion_fallback_remain_single_delivery(
    store, dispatch
):
    delivery, sent, _, _ = dispatch
    runtime = Runtime(store, delivery=delivery)
    blocked = store.new_task("Runtime error", channel="discord", address="main")
    runtime.block(blocked, "Fixture error")
    runtime.notify_parent(blocked)
    await delivery.flush()
    assert len(sent) == 1 and sent[0]["content"].startswith("Attention needed: Fixture error")
    assert not card_row(store)
    fallback = store.new_task("No final prose available", channel="discord", address="main")
    await runtime.finish(fallback)
    await delivery.flush()
    assert len(sent) == 2 and sent[1]["embeds"]
    await runtime.finish(fallback)
    await delivery.flush()
    assert len(sent) == 2
