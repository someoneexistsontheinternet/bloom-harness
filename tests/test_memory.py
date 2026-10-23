import asyncio
import datetime as dt
import hashlib
import json

import httpx
import pytest

from bloom.context import Context
from bloom.memory import Memory, MemoryInterrupted, PROMPT
from bloom.models import Models
from bloom.runtime import Runtime
from bloom.store import Store, now
from test_runtime_delivery import message, saved


class Curator:
    def __init__(self, store, updates=None, interrupt=None):
        self.store, self.updates, self.interrupt = store, updates, interrupt
        self.requests = []

    async def infer(self, agent, task, instructions, items, tools, **kwargs):
        ident = kwargs["call_id"]
        old = self.store.one("SELECT response_hash FROM model_calls WHERE id=?", (ident,))
        if old:
            return ident, self.store.get(old["response_hash"])
        if kwargs.get("purpose") == "compaction":
            data = {"history": items[:-1], "request": json.loads(items[-1]["content"])}
            self.requests.append(data)
            observations = "\n".join(
                json.dumps(i, ensure_ascii=False)
                for i in items[:-1]
                if i.get("type") == "function_call_output"
            )
            response = {
                "output": [
                    message(
                        json.dumps(
                            {
                                "approve": True,
                                "summary": "Astra retains the requested goal, completed observations, unresolved acceptance and original source references.\n"
                                + observations[:8000],
                            }
                        )
                    )
                ],
                "status": "completed",
            }
            self.store.db.execute(
                "INSERT INTO model_calls(id,agent_id,purpose,model_requested,provider,status,request_hash,response_hash,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    agent,
                    "compaction",
                    self.store.config.profile("astra")["model"],
                    "fixture",
                    "completed",
                    self.store.put(items),
                    self.store.put(response),
                    now(),
                ),
            )
            if self.interrupt:
                self.interrupt()
            return ident, response
        data = json.loads(items[0]["content"])
        self.requests.append(data)
        result = {
            "title": "Memory design and exact evidence",
            "summary": "Chronological record of supplied source fragments; decisions remain source-linked.",
            "events": [
                {
                    "id": key,
                    "description": "Observed source: "
                    + next(f["text"][:180] for f in data["fragments"] if f["node_id"] == key),
                }
                for key in dict.fromkeys(f["node_id"] for f in data["fragments"])
            ],
            "updates": self.updates(data) if self.updates else [],
        }
        response = {"output": [message(json.dumps(result))], "status": "completed"}
        self.store.db.execute(
            "INSERT INTO model_calls(id,agent_id,purpose,model_requested,provider,status,request_hash,response_hash,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                ident,
                agent,
                "turn",
                self.store.config.profile("astra")["model"],
                "fixture",
                "completed",
                self.store.put(items),
                self.store.put(response),
                now(),
            ),
        )
        if self.interrupt:
            self.interrupt()
        return ident, response


