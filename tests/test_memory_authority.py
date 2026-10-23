import json

import httpx
import pytest

from bloom.models import Models
from bloom.runtime import Runtime
from test_control_provenance import invoke
from test_runtime_delivery import message, saved


def proposal(review, *, text="Prefer accurate answers.", quote=None):
    request = review["request"]
    source = next(f for f in request["fragments"] if f["origin"] == "owner")
    return {
        "title": "Owner preference",
        "summary": "The owner stated a durable preference.",
        "events": [
            {"id": key, "description": "Supplied source fragment reviewed."}
            for key in dict.fromkeys(f["node_id"] for f in request["fragments"])
        ],
        "updates": [
            {
                "kind": "persona",
                "key": "core/accuracy",
                "scope": "core",
                "text": text,
                "status": "active",
                "confidence": "explicit",
                "sources": [source["item_id"]],
                "quote": quote or source["text"],
                "expected_revision": 0,
            }
        ],
    }


async def test_semantic_memory_waits_for_exact_astra_native_approval(store):
    runtime = Runtime(store)
    task = store.new_task("My lasting preference is accurate answers.")

    async def forbidden(*a, **k):
        raise AssertionError("Reviewing memory must not launch Luna or any hidden model")

    runtime.models.infer = forbidden
    reviewed = await invoke(runtime, store, task, "memory_review", {}, "review")
    review = store.get(reviewed["result_hash"])
    assert review["approval_required"] and not store.rows("SELECT * FROM memory_facts")
    value = proposal(review)
    with pytest.raises(PermissionError, match="Astra approval"):
        runtime.context.history.record(task, "invented", review["batch"], value)
    accepted = await invoke(
        runtime,
        store,
        task,
        "memory_record",
        {"batch": review["batch"], "proposal": value},
        "approve",
    )
    assert accepted["status"] == "completed"
    assert store.one("SELECT text,revision FROM memory_facts") == {
        "text": "Prefer accurate answers.",
        "revision": 1,
    }
    approval = store.one("SELECT * FROM memory_approvals")
    assert (
        store.one("SELECT model_requested FROM model_calls WHERE id=?", (approval["call_id"],))[
            "model_requested"
        ]
        == store.config.profile("astra")["model"]
    )
    assert store.get(approval["payload_hash"])["proposal"] == value
    tool = store.one("SELECT * FROM tool_calls WHERE operation_id=?", (accepted["id"],))
    resumed = Runtime(store)
    resumed.recover()
    await resumed.execute(task, tool)
    assert store.one("SELECT count(*) n FROM memory_fact_revisions")["n"] == 1


@pytest.mark.parametrize("bad", ["oversized", "fabricated"])
async def test_rejected_proposal_is_a_tool_error_not_a_task_blocker(store, bad):
    runtime = Runtime(store)
    task = store.new_task("My lasting preference is accurate answers.")
    reviewed = await invoke(runtime, store, task, "memory_review", {}, "review")
    review = store.get(reviewed["result_hash"])
    value = (
        proposal(review, text="x" * 2637)
        if bad == "oversized"
        else proposal(review, quote="fabricated original")
    )
    rejected = await invoke(
        runtime, store, task, "memory_record", {"batch": review["batch"], "proposal": value}, "bad"
    )
    assert (
        rejected["status"] == "failed"
        and store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] != "blocked"
    )
    assert not store.rows("SELECT * FROM memory_facts") and not store.rows(
        "SELECT * FROM memory_approvals"
    )
    assert store.one("SELECT status FROM memory_batches")["status"] == "awaiting_approval"
    good = await invoke(
        runtime,
        store,
        task,
        "memory_record",
        {"batch": review["batch"], "proposal": proposal(review)},
        "corrected-by-astra",
    )
    assert (
        good["status"] == "completed"
        and store.one("SELECT count(*) n FROM memory_fact_revisions")["n"] == 1
    )


