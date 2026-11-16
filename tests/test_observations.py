import json
import subprocess
import sys

import pytest

from bloom4.agents import Agents
from bloom4.budget import Budget
from bloom4.mailbox import Mailbox
from bloom4.provider import Generation
from bloom4.runtime import Runtime
from bloom4.store import Store


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["stage", "wake"])
async def test_cli_observations_review_before_owner_and_do_not_repeat(tmp_path, decision):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    Budget(store).grant(3)

    class Provider:
        output_tokens = 1000
        models = []

        async def generate(self, request, **kwargs):
            self.models.append(request["model"])
            if request["model"] == "gpt-6.1-terra":
                assert "Observed marker" in json.dumps(request)
                output = [
                    {
                        "type": "function_call",
                        "name": "attention",
                        "call_id": "review",
                        "arguments": json.dumps(
                            {
                                "decision": decision,
                                "reason": "Fixture judgment",
                                "appendix": "Observed marker",
                            }
                        ),
                    }
                ]
            else:
                assert decision == "wake" and "Observed marker" in json.dumps(request)
                output = [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Reviewed material evidence"}],
                    }
                ]
            return Generation(
                {"output": output, "usage": {"input_tokens": 10, "output_tokens": 10}}
            )

    provider = Provider()
    command = [
        sys.executable,
        "-m",
        "bloom4.cli",
        "--workspace",
        str(tmp_path),
        "observe",
        "Observed marker",
        "--id",
        "stable-observation",
    ]
    for _ in range(2):
        posted = subprocess.run(command, capture_output=True, text=True)
        assert posted.returncode == 0, posted.stderr
        assert json.loads(posted.stdout)["kind"] == "observation"
        await Runtime(store, provider, auto_recall=False).run(seconds=3)
    assert provider.models == ["gpt-6.1-terra"] + (["gpt-6-astra"] if decision == "wake" else [])
    assert len(store.events("evidence.ready")) == 1
    assert len(store.events("answer")) == (1 if decision == "wake" else 0)
    assert not Mailbox(store).pending("astra", "observation")
    assert not Mailbox(store).pending("astra")


def test_observation_task_mismatch_is_quarantined(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    agents.create("builder", "sol", "Maintain a project")
    task = Runtime(store, None).open("builder", "Implement")
    Mailbox(store).post("Wrong recipient", "astra", task=task, kind="observation")
    assert not Mailbox(store).ingest()
    assert "different owner" in store.events("input.rejected")[0]["data"]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "blocked"])
async def test_controller_cannot_bypass_operator_recovery(tmp_path, state):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    Budget(store).grant(2)

    class Provider:
        output_tokens = 1000
        calls = 0

        async def generate(self, request, **kwargs):
            self.calls += 1
            assert request["model"] == "gpt-6.1-terra"
            return Generation(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "attention",
                            "call_id": "wake",
                            "arguments": json.dumps(
                                {
                                    "decision": "wake",
                                    "reason": "Material",
                                    "appendix": "Pending correction",
                                }
                            ),
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 10},
                }
            )

    provider = Provider()
    runtime = Runtime(store, provider, auto_recall=False)
    task = runtime.open("astra", "Paused work")
    store.append("task." + state, {}, target="astra", task=task)
    Mailbox(store).post("Material correction", task=task, kind="observation")
    await runtime.run(seconds=3)
    assert provider.calls == 1
    assert runtime.tasks()[task]["status"] == state
    assert len([t for t in runtime.tasks().values() if t["agent"] == "astra"]) == 1
    assert len(runtime.pending_appendices("astra")) == 1