async def approve_fixture(memory, agent="astra", task=None, max_batches=None):
    """Unit publisher fixture: explicitly model Astra approval, never automatic Luna.

    Native authorization and tool errors are exercised in test_memory_authority;
    these older hierarchy/source tests isolate publication from chat bookkeeping.
    """
    store = memory.store
    completed = []
    for _ in range(max_batches or 4):
        review = memory.prepare(agent, task)
        if not review["batch"]:
            break
        batch = store.one("SELECT * FROM memory_batches WHERE id=?", (review["batch"],))
        call, response = await memory.models.infer(
            agent,
            task,
            PROMPT,
            [{"role": "user", "content": json.dumps(review["request"])}],
            [],
            call_id=batch["call_id"],
            purpose="turn",
            profile_override=store.config.profile("astra"),
            retry_limit=0,
        )
        text = "\n".join(
            p["text"]
            for i in response["output"]
            if i.get("type") == "message"
            for p in i.get("content", [])
            if "text" in p
        )
        proposal = json.loads(text)
        op = "test_approval_" + batch["id"]
        payload = {"operation": "memory_record", "batch": batch["id"], "proposal": proposal}
        store.db.execute(
            "INSERT OR IGNORE INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
            (op, task, "fixture", "memory_record", "prepared", store.put(payload), now()),
        )
        try:
            with store.transaction():
                store.db.execute(
                    "INSERT OR REPLACE INTO memory_approvals VALUES(?,?,?,?,?)",
                    (op, task, call, store.put(payload), now()),
                )
                result = memory.record(task, op, batch["id"], proposal)
                store.db.execute(
                    "UPDATE operations SET status='completed',result_hash=? WHERE id=?",
                    (store.put(result), op),
                )
        except BaseException as exc:
            store.db.execute(
                "UPDATE operations SET status='failed',result_hash=? WHERE id=?",
                (store.put({"error": str(exc)}), op),
            )
            raise
        completed.append(batch["id"])
    return {
        "completed_batches": completed,
        "pending_fragments": len(memory.pending(agent, task)),
        "version": memory.version(agent),
    }


def fact(
    data,
    *,
    kind="persona",
    key="core/relationship",
    text="One continuous Astra.",
    confidence="explicit",
    quote=None,
):
    source = data["fragments"][0]
    old = next((f for f in data["existing_facts"] if f["key"] == key and f["kind"] == kind), None)
    return {
        "kind": kind,
        "key": key,
        "scope": "core",
        "text": text,
        "status": "active",
        "confidence": confidence,
        "sources": [source["item_id"]],
        "quote": quote or source["text"],
        "expected_revision": old["revision"] if old else 0,
    }


def task_fixture(store, objective="Keep one continuous Astra. Design memory before compaction."):
    task = store.new_task(objective)
    store.status(task, "running")
    return task


async def test_hierarchy_state_persona_exact_sources_and_no_repeat(store):
    task = task_fixture(store)
    curator = Curator(store, lambda data: [fact(data)])
    memory = Memory(store, curator)
    original = store.rows("SELECT * FROM items")
    result = await approve_fixture(memory, task=task)
    assert result["pending_fragments"] == 0 and result["version"] == 1
    assert store.rows("SELECT * FROM items") == original
    assert {r["kind"] for r in store.rows("SELECT kind FROM memory_nodes")} == {
        "event",
        "episode",
        "day",
    }
    episode = store.one("SELECT id FROM memory_nodes WHERE kind='episode'")["id"]
    children = memory.search("astra", "", task=task, parent=episode)["nodes"]
    assert children[0]["id"] == "event:1"
    upper = json.loads(memory.read("astra", episode, task=task)["text"])
    assert upper["kind"] == "episode" and upper["children"][0]["id"] == "event:1"
    exact = memory.read("astra", children[0]["id"], task=task)
    assert json.loads(exact["text"])[0]["envelope"] == store.get(original[0]["envelope_hash"])
    persona = memory.search("astra", "continuous", task=task)["persona"][0]
    assert persona["id"] == "persona:core/relationship" and persona["ancestors"] == ["core"]
    assert memory.read("astra", persona["id"], task=task)["complete"]
    await approve_fixture(Memory(store, curator), task=task)
    assert len(curator.requests) == 1
    assert store.one("SELECT count(*) AS n FROM memory_fact_revisions")["n"] == 1


async def test_new_owner_input_rejects_stale_publication_and_is_visible_immediately(store):
    task = task_fixture(store)

    def interrupt():
        store.item(
            "astra",
            {"role": "user", "content": "Correction: do not implement yet."},
            task,
            origin="owner_steering",
        )

    memory = Memory(store, Curator(store, lambda d: [fact(d)], interrupt))
    with pytest.raises(MemoryInterrupted, match="New input"):
        await approve_fixture(memory, task=task)
    assert not store.rows("SELECT * FROM memory_facts")
    assert not store.rows("SELECT * FROM memory_fragments WHERE batch_id IS NOT NULL")
    memory.index()
    assert "do not implement yet" in json.dumps(memory.state("astra", task=task))
    assert store.one("SELECT status FROM memory_batches")["status"] == "awaiting_approval"


