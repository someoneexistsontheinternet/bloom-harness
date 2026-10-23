import asyncio
import hashlib
import json

import jsonschema
import pytest

from bloom import turns
from bloom.presets import expand
from bloom.runtime import Runtime
from bloom.store import Store
from test_runtime_delivery import message, saved
from test_control_provenance import invoke


def wire(runtime, name, values, ident):
    schema = next(t for t in runtime.tools.schemas("astra") if t["name"] == name)
    args = {key: values.get(key) for key in schema["parameters"]["properties"]}
    if name == "apply_request" and args["bindings"] is not None:
        args["bindings"] = {
            k: args["bindings"].get(k) for k in ("question", "cwd", "sources", "criteria")
        }
    args["progress"] = {
        "observed": "Independent evidence is needed.",
        "next": "Launch workers and join their results.",
        "why": "Use one decision for useful parallel work.",
    }
    jsonschema.validate(args, schema["parameters"])
    return {"type": "function_call", "call_id": ident, "name": name, "arguments": json.dumps(args)}


async def test_first_astra_batch_launches_prefab_workers_and_joins_without_polling(store):
    store.config.data["memory"]["retrieval_enabled"] = True
    old = store.new_task("The historical marker is violet-739.")
    store.status(old, "completed")
    source = store.one("SELECT id FROM items WHERE task_id=?", (old,))["id"]
    for name in ("left", "right"):
        directory = store.config.root / name
        directory.mkdir()
        (directory / "source.txt").write_text(name + "-verified")
    runtime = Runtime(store)
    task = store.new_task("Compare current files with the historical marker.")
    counts, entered = {}, set()
    overlap = asyncio.Event()

    async def infer(agent, current, instructions, items, tools, **kwargs):
        counts[agent] = counts.get(agent, 0) + 1
        if agent == "astra" and counts[agent] == 1:
            assert not store.rows("SELECT * FROM memory_retrievals")  # Astra speaks first.
            output = [
                wire(
                    runtime,
                    "apply_request",
                    {
                        "path": "config/agent-presets/inspect-files.toml",
                        "bindings": {
                            "question": "Read the source marker.",
                            "cwd": str(store.config.root / name),
                            "sources": ["source.txt"],
                        },
                    },
                    name,
                )
                for name in ("left", "right")
            ]
            output += [
                wire(
                    runtime,
                    "apply_request",
                    {
                        "path": "config/agent-presets/recall.toml",
                        "bindings": {"question": "What was the historical marker?"},
                    },
                    "recall",
                ),
                wire(runtime, "await_workers", {}, "join"),
            ]
        elif agent == "astra":
            assert counts[agent] == 2
            reports = [
                json.loads(i["content"])
                for i in items
                if i.get("role") == "user"
                and isinstance(i.get("content"), str)
                and '"kind": "' in i["content"]
            ]
            assert sum(r.get("kind") == "worker_evidence_report" for r in reports) == 2
            assert sum(r.get("kind") == "memory_retrieval_report" for r in reports) == 1
            for marker in ("left-verified", "right-verified", "violet-739"):
                assert marker in json.dumps(reports)
            output = [message("Verified the three independent sources.")]
        elif runtime.recall.job(current):
            if counts[agent] == 1:
                assert {t["name"] for t in tools} == {"memory_search", "memory_read"}
                output = [
                    {
                        "type": "function_call",
                        "call_id": "read",
                        "name": "memory_read",
                        "arguments": json.dumps({"node_id": "event:" + str(source)}),
                    }
                ]
            else:
                output = [
                    message(
                        json.dumps(
                            {
                                "status": "found",
                                "summary": "The old marker was violet-739.",
                                "sources": [
                                    {"node_id": "event:" + str(source), "quote": "violet-739"}
                                ],
                            }
                        )
                    )
                ]
        elif counts[agent] == 1:
            entered.add(agent)
            if len(entered) == 2:
                overlap.set()
            await asyncio.wait_for(overlap.wait(), 3)
            assert any("recent_public_summaries" in json.dumps(i) for i in items)
            output = [
                {
                    "type": "function_call",
                    "call_id": "read",
                    "name": "read_file",
                    "arguments": '{"path":"source.txt"}',
                }
            ]
        else:
            receipt = store.one(
                "SELECT id,result_hash FROM operations WHERE task_id=? AND name='read_file'",
                (current,),
            )
            output = [
                message(
                    json.dumps(
                        {
                            "status": "found",
                            "summary": "Verified the source marker.",
                            "evidence": [
                                {
                                    "operation_id": receipt["id"],
                                    "quote": store.get(receipt["result_hash"])["text"],
                                }
                            ],
                        }
                    )
                )
            ]
        call = saved(store, current, output, kwargs["item_ids"])
        store.db.execute(
            "UPDATE model_calls SET request_hash=? WHERE id=?", (store.put({"tools": tools}), call)
        )
        return call, {"output": output}

    runtime.models.infer = infer
    await asyncio.wait_for(runtime.run(until_idle=True), 10)
    assert counts["astra"] == 2 and len(entered) == 2
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    assert len(store.rows("SELECT * FROM preset_invocations")) == 3
    assert len(json.loads(store.one("SELECT workers_json FROM worker_awaits")["workers_json"])) == 3
    assert not store.rows("SELECT * FROM channel_routes")
    assert not store.rows("SELECT * FROM deliveries WHERE task_id!=?", (task,))
    assert not store.rows(
        "SELECT c.id FROM tool_calls c LEFT JOIN tool_results r ON r.call_id=c.id WHERE r.call_id IS NULL"
    )


