import pytest

from bloom.config import Config
from bloom.memory import Memory
from test_memory import Curator, task_fixture
from test_recall import fixture_runtime
from test_runtime_delivery import message


@pytest.mark.parametrize("setting", ["retrieval_input_tokens", "consolidation_tokens"])
def test_luna_input_budgets_accept_256k_and_up_to_actual_window(store, setting):
    for budget in (256000, 512000, 967232):
        config = Config(store.config.root, {"memory": {setting: budget}})
        assert config.data["memory"][setting] == budget
    for budget in (32000, 255999, 967233):
        with pytest.raises(ValueError, match=setting):
            Config(store.config.root, {"memory": {setting: budget}})


@pytest.mark.parametrize(
    "override",
    [
        {"compact_to": 96000},
        {"compact_at": 255999, "compact_to": 200000},
        {"context_limit": 280000},
    ],
)
def test_luna_profile_cannot_reintroduce_a_smaller_context_budget(store, override):
    with pytest.raises(ValueError, match="context"):
        Config(store.config.root, {"profiles": {"luna": override}})


async def test_recall_reads_and_reports_with_real_history_above_old_32k_cap(store):
    runtime, task, seen = fixture_runtime(store)
    await runtime.step(task)
    job = store.one("SELECT * FROM memory_retrievals")
    # Representative accumulated evidence, with real tokenization, not just a
    # changed constant. Originals and the verified source quote remain intact.
    store.item(
        job["agent_id"], message("Earlier evidence: " + "observation " * 40000), job["task_id"]
    )
    rows = runtime.context.selected(job["agent_id"], job["task_id"])
    tokens = runtime.context.count([store.get(r["envelope_hash"]) for r in rows])
    assert 32000 < tokens < 256000
    await runtime.step(job["task_id"])
    await runtime.step(job["task_id"])
    await runtime.step(job["task_id"])  # Explicit launch does not buy a hidden first lookup.
    report = store.get(store.one("SELECT report_hash FROM memory_retrievals")["report_hash"])
    assert report["status"] == "found" and report["sources"][0]["quote"] == "iris-739"
    assert (
        store.one("SELECT status FROM tasks WHERE id=?", (job["task_id"],))["status"] == "completed"
    )
    assert len(seen) == 4


@pytest.mark.parametrize("tokens,allowed", [(256000, True), (256001, False)])
async def test_recall_input_boundary_before_purchase(store, monkeypatch, tokens, allowed):
    runtime, task, seen = fixture_runtime(store)
    await runtime.step(task)
    job = store.one("SELECT * FROM memory_retrievals")
    before = len(seen)
    monkeypatch.setattr(runtime.context, "count", lambda value: tokens)
    await runtime.step(job["task_id"])
    assert len(seen) == before + int(allowed)
    status = store.one("SELECT status,error FROM tasks WHERE id=?", (job["task_id"],))
    if not allowed:
        assert status["status"] == "blocked" and "input budget" in status["error"]
        assert (
            store.get(store.one("SELECT report_hash FROM memory_retrievals")["report_hash"])[
                "status"
            ]
            == "unavailable"
        )
    else:
        assert status["status"] != "blocked"


@pytest.mark.parametrize("tokens,allowed", [(200000, True), (256000, True), (256001, False)])
async def test_legacy_consolidation_never_spawns_luna_or_publishes_regardless_of_budget(
    store, tokens, allowed
):
    task = task_fixture(store)
    curator = Curator(store)
    counted = []

    def count(value):
        counted.append(value)
        return tokens

    memory = Memory(store, curator, count)
    result = await memory.consolidate(task=task)
    assert result["approval_required"] and result["pending_fragments"]
    assert not curator.requests and not counted
    assert not store.rows("SELECT * FROM memory_facts")