async def test_fabricated_quote_rolls_back_and_explicit_persona_requires_owner(store):
    task = task_fixture(store)
    memory = Memory(
        store, Curator(store, lambda d: [fact(d, quote="A quote that was never supplied")])
    )
    with pytest.raises(ValueError, match="does not match"):
        await approve_fixture(memory, task=task)
    assert memory.version("astra") == 0
    assert not store.rows("SELECT * FROM memory_nodes WHERE kind='episode'")
    # Reconciliation is explicit even if the provider returned a completed but invalid answer.
    ident = store.one("SELECT id FROM memory_batches")["id"]
    memory.retry(ident)
    memory.models = Curator(store)
    await approve_fixture(memory, task=task)
    store.item("astra", message("the owner always prefers destructive cleanup."), task)
    memory.models = Curator(store, lambda d: [fact(d, text="Destructive cleanup is preferred.")])
    with pytest.raises(ValueError, match="actual owner"):
        await approve_fixture(memory, task=task)
    assert not memory.facts("astra")


async def test_completed_model_result_reused_after_publication_crash(store, monkeypatch):
    task = task_fixture(store)
    curator = Curator(store)
    memory = Memory(store, curator)
    real = memory.publish
    monkeypatch.setattr(
        memory, "publish", lambda *a, **k: (_ for _ in ()).throw(OSError("publication interrupted"))
    )
    with pytest.raises(OSError):
        await approve_fixture(memory, task=task)
    monkeypatch.setattr(memory, "publish", real)
    await approve_fixture(memory, task=task)
    assert len(curator.requests) == 1
    assert store.one("SELECT status FROM memory_batches")["status"] == "completed"


async def test_long_result_fully_partitioned_and_direct_search_bypasses_summary(store):
    task = task_fixture(store)
    text = "x " * 12000 + " exact-needle-4821 " + " y" * 12000
    item = store.item(
        "astra", {"type": "function_call_output", "call_id": "historic", "output": text}, task
    )
    memory = Memory(store, Curator(store))
    memory.index()
    chunks = store.rows(
        "SELECT text FROM memory_fragments WHERE node_id=? ORDER BY part", ("event:" + str(item),)
    )
    assert "".join(r["text"] for r in chunks) == text
    node = store.one("SELECT body FROM memory_nodes WHERE id=?", ("event:" + str(item),))
    assert "exact-needle-4821" not in node["body"]
    assert memory.search("astra", "exact-needle-4821", task=task)["nodes"][0][
        "id"
    ] == "event:" + str(item)
    start = 0
    parts = []
    while True:
        page = memory.read("astra", "event:" + str(item), task=task, start=start, limit=5000)
        parts.append(page["text"])
        if page["next_start"] is None:
            break
        start = page["next_start"]
    assert json.loads("".join(parts))[0]["envelope"]["output"] == text
    assert hashlib.sha256("".join(parts).encode()).hexdigest() == page["serialization_sha256"]
    await approve_fixture(memory, task=task, max_batches=8)
    assert not memory.pending("astra", task)