async def test_no_automatic_recall_even_when_retrieval_enabled(store):
    store.config.data["memory"]["retrieval_enabled"] = True
    runtime = Runtime(store)
    task = store.new_task("Thanks!")

    async def infer(agent, current, instructions, items, tools, **kwargs):
        assert agent == "astra"
        output = [message("You are welcome.")]
        call = saved(store, current, output, kwargs["item_ids"])
        return call, {"output": output}

    runtime.models.infer = infer
    await runtime.run(until_idle=True)
    assert not store.rows("SELECT * FROM memory_retrievals")
    assert len(store.rows("SELECT * FROM model_calls")) == 1


async def test_presets_snapshot_bindings_and_reuse_accepted_launch_after_source_changes(store):
    runtime = Runtime(store)
    task = store.new_task("Inspect a fixture")
    args = {
        "path": "config/agent-presets/inspect-files.toml",
        "bindings": {
            "question": 'Literal question\nnetwork = true\nmode = "full"',
            "cwd": str(store.config.root),
        },
    }
    operation = await invoke(runtime, store, task, "apply_request", args, "launch")
    assert operation["status"] == "completed"
    receipt = store.one("SELECT * FROM preset_invocations")
    assert store.get(receipt["bindings_hash"]) == args["bindings"]
    children = store.rows("SELECT * FROM tasks WHERE parent_id=?", (task,))
    assert len(children) == 1
    agent = store.one("SELECT mode,network FROM agents WHERE id=?", (children[0]["agent_id"],))
    assert agent == {"mode": "read", "network": 0}
    (store.config.source / args["path"]).write_text("deliberately no longer valid TOML")
    repeated = await runtime.tools.run("astra", task, operation["id"], "apply_request", args)
    assert repeated["launched"] == [children[0]["id"]]
    assert store.rows("SELECT * FROM tasks WHERE parent_id=?", (task,)) == children
    with pytest.raises(ValueError, match="identity changed"):
        await runtime.tools.run(
            "astra",
            task,
            operation["id"],
            "apply_request",
            args | {"bindings": {"question": "different"}},
        )


