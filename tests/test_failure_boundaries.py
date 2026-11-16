import asyncio
import json

import pytest

from bloom4.agents import Agents
from bloom4.budget import Budget
from bloom4.context import Context
from bloom4.mailbox import Mailbox
from bloom4.provider import Generation
from bloom4.runtime import Runtime
from bloom4.store import Store, encoded


def message(text):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


@pytest.mark.asyncio
async def test_crash_before_answer_publication_recovers_without_inference(tmp_path, monkeypatch):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    Budget(store).grant(2)
    runtime = Runtime(store, None)
    task_id = runtime.open("astra", "Deliver the answer", task="recover")
    Context(store).snapshot("astra")
    store.append("task.initialized", {}, target="astra", task=task_id)
    started = Budget(store).reserve("astra", task_id, Context(store).request("astra"))
    response = {
        "output": [message("Retained answer")],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    done = store.append(
        "model.completed",
        {"call": started["id"], "response": store.put(encoded(response)), "purpose": "turn"},
        target="astra",
        task=task_id,
    )
    original = store.append

    def fail(kind, *args, **kwargs):
        if kind == "answer":
            raise RuntimeError("injected crash before publication")
        return original(kind, *args, **kwargs)

    monkeypatch.setattr(store, "append", fail)
    with pytest.raises(RuntimeError):
        runtime.complete(runtime.tasks()[task_id], response, done["id"])
    monkeypatch.setattr(store, "append", original)
    await Runtime(store, None).run(seconds=1)
    assert len(store.events("answer")) == 1
    assert len(store.events("model.started")) == 1


@pytest.mark.asyncio
async def test_urgent_http_update_prevents_obsolete_write(tmp_path):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    Budget(store).grant(3)

    class Provider:
        output_tokens = 1000
        calls = 0

        async def generate(self, request, **kwargs):
            self.calls += 1
            if self.calls == 1:
                Mailbox(store).post("Stop: do not write the file.", urgent=True)
                await asyncio.sleep(0.3)
                output = [
                    {
                        "type": "function_call",
                        "name": "write_file",
                        "call_id": "obsolete",
                        "arguments": json.dumps(
                            {"path": "must-not-exist.txt", "text": "bad", "intent": "Old intent"}
                        ),
                    }
                ]
            else:
                output = [message("Stopped the obsolete write")]
            return Generation(
                {"output": output, "usage": {"input_tokens": 10, "output_tokens": 10}}
            )

    Mailbox(store).post("Write a file")
    provider = Provider()
    runtime = Runtime(store, provider, auto_recall=False)
    await runtime.run(seconds=3)
    assert not (tmp_path / "work/astra/must-not-exist.txt").exists()
    assert provider.calls == 2


def test_null_usage_remains_unknown_and_reserves_allowance(tmp_path):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    Budget(store).grant(2)
    call = Budget(store).reserve(
        "astra", "task", {"model": "test", "input": "x"}, output_reserve=100
    )
    store.append(
        "model.completed",
        {
            "call": call["id"],
            "response": store.put(encoded({"output": []})),
            "usage": {"input_tokens": None, "output_tokens": None},
        },
        target="astra",
        task="task",
    )
    status = Budget(store).status()
    assert status["unknown_usage_calls"] == 1
    assert status["accounted_output"] == 100


@pytest.mark.asyncio
async def test_compaction_does_not_consume_evidence_that_arrives_during_generation(tmp_path):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    Budget(store).grant(3)
    context = Context(store)
    context.append("astra", [{"role": "user", "content": "Original work"}])

    class Provider:
        output_tokens = 1000

        async def generate(self, request, **kwargs):
            context.append("astra", [{"role": "user", "content": "LATE_EXACT_EVIDENCE"}])
            return Generation(
                {
                    "output": [message("Approved summary of original work")],
                    "usage": {"input_tokens": 10, "output_tokens": 10},
                }
            )

    runtime = Runtime(store, Provider())
    await runtime.compact("astra", "maintenance")
    assert sum(i.get("content") == "LATE_EXACT_EVIDENCE" for i in context.items("astra")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["wake", "stage"])
async def test_material_appendix_arriving_during_final_is_reconsidered(tmp_path, decision):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    Budget(store).grant(3)

    class Provider:
        output_tokens = 1000
        calls = 0

        async def generate(self, request, **kwargs):
            self.calls += 1
            if self.calls == 1:
                task = next(iter(store.index.tasks()))
                store.append(
                    "appendix.staged",
                    {"decision": decision, "appendix": "CRITICAL_CORRECTION"},
                    actor="terra",
                    target="astra",
                    task=task,
                )
                return Generation(
                    {
                        "output": [message("Outdated answer")],
                        "usage": {"input_tokens": 10, "output_tokens": 10},
                    }
                )
            assert "CRITICAL_CORRECTION" in json.dumps(request)
            return Generation(
                {
                    "output": [message("Corrected answer")],
                    "usage": {"input_tokens": 10, "output_tokens": 10},
                }
            )

    provider = Provider()
    Mailbox(store).post("Answer the question")
    await Runtime(store, provider, auto_recall=False).run(seconds=3)
    assert provider.calls == (2 if decision == "wake" else 1)
    assert [e["data"]["text"] for e in store.events("answer")] == [
        "Corrected answer" if decision == "wake" else "Outdated answer"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("unstarted_write", [False, True])
async def test_restart_reconsiders_material_evidence_before_saved_response(
    tmp_path, unstarted_write
):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    Budget(store).grant(3)
    runtime = Runtime(store, None, auto_recall=False)
    task = runtime.open("astra", "Question", task="recover-material")
    context = Context(store)
    context.snapshot("astra")
    context.append("astra", [{"role": "user", "content": "Original question"}], task=task)
    store.append("task.initialized", {}, target="astra", task=task)
    started = Budget(store).reserve("astra", task, context.request("astra"))
    output = (
        [
            {
                "type": "function_call",
                "name": "write_file",
                "call_id": "stale",
                "arguments": json.dumps(
                    {"path": "stale.txt", "text": "bad", "intent": "Old intent"}
                ),
            }
        ]
        if unstarted_write
        else [message("Old answer")]
    )
    store.append(
        "model.completed",
        {
            "call": started["id"],
            "response": store.put(encoded({"output": output})),
            "purpose": "turn",
        },
        target="astra",
        task=task,
    )
    store.append(
        "appendix.staged",
        {"decision": "wake", "appendix": "EXACT_CORRECTION"},
        actor="terra",
        target="astra",
        task=task,
    )

    class Provider:
        output_tokens = 1000
        calls = 0

        async def generate(self, request, **kwargs):
            self.calls += 1
            assert "EXACT_CORRECTION" in json.dumps(request)
            return Generation(
                {
                    "output": [message("Reconsidered")],
                    "usage": {"input_tokens": 10, "output_tokens": 10},
                }
            )

    provider = Provider()
    await Runtime(store, provider, auto_recall=False).run(seconds=3)
    assert provider.calls == 1
    assert not (tmp_path / "work/astra/stale.txt").exists()
    assert [e["data"]["text"] for e in store.events("answer")] == ["Reconsidered"]