async def test_full_compaction_zero_native_tail_queued_inputs_and_real_request(store):
    task = task_fixture(store)
    queued = store.new_task("Queued private objective should not enter current work.")
    runtime = Runtime(store)
    output = [
        {
            "type": "function_call",
            "call_id": "read",
            "name": "read_file",
            "arguments": '{"path":"missing.txt"}',
        }
    ]
    call = saved(store, task, output)
    runtime.ingest_response(task, call, {"output": output})
    runtime.result_item(
        store.one("SELECT * FROM tool_calls"), {"observed": "sealed-code-718"}, False
    )
    original = store.rows("SELECT * FROM items")
    curator = Curator(store, lambda d: [fact(d)])
    context = Context(store, curator)
    ident = await context.compact("astra", task)
    assert ident
    selected = context.selected("astra", task)
    assert len(selected) == 1 and selected[0]["kind"] == "message"
    assert all("Queued private" not in json.dumps(x) for x in curator.requests)
    assert not any(
        x["kind"] in ("function_call", "function_call_output", "reasoning") for x in selected
    )
    for row in original:
        assert store.one("SELECT * FROM items WHERE id=?", (row["id"],)) == row
    # Reopen SQLite, assemble again, and inspect the actual serialized HTTP request.
    config = store.config
    reopened = Store(config)
    try:
        reopened.status(task, "completed")
        next_task = reopened.new_task(
            "What was observed from the read_file operation for missing.txt?"
        )
        reopened.status(next_task, "running")
        fresh = Context(reopened, Curator(reopened))
        tools = Runtime(reopened).tools.schemas("astra")
        instructions, items = await fresh.compose("astra", next_task, tools)
        assert all(
            x.get("type") not in ("function_call", "function_call_output", "reasoning")
            for x in items
        )
        assert "sealed-code-718" in json.dumps(items)
        assert "Queued private" not in json.dumps(items)
        seen = []

        def transport(request):
            seen.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "response",
                    "model": "fixture",
                    "status": "completed",
                    "output": [message("sealed-code-718")],
                },
            )

        config.data["providers"]["fixture"] = {
            "kind": "responses",
            "auth": "none",
            "base_url": "http://localhost",
            "stream": False,
        }
        models = Models(reopened, httpx.MockTransport(transport))
        await models.infer(
            "astra",
            next_task,
            instructions,
            items,
            tools,
            profile_override={"provider": "fixture"},
            item_ids=fresh.members["astra"],
        )
        assert seen[0]["input"] == items
        assert seen[0]["tools"] and any(x["name"] == "memory_read" for x in seen[0]["tools"])
        rows = fresh.selected("astra", queued)
        assert (
            sum(
                x["text"] == "Queued private objective should not enter current work." for x in rows
            )
            == 1
        )
    finally:
        reopened.close()


async def test_pending_native_call_refuses_compaction_and_control_queues(store):
    task = task_fixture(store)
    runtime = Runtime(store)
    output = [
        {
            "type": "function_call",
            "call_id": "pending",
            "name": "read_file",
            "arguments": '{"path":"x"}',
        }
    ]
    call = saved(store, task, output)
    runtime.ingest_response(task, call, {"output": output})
    with pytest.raises(MemoryInterrupted, match="unsettled"):
        await runtime.context.compact("astra", task)
    receipt = await runtime.request("astra", task, "compact-control", {"operation": "compact"})
    assert receipt["queued_compaction"]
    assert not store.rows("SELECT * FROM contexts")
    runtime.result_item(store.one("SELECT * FROM tool_calls"), {"not_executed": True}, False)
    runtime.context.models = runtime.context.history.models = Curator(store)
    await runtime.context.compose("astra", task, runtime.tools.schemas("astra"))
    assert store.one("SELECT status FROM memory_compactions")["status"] == "completed"
    assert not any(
        row["kind"] == "function_call" for row in runtime.context.selected("astra", task)
    )