async def test_custom_preset_needs_exact_source_and_can_batch_write_apply(store):
    runtime = Runtime(store)
    task = store.new_task("Custom preset")
    text = (store.config.source / "config/agent-presets/inspect-files.toml").read_text()
    path = store.config.root / "custom.toml"
    path.write_text(text)
    args = {
        "path": str(path),
        "bindings": {"question": "Inspect local data", "cwd": str(store.config.root)},
    }
    failed = await invoke(runtime, store, task, "apply_request", args, "unread")
    assert failed["status"] == "failed" and not store.rows("SELECT * FROM worker_launches")
    applied = await invoke(
        runtime,
        store,
        task,
        "apply_request",
        args | {"expected_sha256": hashlib.sha256(text.encode()).hexdigest()},
        "verified",
    )
    assert applied["status"] == "completed"
    unknown = await invoke(
        runtime,
        store,
        task,
        "apply_request",
        args | {"bindings": args["bindings"] | {"network": True}},
        "invalid",
    )
    assert unknown["status"] == "failed"


async def test_multiple_explicit_recall_assignments_share_owner_without_hidden_model_calls(store):
    store.config.data["memory"]["retrieval_enabled"] = True
    runtime = Runtime(store)
    task = store.new_task("Recall two independent decisions")
    for i in range(2):
        result = await invoke(
            runtime,
            store,
            task,
            "apply_request",
            {
                "path": "config/agent-presets/recall.toml",
                "bindings": {"question": "Find decision " + str(i)},
            },
            "recall-" + str(i),
        )
        assert result["status"] == "completed"
    jobs = store.rows("SELECT * FROM memory_retrievals")
    assert len(jobs) == 2 and jobs[0]["owner_item"] == jobs[1]["owner_item"]
    assert all(r["purpose"] == "turn" for r in store.rows("SELECT purpose FROM model_calls"))
    assert all(r["agent_id"] == "astra" for r in store.rows("SELECT agent_id FROM model_calls"))


async def test_explicit_join_restart_keeps_workers_and_needs_no_collection_inference(store):
    runtime = Runtime(store)
    task = store.new_task("Durable join")
    launched = await invoke(
        runtime,
        store,
        task,
        "apply_request",
        {
            "path": "config/agent-presets/inspect-files.toml",
            "bindings": {"question": "Inspect", "cwd": str(store.config.root)},
        },
        "launch",
    )
    waiting = await invoke(runtime, store, task, "await_workers", {}, "join")
    assert waiting["status"] == "running"
    child = store.one("SELECT id FROM tasks WHERE parent_id=?", (task,))["id"]
    runtime.block(child, "Fixture source unavailable")
    recovered = Runtime(store)
    recovered.recover()
    recovered.workers.poll()

    async def forbidden(*a, **k):
        raise AssertionError("Joining saved workers must not buy inference")

    recovered.models.infer = forbidden
    await recovered.step(task)
    assert (
        store.one("SELECT status FROM operations WHERE id=?", (waiting["id"],))["status"]
        == "completed"
    )
    assert len(store.rows("SELECT * FROM worker_launches")) == 1
    assert len(store.rows("SELECT * FROM tasks WHERE parent_id=?", (task,))) == 1


async def test_legacy_owner_uniqueness_migration_preserves_retrieval_rows(store):
    store.config.data["memory"]["retrieval_enabled"] = True
    runtime = Runtime(store)
    task = store.new_task("Recall")
    child = runtime.recall.start(task, "first", "Question", "Inspect historical sources")
    old = store.rows("SELECT * FROM memory_retrievals")
    sql = store.one("SELECT sql FROM sqlite_master WHERE name='memory_retrievals'")["sql"]
    with store.transaction():
        store.db.execute(
            sql.replace("memory_retrievals(", "legacy_retrievals(", 1).replace(
                "owner_item INTEGER NOT NULL REFERENCES",
                "owner_item INTEGER NOT NULL UNIQUE REFERENCES",
            )
        )
        store.db.execute("INSERT INTO legacy_retrievals SELECT * FROM memory_retrievals")
        store.db.execute("DROP TABLE memory_retrievals")
        store.db.execute("ALTER TABLE legacy_retrievals RENAME TO memory_retrievals")
    store.db.close()
    store._initialize()
    assert store.rows("SELECT * FROM memory_retrievals") == old
    other = Runtime(store).recall.start(
        task, "second", "Other question", "Inspect another historical source"
    )
    assert child != other and len(store.rows("SELECT * FROM memory_retrievals")) == 2
    assert not store.rows("PRAGMA foreign_key_check")