async def test_sol_luna_and_luna_owned_astra_helpers_cannot_approve_memory(store):
    runtime = Runtime(store)
    task = store.new_task("Research without unapproved memory writes.")
    sol = await runtime.request(
        "astra", task, "sol", {"operation": "delegate", "role": "sol", "objective": "Investigate"}
    )
    luna = await runtime.request(
        sol["agent_id"],
        sol["task_id"],
        "luna",
        {"operation": "delegate", "role": "luna", "objective": "Read evidence"},
    )
    update = {
        "topic": "global",
        "expected_revision": 0,
        "summary": "Unauthorized",
        "rationale": "fixture",
        "sources": ["owner"],
    }
    for worker in (sol, luna):
        assert not {"memory_update", "memory_record", "memory_review"} & {
            s["name"] for s in runtime.tools.schemas(worker["agent_id"])
        }
        with pytest.raises(PermissionError):
            await runtime.request(
                worker["agent_id"],
                worker["task_id"],
                "write-memory",
                {"operation": "memory", **update},
            )
    with pytest.raises(PermissionError, match="actual completed Astra"):
        await runtime.request("astra", task, "ownerless", {"operation": "memory_update", **update})
    output = [
        {
            "type": "function_call",
            "call_id": "helper-approval",
            "name": "memory_update",
            "arguments": json.dumps(update),
        }
    ]
    call = saved(store, task, output)
    store.db.execute(
        "UPDATE model_calls SET model_requested=? WHERE id=?",
        (store.config.profile("luna")["model"], call),
    )
    runtime.ingest_response(task, call, {"output": output})
    await runtime.execute(task, store.one("SELECT * FROM tool_calls WHERE task_id=?", (task,)))
    assert not store.rows("SELECT * FROM memories") and not store.rows(
        "SELECT * FROM memory_approvals"
    )


async def test_legacy_consolidation_is_read_only_and_never_calls_luna(store):
    runtime = Runtime(store)
    task = store.new_task("An unapproved memory candidate.")

    async def forbidden(*a, **k):
        raise AssertionError("Automatic memory generation is prohibited")

    runtime.context.history.models.infer = forbidden
    result = await runtime.context.history.consolidate(task=task)
    assert result["approval_required"] and not store.rows("SELECT * FROM memory_facts")
    assert not store.rows("SELECT * FROM model_calls")


def compactor(store, runtime, *, fail=False):
    store.config.data["providers"]["fixture"] = {
        "kind": "responses",
        "auth": "none",
        "base_url": "http://localhost",
        "stream": False,
        "max_retries": 0,
    }
    store.config.profile("astra")["provider"] = "fixture"
    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        assert body["model"] == store.config.profile("astra")["model"] and body["tools"] == []
        if fail:
            return httpx.Response(503, json={"error": "fixture compaction unavailable"})
        output = json.dumps(
            {
                "approve": True,
                "summary": "The owner wants the research finished. Evidence was gathered; preserve the unresolved acceptance check and consult retained source receipts.",
            }
        )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [message(output)],
                "usage": {"input_tokens": 100, "output_tokens": 40},
            },
        )

    runtime.models = Models(store, httpx.MockTransport(handle))
    runtime.context.models = runtime.models
    runtime.context.history.models = runtime.models
    return seen


async def test_working_compaction_does_not_require_publishing_old_owner_history(store):
    runtime = Runtime(store)
    previous = store.new_task(
        "An old completed discussion. " + "Historical source material. " * 6000
    )
    store.status(previous, "completed")
    task = store.new_task("Current obligation: finish the concise research answer.")
    runtime.context.history.index()
    before = runtime.context.history.state("astra", task=task)
    assert runtime.context.count(before) > 2000
    pending = runtime.context.history.pending("astra", task)
    seen = compactor(store, runtime)
    original = store.rows("SELECT * FROM items")
    ident = await runtime.context.compact("astra", task, target=2000)
    assert ident and len(seen) == 1
    manifest = store.get(store.one("SELECT source_hash FROM working_compactions")["source_hash"])
    assert set(manifest["owner_history_covered_in_memo"]) == {r["id"] for r in original}
    assert not manifest["state"]["unconsolidated_owner_messages"]
    assert (
        manifest["state"]["operational"]["unfinished_tasks"][0]["objective"]
        == "Current obligation: finish the concise research answer."
    )
    assert runtime.context.history.pending("astra", task) == pending
    assert all(store.one("SELECT * FROM items WHERE id=?", (r["id"],)) == r for r in original)
    assert not store.rows("SELECT * FROM memory_facts") and not store.rows(
        "SELECT * FROM memory_batches"
    )
    # Next compaction uses prior coverage; it must not reintroduce the original
    # long completed discussion into exact carried state.
    store.item(
        "astra",
        {"role": "user", "content": "Keep this correction in the memo."},
        task,
        origin="owner_steering",
    )
    await runtime.context.compact("astra", task, target=2000)
    assert len(seen) == 2 and not store.rows("SELECT * FROM memory_facts")