@pytest.mark.parametrize("pause_new_work", [False, True])
async def test_scope_and_idle_preemption(store, monkeypatch, pause_new_work):
    task = task_fixture(store)
    store.status(task, "completed")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat()
    store.db.execute("UPDATE items SET created_at=?", (old,))
    entered = asyncio.Event()

    class Slow:
        async def infer(self, *a, **k):
            entered.set()
            await asyncio.Event().wait()

    runtime = Runtime(store, Slow())
    original_count = runtime.context.count
    monkeypatch.setattr(
        runtime.context,
        "count",
        lambda value: (
            100001
            if isinstance(value, list) and value and all(isinstance(x, dict) for x in value)
            else original_count(value)
        ),
    )
    await runtime.maintain_memory()
    await asyncio.wait_for(entered.wait(), 1)
    new = store.new_task("New owner work has priority")
    if pause_new_work:
        runtime.pause(new)
    await runtime.maintain_memory()
    assert runtime.memory_job is None
    assert store.one("SELECT status FROM working_compactions")["status"] == "interrupted"
    assert not store.rows("SELECT * FROM memory_facts")
    store.db.execute(
        "INSERT INTO agents VALUES('worker','luna','astra','Worker','read',0,?,?)",
        (str(store.config.root), now()),
    )
    store.db.execute("INSERT INTO sessions VALUES('worker','worker',?)", (now(),))
    with pytest.raises(PermissionError):
        Memory(store).read("worker", "event:1")
    assert "memory_read" not in [x["name"] for x in runtime.tools.schemas("worker")]


async def test_state_revisions_and_operational_truth_do_not_depend_on_retrieval(store):
    task = task_fixture(store)
    curator = Curator(
        store,
        lambda d: [
            fact(d, kind="state", key="bloom/sequence", text="Design before implementation.")
        ],
    )
    memory = Memory(store, curator)
    await approve_fixture(memory, task=task)
    store.item(
        "astra",
        {"role": "user", "content": "Design is approved; now implement the memory MVP."},
        task,
        origin="owner_steering",
    )
    memory.models = Curator(
        store,
        lambda d: [fact(d, kind="state", key="bloom/sequence", text="Implementation approved.")],
    )
    await approve_fixture(memory, task=task)
    store.status(task, "blocked", "Await exact evidence")
    state = memory.state("astra", "completely unrelated query", task)
    assert state["facts"][0]["revision"] == 2
    assert state["facts"][0]["text"] == "Implementation approved."
    assert state["operational"]["unfinished_tasks"][0]["status"] == "blocked"
    assert len(store.rows("SELECT * FROM memory_fact_revisions")) == 2


async def test_offline_original_is_explicit_and_summary_remains_searchable(store, monkeypatch):
    task = task_fixture(store)
    memory = Memory(store, Curator(store))
    await approve_fixture(memory, task=task)
    original = store.bytes
    digest = store.one("SELECT envelope_hash FROM items WHERE id=1")["envelope_hash"]

    def unavailable(value, **kwargs):
        if value == digest:
            raise FileNotFoundError("Cold RAID disconnected")
        return original(value, **kwargs)

    monkeypatch.setattr(store, "bytes", unavailable)
    assert memory.search("astra", "continuous", task=task)["nodes"]
    records = json.loads(memory.read("astra", "event:1", task=task)["text"])
    assert "Cold RAID disconnected" in records[0]["unavailable"]


async def test_repeated_full_compaction_retains_packet_and_originals(store):
    task = task_fixture(store)
    context = Context(store, Curator(store))
    first = await context.compact("astra", task)
    second = await context.compact("astra", task)
    assert second != first
    selected = context.selected("astra", task)
    assert len(selected) == 1 and selected[0]["kind"] == "message"
    assert store.one("SELECT text FROM items WHERE id=1")["text"].startswith("Keep one continuous")


async def test_100k_is_rebuilt_budget_not_active_trigger(store):
    task = task_fixture(store)
    context = Context(store, Curator(store))
    store.config.data["memory"]["context_tokens"] = 8000
    store.config.data["memory"]["state_tokens"] = 2000
    store.config.data["memory"]["retrieval_tokens"] = 1000
    store.config.data["profiles"]["astra"]["compact_at"] = 24000
    for _ in range(10):
        store.item("astra", message("evidence " * 1000, "commentary"), task)
    # Isolate context budget behavior from machine/project instruction length.
    fixed = "Memory policy."
    instructions, items = await context.compose_memory("astra", task, [], fixed)
    assert context.count(items) > 8000
    assert store.one("SELECT through_item FROM contexts")["through_item"] == 0
    assert not store.rows("SELECT * FROM events WHERE kind='context.compacted'")
    assert not context.models.requests


