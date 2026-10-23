import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


def evaluator(store):
    path = Path(__file__).resolve().parents[1] / "scripts/evaluate_parallel_evidence.py"
    spec = importlib.util.spec_from_file_location("parallel_evaluator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # A public checkout has no account/model defaults for replay to inherit.
    original = module.Config

    def configured(root):
        return original(
            root,
            {
                "profiles": store.config.data["profiles"],
                "discord": {
                    key: store.config.data["discord"][key]
                    for key in ("owner_id", "channel_id", "guild_id", "bot_id")
                },
            },
        )

    module.Config = configured
    return module


def test_replay_exact_prompt_and_ack_recovery_never_duplicate_owner_work(store, monkeypatch):
    module = evaluator(store)
    root = store.config.root
    (root / "config/local.toml").write_text("[discord]\nenabled=true\n")
    (root / "workspace/session.json").write_text('{"reset_id":"fixture-reset"}')
    prompt = root / "original.txt"
    prompt.write_text("Find the original project.\nKeep this exact newline.")
    run = root / "logs/replay"
    monkeypatch.setattr(
        module, "health", lambda root: {"healthy": True, "active_tasks": [], "release": "fixture"}
    )
    result = module.submit(root, run, prompt, 6)
    task = store.one("SELECT * FROM tasks WHERE id=?", (result["task"],))
    assert task["objective"] == prompt.read_text() and task["max_turns"] == 6
    assert (
        task["channel"] == "discord"
        and task["address"] == store.config.data["discord"]["channel_id"]
    )
    assert not store.rows("SELECT * FROM inbound_messages")
    (run / "replay.json").unlink()
    monkeypatch.setattr(
        module,
        "health",
        lambda root: {"healthy": True, "active_tasks": [task["id"]], "release": "fixture"},
    )
    assert module.submit(root, run, prompt, 6) == result
    assert json.loads((run / "replay.json").read_text()) == result
    assert store.one("SELECT count(*) n FROM tasks")["n"] == 1
    assert module.inspect(root, run)["astra_calls"] == 0
    from test_runtime_delivery import message, saved

    main_call = saved(store, task["id"], [message()])
    helper_call = saved(store, task["id"], [message()])
    store.db.execute(
        "UPDATE model_calls SET model_requested=? WHERE id=?",
        (store.config.profile("astra")["model"], main_call),
    )
    store.db.execute(
        "UPDATE model_calls SET purpose='curation',model_requested=? WHERE id=?",
        (store.config.profile("luna")["model"], helper_call),
    )
    assert module.inspect(root, run)["astra_calls"] == 1
    prompt.write_text("Changed question")
    with pytest.raises(ValueError, match="different input"):
        module.submit(root, run, prompt, 6)


def test_replay_refuses_a_nonempty_session(store, monkeypatch):
    module = evaluator(store)
    root = store.config.root
    (root / "config/local.toml").write_text("[discord]\nenabled=true\n")
    (root / "workspace/session.json").write_text('{"reset_id":"fixture-reset"}')
    prompt = root / "original.txt"
    prompt.write_text("Find the original project")
    store.new_task("Existing owner work")
    monkeypatch.setattr(
        module, "health", lambda root: {"healthy": True, "active_tasks": [], "release": "fixture"}
    )
    with pytest.raises(ValueError, match="empty conversation"):
        module.submit(root, root / "logs/replay", prompt, 6)
    assert store.one("SELECT count(*) n FROM tasks")["n"] == 1


def test_sequential_replay_requires_delivery_preserves_history_and_is_idempotent(
    store, monkeypatch
):
    from bloom.delivery import Delivery
    from bloom import turns
    from test_runtime_delivery import message

    module = evaluator(store)
    root = store.config.root
    (root / "config/local.toml").write_text("[discord]\nenabled=true\n")
    (root / "workspace/session.json").write_text('{"reset_id":"fixture-reset"}')
    monkeypatch.setattr(
        module, "health", lambda root: {"healthy": True, "active_tasks": [], "release": "fixture"}
    )
    prompt = root / "first.txt"
    prompt.write_text("First exact question")
    first = module.submit(root, root / "logs/first", prompt, 20)["task"]
    followup = root / "second.txt"
    followup.write_text("Second exact question\n")
    with pytest.raises(ValueError, match="completed predecessor"):
        module.submit(root, root / "logs/second", followup, 20, first)
    item = store.item("astra", message("First retained answer"), first)
    store.status(first, "completed")
    store.db.execute("UPDATE tasks SET final_item=? WHERE id=?", (item, first))
    turns.close(store, first, item)
    delivery = Delivery(store)
    delivery.queue(item, first, "First retained answer")
    delivery.queue_payload(
        first, "usage-fixture", {"embeds": [{"title": "Usage"}]}, kind="completion"
    )
    with pytest.raises(ValueError, match="outbox delivery"):
        module.submit(root, root / "logs/second", followup, 20, first)
    store.db.execute("UPDATE deliveries SET status='sent',external_id='fixture'")
    before = store.rows("SELECT id,envelope_hash FROM items ORDER BY id")
    second = module.submit(root, root / "logs/second", followup, 20, first)
    assert second["after_task"] == first
    assert (
        store.one("SELECT objective FROM tasks WHERE id=?", (second["task"],))["objective"]
        == followup.read_text()
    )
    assert (
        store.rows(
            "SELECT id,envelope_hash FROM items WHERE id<=? ORDER BY id", (before[-1]["id"],)
        )
        == before
    )
    assert store.one("SELECT count(*) n FROM sessions WHERE agent_id='astra'")["n"] == 1
    assert module.submit(root, root / "logs/second", followup, 20, first) == second
    assert store.one("SELECT count(*) n FROM owner_turns")["n"] == 2
    assert not store.rows("SELECT * FROM inbound_messages")
    with pytest.raises(ValueError, match="different input"):
        module.submit(root, root / "logs/second", followup, 20, "different-predecessor")


@pytest.mark.parametrize("after_rewind", [False, True])
def test_sequence_checks_all_exact_inputs_before_submitting_and_resumes_idempotently(
    store, monkeypatch, after_rewind
):
    module = evaluator(store)
    root = store.config.root
    (root / "config/local.toml").write_text("[discord]\nenabled=true\n")
    store.config.data["discord"]["enabled"] = True
    (root / "workspace/session.json").write_text('{"reset_id":"fixture-reset"}')
    monkeypatch.setattr(
        module, "health", lambda root: {"healthy": True, "active_tasks": [], "release": "fixture"}
    )
    prompts = []
    for i in range(1, 4):
        path = root / ("question-" + str(i) + ".txt")
        path.write_text("Exact question " + str(i))
        prompts.append(
            {
                "sequence": i,
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    intent = root / "intent.json"
    intent.write_text(json.dumps({"prompts": prompts}))
    real = module.inspect

    def finish(root, run):
        from bloom.delivery import Delivery
        from bloom import turns
        from test_runtime_delivery import message

        receipt = json.loads((run / "replay.json").read_text())
        task = receipt["task"]
        if store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] != "completed":
            item = store.item("astra", message("Exact retained answer"), task)
            store.status(task, "completed")
            store.db.execute("UPDATE tasks SET final_item=? WHERE id=?", (item, task))
            turns.close(store, task, item)
            Delivery(store).queue_final(task)
            store.db.execute("UPDATE deliveries SET status='sent',external_id='fixture'")
        return real(root, run)

    monkeypatch.setattr(module, "inspect", finish)
    predecessor = None
    if after_rewind:
        first_run = root / "logs/rewind"
        first = module.submit(root, first_run, Path(prompts[0]["path"]), 20)
        predecessor = first["task"]
        finish(root, first_run)
        # Rewind specs retain the original owner item but no reset_id. The
        # accepted result is the authoritative task/session mapping.
        store.db.execute(
            "UPDATE owner_requests SET source_hash=? WHERE task_id=?",
            (store.put({"item": 591, "request_id": "fixture-rewind"}), predecessor),
        )
        intent.write_text(json.dumps({"prompts": prompts, "after_task": predecessor}))
        with pytest.raises(ValueError, match="predecessor differs"):
            module.sequence(root, root / "logs/three", intent, poll_seconds=0)
        for field, value in [("reset_id", "different-reset"), ("task", "different-task")]:
            store.db.execute(
                "UPDATE owner_requests SET result_hash=? WHERE task_id=?",
                (store.put({**first, field: value}), predecessor),
            )
            with pytest.raises(ValueError, match="latest replay in this reset session"):
                module.submit(
                    root, root / "logs/invalid", Path(prompts[0]["path"]), 20, predecessor
                )
            assert store.one("SELECT count(*) n FROM tasks")["n"] == 1
        store.db.execute(
            "UPDATE owner_requests SET result_hash=? WHERE task_id=?",
            (store.put(first), predecessor),
        )
    result = module.sequence(
        root, root / "logs/three", intent, after_task=predecessor, poll_seconds=0, timeout_seconds=5
    )
    assert result["status"] == "completed" and len(result["questions"]) == 3
    before = store.rows("SELECT * FROM tasks")
    module.sequence(
        root, root / "logs/three", intent, after_task=predecessor, poll_seconds=0, timeout_seconds=5
    )
    assert store.rows("SELECT * FROM tasks") == before and len(before) == 3 + bool(after_rewind)
    Path(prompts[2]["path"]).write_text("Tampered third question")
    with pytest.raises(ValueError, match="changed after authorization"):
        module.sequence(root, root / "logs/other", intent, after_task=predecessor, poll_seconds=0)
    assert store.rows("SELECT * FROM tasks") == before
