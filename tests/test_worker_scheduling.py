import json

import pytest

from bloom.runtime import Runtime
from bloom.storage import Archive
from test_control_provenance import invoke
from test_runtime_delivery import message, saved


@pytest.mark.parametrize("kind", ["evidence", "recall"])
@pytest.mark.parametrize("arrival", ["during", "after_final", "after_delivery", "restart"])
async def test_supplementary_report_never_rebuys_finished_reply(store, kind, arrival):
    store.config.data["memory"]["retrieval_enabled"] = True
    runtime = Runtime(store)
    history = store.new_task("Earlier supporting evidence exists.")
    store.status(history, "completed")
    prior = store.new_task("Previous request with a supplementary lookup.")
    values = {"question": "Inspect earlier supporting evidence."}
    if kind == "evidence":
        values["cwd"] = str(store.config.root)
    await invoke(
        runtime,
        store,
        prior,
        "apply_request",
        {
            "path": "config/agent-presets/"
            + ("recall" if kind == "recall" else "inspect-files")
            + ".toml",
            "bindings": values,
            "required": False,
        },
        "supplementary",
    )
    child = store.one("SELECT id FROM tasks WHERE parent_id=?", (prior,))["id"]
    store.status(prior, "completed")
    task = store.new_task("A simple reply using current information.")
    calls = []

    async def infer(agent, current, instructions, items, tools, **kwargs):
        assert agent == "astra"
        calls.append(current)
        if current == task and arrival == "during":
            runtime.block(child, "Supplementary lookup has no additional source.")
        output = [message("A complete answer.")]
        return saved(store, current, output, kwargs["item_ids"]), {"output": output}

    runtime.models.infer = infer
    await runtime.step(task)
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert len(calls) == 1
    if arrival == "after_delivery":
        await runtime.delivery.flush()
    if arrival != "during":
        runtime.recall.poll()
        await runtime.evidence.poll()
        assert store.one("SELECT status FROM tasks WHERE id=?", (child,))["status"] == "ready"
        runtime.block(child, "Supplementary lookup has no additional source.")
    if arrival == "restart":
        runtime = Runtime(store)
        runtime.recover()
        runtime.models.infer = infer
    runtime.workers.poll()
    runtime.wake_waits()
    await runtime.delivery.flush()
    assert len(calls) == 1
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    launch = store.one("SELECT * FROM worker_launches")
    assert launch["report_hash"] and launch["report_item"] is None
    assert launch["report_hash"] in Archive(store).pins()
    assert len(store.rows("SELECT * FROM deliveries WHERE task_id=?", (task,))) == 1

    following = store.new_task("A naturally required next reply.")
    await runtime.step(following)
    assert calls == [task, following]
    reports = store.rows(
        "SELECT i.* FROM items i JOIN item_origins o ON o.item_id=i.id WHERE o.kind='worker_background'"
    )
    assert len(reports) == 1 and reports[0]["task_id"] == following
    assert store.one("SELECT report_item FROM worker_launches")["report_item"] == reports[0]["id"]
    runtime.workers.flush(following)
    assert len(store.rows("SELECT * FROM item_origins WHERE kind='worker_background'")) == 1


async def test_late_supplementary_item_is_not_owner_steering(store):
    runtime = Runtime(store)
    task = store.new_task("Answer once.")

    async def infer(agent, current, instructions, items, tools, **kwargs):
        store.item(
            "astra",
            {"role": "user", "content": "Late candidate evidence, not a new request."},
            task,
            origin="worker_background",
        )
        output = [message("Done.")]
        return saved(store, current, output, kwargs["item_ids"]), {"output": output}

    runtime.models.infer = infer
    await runtime.step(task)
    assert store.one("SELECT status FROM tasks")["status"] == "completed"
    assert len(store.rows("SELECT * FROM model_calls")) == 1
    assert not store.rows("SELECT * FROM item_origins WHERE kind='runtime_notice'")


async def test_required_worker_still_holds_final_and_retains_exact_request(store):
    runtime = Runtime(store)
    task = store.new_task("Required evidence before completion.")
    await invoke(
        runtime,
        store,
        task,
        "apply_request",
        {
            "path": "config/agent-presets/inspect-files.toml",
            "bindings": {"question": "Check source.", "cwd": str(store.config.root)},
        },
        "required",
    )
    child = store.one("SELECT id FROM tasks WHERE parent_id=?", (task,))["id"]
    assert runtime.workers.required(child)
    seen = []

    async def infer(agent, current, instructions, items, tools, **kwargs):
        seen.append(items)
        output = [message("Evidence considered." if len(seen) > 1 else "Premature final.")]
        return saved(store, current, output, kwargs["item_ids"]), {"output": output}

    runtime.models.infer = infer
    await runtime.step(task)
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "waiting"
    assert not store.rows(
        "SELECT * FROM deliveries WHERE task_id=? AND body='Premature final.'", (task,)
    )
    runtime.block(child, "Required source unavailable; preserved receipt.")
    runtime.wake_waits()
    await runtime.step(task)
    assert "Required source unavailable" in json.dumps(seen[-1])
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert [
        r["body"]
        for r in store.rows(
            "SELECT body FROM deliveries WHERE task_id=? AND item_id=(SELECT final_item FROM tasks WHERE id=?)",
            (task, task),
        )
    ] == ["Evidence considered."]