async def test_pause_during_consolidation_invalidates_publication(store):
    task = task_fixture(store)
    runtime = Runtime(store)
    curator = Curator(store, interrupt=lambda: runtime.pause(task))
    with pytest.raises(MemoryInterrupted):
        await approve_fixture(Memory(store, curator), task=task)
    assert not store.rows("SELECT * FROM memory_facts")
    assert not store.rows("SELECT * FROM memory_nodes WHERE kind='episode'")


async def test_inferred_update_cannot_overwrite_explicit_persona(store):
    task = task_fixture(store)
    memory = Memory(store, Curator(store, lambda d: [fact(d)]))
    await approve_fixture(memory, task=task)
    store.item("astra", message("Maybe the owner wants separate main agents now."), task)
    memory.models = Curator(
        store, lambda d: [fact(d, confidence="inferred", text="Separate main agents preferred.")]
    )
    with pytest.raises(ValueError, match="cannot replace"):
        await approve_fixture(memory, task=task)
    assert memory.facts("astra")[0]["text"] == "One continuous Astra."


async def test_unsettled_operational_truth_survives_irrelevant_query(store):
    task = task_fixture(store)
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
        (
            "uncertain-effect",
            task,
            "tool",
            "write_file",
            "uncertain",
            store.put({"path": "x"}),
            now(),
        ),
    )
    memory = Memory(store)
    memory.index()
    assert (
        memory.state("astra", "unrelated", task)["operational"]["unsettled_operations"][0]["status"]
        == "uncertain"
    )


async def test_exact_memory_tool_is_not_generic_curated(store):
    task = task_fixture(store)
    runtime = Runtime(store)
    runtime.context.history.index()
    raw = await runtime.tools.run(
        "astra", task, "memory-read", "memory_read", {"node_id": "event:1"}
    )
    raw["text"] = "x" * 60000
    runtime.results.threshold = 10
    output = await runtime.results.present(
        "astra", task, {"name": "memory_read"}, raw, store.put(raw)
    )
    assert output == raw and not store.rows("SELECT * FROM model_calls")


def test_pagination_rejects_changed_view_and_child_effects_remain_visible(store):
    task = task_fixture(store)
    memory = Memory(store)
    memory.index()
    page = memory.read("astra", "event:1", task=task, limit=20)
    with pytest.raises(ValueError, match="view changed"):
        memory.read("astra", "event:1", task=task, start=20, expected_sha256="wrong")
    next_page = memory.read(
        "astra", "event:1", task=task, start=20, expected_sha256=page["serialization_sha256"]
    )
    assert next_page["start"] == 20
    store.db.execute(
        "INSERT INTO agents VALUES('child','sol','astra','Child','full',1,?,?)",
        (str(store.config.root), now()),
    )
    store.db.execute("INSERT INTO sessions VALUES('child','child',?)", (now(),))
    child = store.new_task("Child investigation", "child", task)
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
        ("child-effect", child, "tool", "write_file", "uncertain", store.put({}), now()),
    )
    state = memory.operational("astra", task)
    assert state["children"][0]["id"] == child
    assert state["unsettled_operations"][0]["id"] == "child-effect"


