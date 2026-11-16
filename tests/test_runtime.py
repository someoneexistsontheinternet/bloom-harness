import asyncio
from collections import defaultdict, deque
import json

import pytest

from bloom4.agents import Agents
from bloom4.budget import Budget
from bloom4.context import Context
from bloom4.mailbox import Mailbox
from bloom4.memory import Memory
from bloom4.provider import Generation, ProviderFailure
from bloom4.runtime import Runtime
from bloom4.store import Store, encoded


def message(text):
    return {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": text}]}


def tool(name, arguments, call_id):
    return {"type": "function_call", "name": name, "arguments": json.dumps(arguments), "call_id": call_id}


class Scripted:
    output_tokens = 1000
    def __init__(self, scripts, delays=None):
        self.scripts = {model: deque(values) for model, values in scripts.items()}
        self.delays = delays or {}; self.requests = []

    async def generate(self, request, **kwargs):
        self.requests.append(request)
        model = request["model"]
        await asyncio.sleep(self.delays.get(model, .01))
        if not self.scripts.get(model): raise AssertionError("Unexpected model call: " + model)
        output = self.scripts[model].popleft()
        if isinstance(output, Exception): raise output
        response = {"id": "response_" + str(len(self.requests)), "status": "completed", "output": output,
                    "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 75}, "output_tokens": 20}}
        return Generation(response)


def setup(tmp_path, scripts, **kwargs):
    store = Store(tmp_path); Agents(store).bootstrap(); Budget(store).grant(12)
    provider = Scripted(scripts, kwargs.pop("delays", None))
    runtime = Runtime(store, provider, **kwargs)
    return store, provider, runtime


@pytest.mark.asyncio
async def test_completed_reply_is_not_repurchased(tmp_path):
    store, provider, runtime = setup(tmp_path, {"gpt-6-astra": [[message("Hello")]]})
    Mailbox(store).post("Hello")
    await runtime.run(seconds=3)
    await Runtime(store, provider).run(seconds=3)
    assert len(provider.requests) == 1 and len(store.events("answer")) == 1


@pytest.mark.asyncio
async def test_delegation_parks_parent_and_sol_writes(tmp_path):
    store, provider, runtime = setup(tmp_path, {
        "gpt-6-astra": [[tool("delegate", {"agent": "builder", "responsibility": "Build demo", "message": "Write the demo"}, "delegate1")], [message("Implemented and inspected")]],
        "gpt-6.1-sol": [[tool("write_file", {"path": "demo.py", "text": "print('demo')\n", "intent": "Implement demo"}, "write1")], [message("Created demo.py")]],
    })
    Mailbox(store).post("Build a demo")
    await runtime.run(seconds=5)
    assert len(provider.requests) == 4
    assert (tmp_path / "work/builder/demo.py").read_text() == "print('demo')\n"
    assert len(store.events("answer")) == 1
    assert all(t["status"] == "completed" for t in runtime.tasks().values())


@pytest.mark.asyncio
async def test_late_recall_is_staged_without_extra_astra_turn(tmp_path):
    store, provider, runtime = setup(tmp_path, {
        "gpt-6-astra": [[message("Current answer")]],
        "gpt-6-luna": [[message("Some older evidence")]],
        "gpt-6.1-terra": [[tool("attention", {"decision": "stage", "reason": "No material correction", "appendix": "Earlier context is available if needed"}, "attention1")]],
    }, delays={"gpt-6-luna": .15})
    Memory(store).publish("owner", "astra", "L2", "prior", "project previous evidence")
    Mailbox(store).post("What about the project?")
    await runtime.run(seconds=5)
    assert [r["model"] for r in provider.requests].count("gpt-6-astra") == 1
    assert len(store.events("appendix.staged", target="astra")) == 1
    assert not store.events("appendix.delivered", target="astra")
    assert len(store.events("answer")) == 1


@pytest.mark.asyncio
async def test_uncertain_model_is_blocked_without_retry(tmp_path):
    store, provider, runtime = setup(tmp_path, {"gpt-6-astra": [ProviderFailure("connection lost")]})
    Mailbox(store).post("Work")
    await runtime.run(seconds=3)
    await Runtime(store, provider).run(seconds=3)
    assert len(provider.requests) == 1
    assert next(iter(runtime.tasks().values()))["status"] == "blocked"
    assert store.events("model.failed")[0]["data"]["uncertain"]


@pytest.mark.asyncio
async def test_restart_reuses_completed_native_final(tmp_path):
    store, provider, runtime = setup(tmp_path, {})
    task = runtime.open("astra", "Question", task="task")
    Context(store).snapshot("astra")
    store.append("task.initialized", {}, target="astra", task=task)
    call = Budget(store).reserve("astra", task, Context(store).request("astra"))
    response = {"output": [message("Already generated")], "usage": {"input_tokens": 10, "output_tokens": 10}}
    store.append("model.completed", {"call": call["id"], "response": store.put(encoded(response)), "purpose": "turn", "usage": response["usage"]}, target="astra", task=task)
    await runtime.run(seconds=3)
    assert not provider.requests
    assert store.events("answer")[0]["data"]["text"] == "Already generated"


@pytest.mark.asyncio
async def test_budget_persists_and_covers_helpers(tmp_path):
    store, provider, runtime = setup(tmp_path, {"gpt-6-astra": [[tool("memory_search", {"query": "none"}, "r1")], [message("Not purchased")]]})
    Budget(store).grant(1)
    Mailbox(store).post("Look")
    await runtime.run(seconds=3)
    assert len(provider.requests) == 1
    assert Budget(store).status()["remaining_calls"] == 0
    assert "budget" in next(iter(runtime.tasks().values()))["error"].lower()
