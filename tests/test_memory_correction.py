"""Invalid semantic proposals require a fresh explicit Astra decision."""

import json

import httpx

from bloom.memory import Memory
from bloom.models import Models
from bloom.runtime import Runtime
from test_memory import Curator, approve_fixture, fact, task_fixture
from test_memory_authority import proposal
from test_control_provenance import invoke
from test_runtime_delivery import message
from test_worker_presets import wire


async def reviewed(store, text="My lasting preference is concise, accurate answers."):
    runtime = Runtime(store)
    task = store.new_task(text)
    store.status(task, "running")
    owner = runtime.recall.owner(task)
    op = await invoke(
        runtime, store, task, "memory_review", {"source_ids": [owner["id"]]}, "review"
    )
    return runtime, task, store.get(op["result_hash"])


def adapter(store, handle):
    store.config.data["providers"]["fixture"] = {
        "kind": "responses",
        "auth": "none",
        "base_url": "http://localhost",
        "stream": False,
        "max_retries": 0,
    }
    store.config.profile("astra")["provider"] = "fixture"
    return Models(store, httpx.MockTransport(handle))


async def test_prior_fact_citations_are_rejected_then_astra_explicitly_corrects_them(store):
    old = task_fixture(store, "My lasting preference is accurate answers.")
    await approve_fixture(
        Memory(store, Curator(store, lambda data: [fact(data, key="core/accuracy")])), task=old
    )
    prior = store.one("SELECT * FROM memory_facts")
    old_source = json.loads(prior["sources_json"])[0]
    store.status(old, "completed")
    runtime, task, review = await reviewed(store)
    value = proposal(review)
    value["updates"][0]["expected_revision"] = 1
    originals = store.rows("SELECT * FROM items ORDER BY id")
    sent = []

    def handle(request):
        body = json.loads(request.content)
        sent.append(body)
        assert body["model"] == store.config.profile("astra")["model"]
        if len(sent) < 3:
            candidate = json.loads(json.dumps(value))
            if len(sent) == 1:
                candidate["updates"][0]["sources"].insert(0, old_source)
            else:
                assert "unavailable source IDs" in json.dumps(body["input"])
            output = [
                wire(
                    runtime,
                    "memory_record",
                    {"batch": review["batch"], "proposal": candidate},
                    "approve-" + str(len(sent)),
                )
            ]
        else:
            output = [message("The memory update is approved and saved.")]
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )

    runtime.models = adapter(store, handle)
    runtime.context.models = runtime.models
    for _ in range(3):
        await runtime.step(task)
    assert store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
    current = store.one("SELECT * FROM memory_facts")
    assert (
        current["revision"] == 2
        and json.loads(current["sources_json"]) == value["updates"][0]["sources"]
    )
    assert json.loads(
        store.one("SELECT record_json FROM memory_fact_revisions WHERE revision=1")["record_json"]
    )["sources"] == json.loads(prior["sources_json"])
    assert all(store.one("SELECT * FROM items WHERE id=?", (r["id"],)) == r for r in originals)
    assert len(sent) == 3 and not store.rows(
        "SELECT * FROM model_calls WHERE purpose='memory_consolidation'"
    )


async def test_rejected_native_approval_stays_rejected_across_restart_without_hidden_purchase(
    store,
):
    runtime, task, review = await reviewed(store)
    rejected = await invoke(
        runtime,
        store,
        task,
        "memory_record",
        {"batch": review["batch"], "proposal": proposal(review, quote="Invented source")},
        "invalid",
    )
    assert rejected["status"] == "failed"
    before = store.rows("SELECT * FROM model_calls")
    tool = store.one("SELECT * FROM tool_calls WHERE operation_id=?", (rejected["id"],))
    resumed = Runtime(store)
    resumed.recover()

    async def forbidden(*a, **k):
        raise AssertionError("A rejected proposal cannot buy an automatic repair")

    resumed.models.infer = forbidden
    await resumed.execute(task, tool)
    assert store.rows("SELECT * FROM model_calls") == before
    assert not store.rows("SELECT * FROM memory_facts") and not store.rows(
        "SELECT * FROM memory_approvals"
    )
    assert store.one("SELECT status FROM memory_batches")["status"] == "awaiting_approval"


async def test_new_owner_message_invalidates_unpublished_approval_even_without_steering_event(
    store,
):
    runtime, task, review = await reviewed(store)
    store.new_task("A new owner request arrives before publication.")
    result = await invoke(
        runtime,
        store,
        task,
        "memory_record",
        {"batch": review["batch"], "proposal": proposal(review)},
        "stale",
    )
    assert result["status"] == "failed" and "New input" in json.dumps(
        store.get(result["result_hash"])
    )
    assert not store.rows("SELECT * FROM memory_facts") and not store.rows(
        "SELECT * FROM memory_approvals"
    )


async def test_blocked_task_can_compact_and_answer_without_accepting_failed_memory_or_replaying_alert(
    store,
):
    runtime, task, review = await reviewed(
        store, "Finish the research and retain the exact evidence."
    )
    store.db.execute(
        "UPDATE memory_batches SET status='failed',error='Fact needs bounded text and an exact evidence quote'"
    )
    old_batch = store.one("SELECT * FROM memory_batches")
    runtime.block(task, old_batch["error"])
    await runtime.delivery.flush()
    alert = store.one("SELECT * FROM deliveries")
    owner = store.one("SELECT id FROM owner_turns WHERE task_id=?", (task,))
    sent = []

    def handle(request):
        body = json.loads(request.content)
        sent.append(body)
        assert body["model"] == store.config.profile("astra")["model"]
        if not body["tools"]:
            output = [
                message(
                    json.dumps(
                        {
                            "approve": True,
                            "summary": "The research is pending; the failed optional memory proposal was not accepted. Continue from retained evidence.",
                        }
                    )
                )
            ]
        else:
            output = [message("The requested research answer.")]
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )

    models = adapter(store, handle)
    runtime = Runtime(store, models)

    async def forbidden(*a, **k):
        raise AssertionError("Answering must not invoke semantic consolidation")

    runtime.context.history.consolidate = forbidden
    runtime.context.history.request_compaction("astra", task)
    runtime.resume(task)
    await runtime.step(task)
    await runtime.delivery.flush()
    assert store.one("SELECT status,turns FROM tasks WHERE id=?", (task,)) == {
        "status": "completed",
        "turns": 1,
    }
    assert store.one("SELECT id FROM owner_turns WHERE task_id=?", (task,)) == owner
    assert store.one("SELECT * FROM deliveries WHERE id=?", (alert["id"],)) == alert
    assert store.one("SELECT * FROM memory_batches") == old_batch
    assert len(sent) == 2 and not store.rows("SELECT * FROM memory_facts")
    delivered = store.rows("SELECT * FROM deliveries")
    await runtime.finish(task)
    await runtime.delivery.flush()
    assert store.rows("SELECT * FROM deliveries") == delivered