def test_initial_offline_index_does_not_block_new_work_and_recovers(store, monkeypatch):
    task = task_fixture(store)
    item = store.item(
        "astra",
        {
            "type": "function_call",
            "name": "read_file",
            "call_id": "offline",
            "arguments": '{"path":"rare-original-path"}',
        },
        task,
    )
    digest = store.one("SELECT envelope_hash FROM items WHERE id=?", (item,))["envelope_hash"]
    original = store.bytes

    def unavailable(value, **kwargs):
        if value == digest:
            raise FileNotFoundError("RAID disconnected")
        return original(value, **kwargs)

    memory = Memory(store)
    with monkeypatch.context() as patch:
        patch.setattr(store, "bytes", unavailable)
        memory.index()
        assert memory.state("astra", task=task)["unavailable_original_count"] == 1
        assert memory.search("astra", "continuous", task=task)["nodes"]
        new = store.item(
            "astra",
            {"role": "user", "content": "Independent new instruction"},
            task,
            origin="owner_steering",
        )
        memory.index()
        assert any(
            r["id"] == new
            for r in memory.state("astra", task=task)["unconsolidated_owner_messages"]
        )
    memory.index()
    assert not store.rows("SELECT * FROM memory_missing")
    assert memory.search("astra", "rare-original-path", task=task)["nodes"]


def test_leaf_expansion_includes_original_operation_result_not_only_curated_view(store):
    task = task_fixture(store)
    runtime = Runtime(store)
    output = [
        {
            "type": "function_call",
            "call_id": "historic-curated",
            "name": "read_file",
            "arguments": '{"path":"original.txt"}',
        }
    ]
    call = saved(store, task, output)
    runtime.ingest_response(task, call, {"output": output})
    tool = store.one("SELECT * FROM tool_calls")
    original = {"text": "unaltered source " * 5000}
    store.db.execute(
        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,result_hash,started_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            tool["operation_id"],
            task,
            "tool",
            "read_file",
            "completed",
            tool["arguments_hash"],
            store.put(original),
            now(),
        ),
    )
    runtime.result_item(tool, {"summary": "short derived view"}, False)
    memory = Memory(store)
    memory.index()
    offset = 0
    parts = []
    digest = None
    while True:
        page = memory.read(
            "astra",
            "event:" + str(tool["item_id"]),
            task=task,
            start=offset,
            expected_sha256=digest,
        )
        digest = page["serialization_sha256"]
        parts.append(page["text"])
        if page["next_start"] is None:
            break
        offset = page["next_start"]
    records = json.loads("".join(parts))
    assert (
        next(r for r in records if r["kind"] == "function_call")["operation"]["result"] == original
    )


async def test_idle_index_failure_is_supervised_without_stopping_coordinator(store, monkeypatch):
    task = task_fixture(store)
    store.status(task, "completed")
    store.db.execute(
        "UPDATE items SET created_at=?",
        ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat(),),
    )
    runtime = Runtime(store)
    monkeypatch.setattr(runtime.context, "count", lambda value: 100001)
    monkeypatch.setattr(
        runtime.context.history,
        "index",
        lambda *a: (_ for _ in ()).throw(OSError("index unavailable")),
    )
    await runtime.maintain_memory()
    await asyncio.sleep(0)
    await runtime.maintain_memory()
    assert runtime.memory_job is None and not runtime.stopping
    assert (
        store.one("SELECT count(*) AS n FROM events WHERE kind='memory.maintenance_failed'")["n"]
        == 1
    )


@pytest.mark.parametrize("tokens", [99999, 100000])
async def test_idle_below_or_at_threshold_never_consolidates(store, monkeypatch, tokens):
    task = task_fixture(store)
    store.status(task, "completed")
    store.db.execute(
        "UPDATE items SET created_at=?",
        ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat(),),
    )
    curator = Curator(store)
    runtime = Runtime(store, curator)
    monkeypatch.setattr(runtime.context, "count", lambda value: tokens)

    def forbidden(*args):
        raise AssertionError(
            "Below-threshold idle maintenance must not even index for consolidation"
        )

    monkeypatch.setattr(runtime.context.history, "index", forbidden)
    await runtime.maintain_memory()
    result = await runtime.memory_job
    assert result == {"skipped": "below_idle_token_threshold", "tail_tokens": tokens}
    assert not curator.requests
    assert not store.rows("SELECT * FROM memory_batches")
    assert not store.rows("SELECT * FROM contexts")
    await runtime.maintain_memory()
    assert runtime.memory_job is None