async def test_astra_working_compaction_ignores_rejected_memory_and_preserves_all_sources(store):
    runtime = Runtime(store)
    task = store.new_task("Finish this research; do not publish new permanent facts.")
    store.status(task, "running")
    await runtime.context.compose("astra", task, [])
    raw = {
        "type": "function_call",
        "call_id": "old-page",
        "name": "web_open",
        "arguments": '{"url":"https://example.test"}',
    }
    call = saved(store, task, [raw])
    runtime.ingest_response(task, call, {"output": [raw]})
    runtime.result_item(
        store.one("SELECT * FROM tool_calls WHERE task_id=?", (task,)),
        {"text": "retained observation\n" * 200},
        False,
    )
    originals = store.rows("SELECT * FROM items ORDER BY id")
    seen = compactor(store, runtime)

    async def forbidden(*a, **k):
        raise AssertionError("Context compression must not call permanent-memory consolidation")

    runtime.context.history.consolidate = forbidden
    ident = await runtime.context.compact("astra", task)
    assert ident and len(seen) == 1 and not store.rows("SELECT * FROM memory_facts")
    assert not store.rows("SELECT * FROM memory_fact_revisions") and not store.rows(
        "SELECT * FROM memory_approvals"
    )
    assert all(store.one("SELECT * FROM items WHERE id=?", (r["id"],)) == r for r in originals)
    selected = runtime.context.selected("astra", task)
    assert len(selected) == 1 and selected[0]["kind"] == "message"
    assert (
        store.one("SELECT kind FROM item_origins WHERE item_id=?", (selected[0]["id"],))["kind"]
        == "compaction"
    )
    before = [store.get(r["envelope_hash"]) for r in selected]
    store.item("astra", message("A later observation."), task)
    _, after = await runtime.context.compose("astra", task, [])
    assert after[: len(before)] == before and len(seen) == 1
    assert store.one("SELECT status FROM working_compactions")["status"] == "completed"


async def test_failed_working_compaction_uses_remaining_real_window_without_repurchase(store):
    runtime = Runtime(store)
    task = store.new_task("Continue the research.")
    store.status(task, "running")
    await runtime.context.compose("astra", task, [])
    store.config.profile("astra")["compact_at"] = (
        1  # Exercise the same trigger without a heavy unit fixture.
    )
    seen = compactor(store, runtime, fail=True)
    _, first = await runtime.context.compose("astra", task, [])
    _, second = await runtime.context.compose("astra", task, [])
    assert len(seen) == 1 and second == first
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "running"
    assert any("compaction deferred" in json.dumps(i).lower() for i in first)
    assert not store.rows("SELECT * FROM memory_facts")
    store.config.profile("astra")["context_limit"] = 1
    with pytest.raises(RuntimeError, match="model window"):
        await runtime.context.compose("astra", task, [])


async def test_luna_keeps_scratch_scripts_but_cannot_receive_substantive_write_mode(store):
    runtime = Runtime(store)
    task = store.new_task("Inspect with disposable helpers.")
    with pytest.raises(PermissionError, match="disposable helper"):
        await runtime.request(
            "astra",
            task,
            "write-worker",
            {
                "operation": "delegate",
                "role": "luna",
                "purpose": "attempt",
                "objective": "Patch app code",
                "mode": "workspace",
            },
        )
    worker = await runtime.request(
        "astra",
        task,
        "reader",
        {
            "operation": "delegate",
            "role": "luna",
            "purpose": "evidence",
            "objective": "Inspect data with a tiny helper",
        },
    )
    assert store.one("SELECT mode FROM agents WHERE id=?", (worker["agent_id"],))["mode"] == "read"
    names = {s["name"] for s in runtime.tools.schemas(worker["agent_id"])}
    assert "start_process" in names and not {"write_file", "patch_file"} & names


async def test_failed_compaction_does_not_permanently_block_resumed_task_new_evidence(store):
    runtime = Runtime(store)
    task = store.new_task("Finish retained work")
    store.status(task, "running")
    await runtime.context.compose("astra", task, [])
    store.config.profile("astra")["compact_at"] = 1
    failed = compactor(store, runtime, fail=True)
    await runtime.context.compose("astra", task, [])
    await runtime.context.compose("astra", task, [])
    assert len(failed) == 1
    store.item(
        "astra",
        {"role": "user", "content": "New verified source after explicit task recovery."},
        task,
        origin="owner_steering",
    )
    accepted = compactor(store, runtime)
    await runtime.context.compose("astra", task, [])
    assert len(accepted) == 1
    assert [
        r["status"] for r in store.rows("SELECT status FROM working_compactions ORDER BY rowid")
    ] == ["failed", "completed"]