async def test_captured_preset_interruption_recovers_exact_intent_without_relaunch(store):
    runtime = Runtime(store)
    task = store.new_task("Recover captured local intent")
    original = runtime.tools.request_handler

    async def interrupted(*a, **k):
        raise asyncio.CancelledError()

    runtime.tools.request_handler = interrupted
    args = {
        "path": "config/agent-presets/inspect-files.toml",
        "bindings": {"question": "Inspect", "cwd": str(store.config.root)},
    }
    with pytest.raises(asyncio.CancelledError):
        await invoke(runtime, store, task, "apply_request", args, "interrupted")
    assert store.one("SELECT status FROM operations")["status"] == "prepared"
    assert store.one("SELECT * FROM preset_invocations") and not store.rows(
        "SELECT * FROM worker_launches"
    )
    runtime.tools.request_handler = original
    recovered = Runtime(store)
    recovered.recover()

    async def forbidden(*a, **k):
        raise AssertionError("Receipt recovery must not buy parent inference")

    recovered.models.infer = forbidden
    await recovered.step(task)
    assert len(store.rows("SELECT * FROM worker_launches")) == 1
    assert len(store.rows("SELECT * FROM tasks WHERE parent_id=?", (task,))) == 1
    assert store.one("SELECT status FROM operations")["status"] == "completed"
    # Hard restart after worker/control commit but before native result delivery.
    store.db.execute("UPDATE operations SET status='running'")
    Runtime(store).recover()
    assert store.one("SELECT status FROM operations")["status"] == "completed"
    assert len(store.rows("SELECT * FROM worker_launches")) == 1


async def test_pause_resume_and_owner_steering_preserve_join_handles(store):
    runtime = Runtime(store)
    task = store.new_task("Wait for scoped evidence")
    await invoke(
        runtime,
        store,
        task,
        "apply_request",
        {
            "path": "config/agent-presets/inspect-files.toml",
            "bindings": {"question": "Inspect", "cwd": str(store.config.root)},
        },
        "launch",
    )
    waiting = await invoke(runtime, store, task, "await_workers", {}, "join")
    before = store.rows("SELECT id FROM tasks WHERE parent_id=?", (task,))
    runtime.pause(task)
    runtime.workers.poll()
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "paused"
    runtime.resume(task)
    runtime.steer(task, "New question supersedes that inspection.")
    await runtime.evidence.poll()
    runtime.workers.poll()
    assert store.rows("SELECT id FROM tasks WHERE parent_id=?", (task,)) == before
    assert store.one("SELECT obsolete FROM worker_launches")["obsolete"] == 1
    assert runtime.workers.flush(task) == []
    assert not store.rows("SELECT * FROM item_origins WHERE kind='worker_evidence'")
    assert (
        store.one("SELECT status FROM operations WHERE id=?", (waiting["id"],))["status"]
        == "prepared"
    )


async def test_pending_preset_artifacts_are_pinned_and_legacy_release_is_refused(store):
    from bloom.storage import Archive
    from bloom.service import launch

    runtime = Runtime(store)
    task = store.new_task("Keep receipt local")
    await invoke(
        runtime,
        store,
        task,
        "apply_request",
        {
            "path": "config/agent-presets/inspect-files.toml",
            "bindings": {"question": "Inspect", "cwd": str(store.config.root)},
        },
        "launch",
    )
    receipt = store.one("SELECT * FROM preset_invocations")
    assert {receipt[k] for k in ("template_hash", "bindings_hash", "request_hash")} <= Archive(
        store
    ).pins()
    legacy = store.config.artifacts / "releases/legacy"
    legacy.mkdir(parents=True)
    with pytest.raises(ValueError, match="explicit worker"):
        launch(
            store.config.root,
            {"root": str(store.config.root), "source": str(legacy), "manifest": {}},
        )