async def test_idle_above_threshold_compacts_once_and_excludes_frozen_packet(store, monkeypatch):
    task = task_fixture(store)
    store.status(task, "completed")
    store.db.execute(
        "UPDATE items SET created_at=?",
        ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat(),),
    )
    curator = Curator(store)
    runtime = Runtime(store, curator)
    counted = []
    original_count = runtime.context.count

    def count(value):
        counted.append(value)
        return (
            100001
            if isinstance(value, list) and value and all(isinstance(x, dict) for x in value)
            else original_count(value)
        )

    monkeypatch.setattr(runtime.context, "count", count)
    originals = store.rows("SELECT id,envelope_hash FROM items")
    await runtime.maintain_memory()
    result = await runtime.memory_job
    assert result["context_id"] and result["permanent_memory_changed"] is False
    assert len(curator.requests) == 1
    selected = runtime.context.selected("astra", task)
    assert (
        len(selected) == 1
        and selected[0]["id"] == store.one("SELECT item_id FROM memory_packets")["item_id"]
    )
    assert (
        store.rows("SELECT id,envelope_hash FROM items WHERE id<=?", (originals[-1]["id"],))
        == originals
    )
    # The current memory packet and covered native history do not count as a
    # new tail, even though the counter treats any nonempty history as large.
    result = await runtime.memory_maintenance()
    assert result == {"skipped": "below_idle_token_threshold", "tail_tokens": original_count([])}
    assert counted[-1] == [] and len(curator.requests) == 1


async def test_idle_age_and_active_work_still_gate_large_histories(store, monkeypatch):
    task = task_fixture(store)
    runtime = Runtime(store)
    monkeypatch.setattr(runtime.context, "count", lambda value: 100001)
    store.status(task, "completed")
    await runtime.maintain_memory()
    assert runtime.memory_job is None  # New owner input is not idle yet.
    store.db.execute(
        "UPDATE items SET created_at=?",
        ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat(),),
    )
    for status in ("waiting", "paused"):
        store.status(task, status)
        await runtime.maintain_memory()
        assert runtime.memory_job is None


async def test_foreground_compaction_obeys_owner_steering_deadline(store):
    task = task_fixture(store)
    entered = asyncio.Event()

    class Slow:
        async def infer(self, *a, **k):
            entered.set()
            await asyncio.Event().wait()

    runtime = Runtime(store, Slow())
    store.config.data["runtime"]["mention_seconds"] = 0.05
    runtime.context.history.request_compaction("astra", task)
    step = asyncio.create_task(runtime.step(task))
    await asyncio.wait_for(entered.wait(), 1)
    runtime.steer(task, "New correction interrupts this compaction.")
    await asyncio.wait_for(step, 2)
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "ready"
    assert store.one("SELECT through_item FROM contexts")["through_item"] == 0
    assert not store.rows("SELECT * FROM events WHERE kind='context.compacted'")
    assert store.one("SELECT status FROM working_compactions")["status"] == "interrupted"


async def test_persona_virtual_branches_expand_to_exact_sources(store):
    task = task_fixture(store, "Keep one continuous Astra. Use exact citations in research.")

    def updates(data):
        return [
            fact(data, key="identity/continuity", quote="one continuous Astra"),
            fact(
                data,
                key="work/research/citations",
                text="Use exact citations.",
                quote="exact citations in research",
            ),
        ]

    memory = Memory(store, Curator(store, updates))
    await approve_fixture(memory, task=task)
    root = memory.search("astra", "", task=task, parent="persona:")["nodes"]
    assert {n["id"] for n in root} == {"persona:identity", "persona:work"}
    branch = json.loads(memory.read("astra", "persona:work", task=task)["text"])
    assert branch["children"][0]["id"] == "persona:work/research"
    leaf = json.loads(memory.read("astra", "persona:work/research/citations", task=task)["text"])
    assert leaf["original_records"][0]["id"] == 1
