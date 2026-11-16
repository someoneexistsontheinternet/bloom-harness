"""A no-model end-to-end demo: scripted decisions, real files and process receipts."""

import asyncio
import json
import sys

from .agents import Agents, PROFILES
from .budget import Budget
from .mailbox import Mailbox
from .memory import Memory
from .provider import Generation, public_text
from .runtime import Runtime


def text(value):
    return {
        "type": "message",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": value}],
    }


def tool(name, arguments, call_id):
    return {
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(arguments),
        "call_id": call_id,
    }


class DemoProvider:
    """Supply clearly labeled synthetic model outputs, with no fabricated token usage."""

    simulated = True
    output_tokens = 512

    def __init__(self):
        self.counts = {}

    async def generate(self, request, **kwargs):
        role = next(
            role for role, profile in PROFILES.items() if profile["model"] == request["model"]
        )
        count = self.counts.get(role, 0)
        self.counts[role] = count + 1
        await asyncio.sleep(0.1 if role == "luna" else 0.01)
        if role == "astra" and count == 0:
            output = [
                tool(
                    "delegate",
                    {
                        "agent": "demo_builder",
                        "responsibility": "Maintain the arithmetic demonstration",
                        "message": "Create add.py and verify it with a real Python assertion",
                    },
                    "demo_delegate",
                )
            ]
        elif role == "sol" and count == 0:
            output = [
                tool(
                    "write_file",
                    {
                        "path": "add.py",
                        "text": 'def add(a, b):\n    """Return the sum of two values."""\n    return a + b\n',
                        "intent": "Create the arithmetic example for this demonstration",
                    },
                    "demo_write",
                ),
                tool(
                    "run_process",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "from add import add; assert add(2, 3) == 5; print('demo arithmetic passed')",
                        ],
                        "intent": "Verify the actual written function",
                    },
                    "demo_process",
                ),
            ]
        elif role == "sol":
            if "demo arithmetic passed" not in json.dumps(request["input"]):
                raise AssertionError("The real process result has not arrived")
            output = [text("Real Python process passed; add.py is ready.")]
        elif role == "luna":
            output = [
                text(
                    "Earlier demonstration notes exist but add no necessary evidence to this implementation."
                )
            ]
        elif role == "terra":
            output = [
                tool(
                    "attention",
                    {
                        "decision": "stage",
                        "reason": "No material correction or unresolved requirement",
                        "appendix": "Earlier demo notes are available if needed; omitted in full as supplementary.",
                    },
                    "demo_attention",
                )
            ]
        else:
            output = [
                text(
                    "SIMULATED AGENT: the real file was written and the real Python assertion passed. The worker and evidence histories are retained."
                )
            ]
        response = {
            "id": "demo_" + role + "_" + str(count),
            "model": "simulated/" + role,
            "status": "completed",
            "output": output,
        }
        if kwargs.get("on_text") and public_text(response):
            kwargs["on_text"](public_text(response))
        return Generation(response)


async def run_demo(store, output=None):
    """Exercise the complete loop in a fresh workspace without contacting a provider."""
    if any(e["kind"] != "agent.created" for e in store.events()):
        raise ValueError("Use a new workspace for the simulated demo")
    store.append(
        "workspace.simulation",
        {"note": "Scripted model decisions; real local effects; no API calls"},
        actor="owner",
    )
    Agents(store).bootstrap()
    Budget(store).grant(8, output_tokens=16384)
    Memory(store).publish(
        "owner", "astra", "L2", "demo_notes", "Historical demonstration note: use exact evidence."
    )
    Mailbox(store).post("Create an arithmetic demonstration and verify it.")
    runtime = Runtime(store, DemoProvider(), allow_processes=True, output=output)
    await runtime.run(seconds=15)
    jobs = store.events("process.settled")
    verified = bool(store.events("answer") and jobs and jobs[-1]["data"].get("exit_code") == 0)
    return {
        "simulation": True,
        "api_calls": 0,
        "verified": verified,
        "workspace": str(store.root),
        "file": str(store.root / "work/demo_builder/add.py"),
        **runtime.status(),
    }
