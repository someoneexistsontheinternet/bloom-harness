import httpx
import pytest

from bloom.models import Models, ProviderError
from bloom.reconciliation import abandon
from bloom.runtime import Runtime


async def uncertain(store, monkeypatch, tools=()):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    model = Models(
        store,
        httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ReadError("fixture"))),
    )
    task = store.new_task("Recover exactly this task")
    with pytest.raises(httpx.ReadError):
        await model.infer(
            "astra",
            task,
            "test",
            [],
            list(tools),
            profile_override={"provider": "openai"},
            retry_limit=0,
        )
    call = store.one("SELECT * FROM model_calls")
    return task, call


async def test_explicit_resume_preserves_uncertain_original_usage_and_attention_identity(
    store, monkeypatch
):
    task, call = await uncertain(store, monkeypatch)
    runtime = Runtime(store)
    runtime.recover()
    alerts = store.rows("SELECT * FROM deliveries WHERE kind LIKE 'attention:%'")
    runtime.block(task, "Different subsequent diagnostic")
    assert (
        len(alerts)
        == len(store.rows("SELECT * FROM deliveries WHERE kind LIKE 'attention:%'"))
        == 1
    )
    runtime.resume(task)
    assert store.one("SELECT status FROM tasks")["status"] == "ready"
    receipt = store.one("SELECT * FROM model_reconciliations")
    assert store.get(receipt["original_hash"])["call"] == call
    assert abandon(store, call["id"], "second explicit action") == receipt
    current = store.one("SELECT * FROM model_calls")
    assert (
        current["status"] == "failed"
        and current["input_tokens"] is None
        and current["response_hash"] is None
    )
    assert store.rows("SELECT * FROM deliveries WHERE kind LIKE 'attention:%'") == alerts


async def test_hosted_mutation_uncertainty_is_not_resolved_by_general_resume(store, monkeypatch):
    task, call = await uncertain(store, monkeypatch, [{"type": "mcp", "server_label": "mutating"}])
    runtime = Runtime(store)
    runtime.recover()
    with pytest.raises(ValueError, match="Hosted mutation"):
        runtime.resume(task)
    assert store.one("SELECT status FROM tasks")["status"] == "blocked"
    assert store.one("SELECT status FROM model_calls")["status"] == "uncertain"
    assert not store.rows("SELECT * FROM model_reconciliations")


async def test_authentication_failure_has_physical_receipt_and_diagnostics(store, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = Models(store)
    task = store.new_task("Missing credential")
    with pytest.raises(ProviderError, match="Missing credential"):
        await model.infer(
            "astra", task, "test", [], [], profile_override={"provider": "openai"}, retry_limit=0
        )
    row = store.one("SELECT * FROM model_calls")
    assert row["status"] == "failed"
    detail = store.get(store.one("SELECT detail_hash FROM model_call_diagnostics")["detail_hash"])
    assert detail["phase"] == "authentication" and detail["stream_events"] == 0
