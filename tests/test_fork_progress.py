"""One lead retains the investigation while its independent forks return evidence."""

import asyncio
import json
import sys

from bloom41.agents import Actor
from bloom41.demo import DemoProvider, evidence
from bloom41.loop import Loop
from test_loop import call, final


async def test_lead_progresses_while_a_fork_runs_then_joins_both_under_one_identity(tmp_path):
    alpha_started, parent_moved = asyncio.Event(), asyncio.Event()
    parent_turns = 0
    overlap = False
    terra = DemoProvider()

    class Provider:
        simulated = True

        async def generate(self, request):
            nonlocal parent_turns, overlap
            model = request["model"]
            raw = json.dumps(request["input"])
            if model.endswith("terra"):
                return await terra.generate(request)
            if model.endswith("astra"):
                output = (
                    [final("---\ntarget: user\n---\nBoth independent results reached the lead.")]
                    if "Joined ALPHA and BETA" in raw
                    else [
                        call(
                            "create_agent",
                            {
                                "name": "researcher",
                                "responsibility": "Own the complete investigation",
                            },
                            "create",
                        ),
                        final(
                            "---\ntarget: researcher\n---\nCheck ALPHA and BETA independently and reconcile the evidence."
                        ),
                    ]
                )
            elif "Temporary self-fork. Investigate only: ALPHA" in raw:
                alpha_started.set()
                await asyncio.wait_for(parent_moved.wait(), 6)
                output = [final("---\ntarget: researcher\n---\nALPHA verified independently.")]
            elif "Temporary self-fork. Investigate only: BETA" in raw:
                async with asyncio.timeout(6):
                    while not any(
                        j["name"] == "bash" and j["status"] == "completed"
                        for j in loop.jobs.states()
                    ):
                        await asyncio.sleep(0.001)
                output = [final("---\ntarget: researcher\n---\nBETA verified independently.")]
            else:
                parent_turns += 1
                if parent_turns == 1:
                    output = [
                        call("fork_self", {"question": "ALPHA"}, "alpha"),
                        call("fork_self", {"question": "BETA"}, "beta"),
                        call(
                            "bash",
                            {"command": "printf 'parent evidence' > parent.txt; cat parent.txt"},
                            "work",
                        ),
                    ]
                elif parent_turns == 2:
                    await asyncio.wait_for(alpha_started.wait(), 6)
                    assert any(
                        isinstance(v, dict) and v.get("stdout") == "parent evidence"
                        for v in evidence(request)
                    )
                    assert "ALPHA verified independently." not in raw
                    overlap = True
                    parent_moved.set()
                    async with asyncio.timeout(6):
                        while (
                            sum(
                                e["data"]["recipient"] == "researcher"
                                and e["data"]["sender"].startswith("branch_")
                                and bool(loop.store.by_key("message-delivered:" + e["subject"]))
                                for e in loop.store.events("message.prepared")
                            )
                            < 2
                        ):
                            await asyncio.sleep(0.001)
                    output = [call("finish_independent", {}, "independent")]
                else:
                    assert "ALPHA verified independently." in raw
                    assert "BETA verified independently." in raw
                    output = [
                        final(
                            "---\ntarget: astra\n---\nJoined ALPHA and BETA with the parent evidence."
                        )
                    ]
            return {"status": "completed", "output": output}, []

    loop = Loop(tmp_path, Provider(), heartbeat=0.01, trusted_tools=sys.platform != "darwin")
    loop.models.grant(30)
    # Preexisting domain owner: creation must reuse its identity and tools.
    loop.agents.create(
        "researcher", "sol", "Own the complete investigation", principal=Actor("astra")
    )
    root = loop.agents.path("researcher")
    (root / "tools/finish_independent.py").write_text('''NOTIFY_ON_SUCCESS = False
async def run(tool):
    """Verify independent parent work without a receipt-polling turn."""
    assert tool.path("parent.txt").read_text() == "parent evidence"
    tool.path("verified.txt", write=True).write_text("parent verified")
    return {"verified": True}
''')
    loop.messages.deliver("user", "astra", "Investigate both claims", message_id="question")
    result = await loop.run(seconds=12)
    assert not [t for t in result["turns"] if t["status"] == "blocked"], result
    assert overlap and parent_turns == 3
    assert (root / "work/verified.txt").read_text() == "parent verified"
    assert [a["id"] for a in loop.agents.all() if a["role"] == "sol"] == ["researcher"]
    branches = loop.store.events("branch.created")
    assert len(branches) == 2 and all(b["data"]["owner"] == "researcher" for b in branches)
    assert len(loop.messages.pending("user")) == 1
    calls = len(loop.store.events("model.started"))
    await loop.run(seconds=0.1)
    assert len(loop.store.events("model.started")) == calls
    loop.store.close()
