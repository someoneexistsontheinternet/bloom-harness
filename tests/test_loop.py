import json
import re
import sys

from bloom41.agents import Actor
from bloom41.loop import Loop
from bloom41.models import final_text


def final(text):
    return {
        "type": "message",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": text}],
    }


def call(name, args, ident):
    return {"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": ident}


class Conversation:
    simulated = True

    def __init__(self):
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        raw = json.dumps(request["input"], ensure_ascii=False)
        model = request["model"]
        if model.endswith("terra"):
            latest = request["input"][-1]["content"][0]["text"]
            match = re.search(r"Gather only context useful to ([\w-]+)", latest)
            activity = re.search(r"Monitor the latest turn of ([\w-]+)", latest)
            target = (match or activity).group(1)
            disposition = "stage" if activity else "deliver"
            output = [
                final(
                    f"---\ntarget: {target}\ndisposition: {disposition}\n---\nRelevant context is attached; retain the original question."
                )
            ]
        elif model.endswith("astra"):
            if "The checked result is 4" in raw:
                output = [
                    final(
                        "---\ntarget: user\n---\nI checked the returned evidence: the result is 4."
                    )
                ]
            else:
                output = [
                    call(
                        "create_agent",
                        {
                            "name": "calculator",
                            "responsibility": "Check simple arithmetic with executable evidence",
                        },
                        "create",
                    ),
                    final(
                        "---\ntarget: calculator\n---\nCalculate 2+2 with a command and send me the evidence."
                    ),
                ]
        else:
            results = [
                i
                for i in request["input"]
                if i.get("type") == "function_call_output" and i.get("call_id") == "calculate"
            ]
            if not results:
                output = [call("bash", {"command": "printf '%s' \"$((2+2))\""}, "calculate")]
            else:
                handle = json.loads(results[0]["output"])["job"]
                assert '\\"stdout\\":\\"4\\"' in raw or "stdout" in raw
                output = [
                    call(
                        "send_message",
                        {"recipient": "astra", "additional_content": {"results": [handle]}},
                        "send",
                    ),
                    final(
                        "---\ntarget: astra\n---\nThe checked result is 4. The command output is attached."
                    ),
                ]
        return {"status": "completed", "model": model, "output": output}, []


async def test_common_loop_delivers_real_command_evidence_then_stays_idle(tmp_path):
    provider = Conversation()
    loop = Loop(tmp_path, provider, heartbeat=0.01, trusted_tools=sys.platform != "darwin")
    loop.models.grant(40)
    loop.messages.deliver(
        "user", "astra", "Please check 2+2 using a specialist.", message_id="question"
    )
    result = await loop.run(seconds=8)
    assert not [t for t in result["turns"] if t["status"] == "blocked"], result
    answers = loop.messages.pending("user")
    assert len(answers) == 1, result
    assert "result is 4" in loop.store.read(answers[0]["body"]).decode()
    sol = [a for a in loop.agents.all() if a["role"] == "sol"]
    assert [a["id"] for a in sol] == ["calculator"]
    purchases = len(provider.requests)
    await loop.run(seconds=0.15)
    assert len(provider.requests) == purchases
    assert all(j["status"] == "completed" for j in loop.jobs.states())
    loop.store.close()
    reopened = Loop(tmp_path, provider, heartbeat=0.01, trusted_tools=sys.platform != "darwin")
    await reopened.run(seconds=0.15)
    assert len(provider.requests) == purchases
    assert len(reopened.messages.pending("user")) == 1
    reopened.store.close()


async def test_api_rejects_sol_creation_even_from_custom_tool(tmp_path):
    loop = Loop(tmp_path, Conversation(), heartbeat=0.01, trusted_tools=sys.platform != "darwin")
    loop.agents.create("worker", "sol", "Read fixtures", principal=Actor("astra"))
    root = loop.agents.path("worker")
    (root / "tools/attempt.py").write_text('''async def run(tool):
    """Attempt a forbidden identity change."""
    return await tool.request("create_agent", name="intruder", responsibility="Should not exist")
''')
    await loop.jobs.start()
    try:
        from test_delivery import done

        job = loop.jobs.submit(Actor("worker"), "attempt", {})
        result = await done(loop.jobs, job["id"])
        assert result["status"] == "failed" and "Only continuous Astra" in result["error"]
        assert not loop.store.latest("agent.created", "intruder")
    finally:
        await loop.jobs.close()
        loop.store.close()


async def test_same_identity_fork_uses_exact_seed_and_returns_isolated_work(tmp_path):
    class Forks(Conversation):
        async def generate(self, request):
            if request["model"].endswith("terra"):
                return await super().generate(request)
            self.requests.append(request)
            raw = json.dumps(request["input"])
            branch = any(
                isinstance(i.get("content"), str)
                and i["content"].startswith("Temporary self-fork.")
                for i in request["input"]
            )
            calls = {
                i.get("call_id")
                for i in request["input"]
                if i.get("type") == "function_call_output"
            }
            if branch:
                if "write-branch" not in calls:
                    output = [
                        call(
                            "edit_file",
                            {"path": "branch.txt", "text": "private branch result"},
                            "write-branch",
                        )
                    ]
                elif "read-parent" not in calls:
                    output = [call("read_file", {"path": str(parent_source)}, "read-parent")]
                else:
                    assert "original source" in raw
                    output = [
                        call(
                            "send_message",
                            {
                                "recipient": "worker",
                                "additional_content": {"files": ["branch.txt"]},
                            },
                            "return-branch",
                        ),
                        final("---\ntarget: worker\n---\nThe branch checked the original source."),
                    ]
            elif "The branch checked the original source." in raw:
                assert "private branch result" in raw
                output = [
                    final(
                        "---\ntarget: user\n---\nThe branch result and original evidence were joined."
                    )
                ]
            else:
                output = [
                    call(
                        "fork_self",
                        {"question": "Read the original source and return an isolated note."},
                        "split",
                    )
                ]
            return {"status": "completed", "model": request["model"], "output": output}, []

    provider = Forks()
    loop = Loop(tmp_path, provider, heartbeat=0.01, trusted_tools=sys.platform != "darwin")
    loop.models.grant(40)
    loop.agents.create("worker", "sol", "Inspect retained evidence", principal=Actor("astra"))
    parent_source = loop.agents.path("worker") / "work/original.txt"
    parent_source.write_text("original source")
    loop.messages.deliver(
        "user", "worker", "Use a self-fork to inspect the original source.", message_id="question"
    )
    result = await loop.run(seconds=10)
    assert not [t for t in result["turns"] if t["status"] == "blocked"], result
    assert len(loop.messages.pending("user")) == 1, result
    (branch,) = loop.store.events("branch.created")
    assert branch["data"]["owner"] == "worker"
    parent_call = next(
        r for r in loop.store.events("model.started", "worker") if r["data"]["purpose"] == "agent"
    )
    assert branch["data"]["seed"]["request"] == parent_call["data"]["request"]
    assert not (loop.agents.path("worker") / "work/branch.txt").exists()
    assert (
        loop.agents.path(Actor("worker", branch["subject"])) / "work/branch.txt"
    ).read_text() == "private branch result"
    assert len([a for a in loop.agents.all() if a["role"] == "sol"]) == 1
    loop.store.close()


async def test_new_owner_input_supersedes_unstarted_write(tmp_path):
    class Steering(Conversation):
        calls = 0

        async def generate(self, request):
            if request["model"].endswith("terra"):
                return await super().generate(request)
            self.requests.append(request)
            self.calls += 1
            if self.calls == 1:
                loop.messages.deliver(
                    "user",
                    "astra",
                    "Do not write the file; acknowledge cancellation.",
                    message_id="correction",
                    metadata={"urgent": True},
                )
                output = [
                    call("edit_file", {"path": "obsolete.txt", "text": "obsolete"}, "obsolete")
                ]
            else:
                output = [final("---\ntarget: user\n---\nThe obsolete write was not executed.")]
            return {"status": "completed", "output": output}, []

    provider = Steering()
    loop = Loop(tmp_path, provider, heartbeat=0.01, trusted_tools=sys.platform != "darwin")
    loop.models.grant(20)
    loop.messages.deliver("user", "astra", "Write a file.", message_id="original")
    result = await loop.run(seconds=8)
    assert not [t for t in result["turns"] if t["status"] == "blocked"], result
    assert not (loop.agents.path("astra") / "work/obsolete.txt").exists()
    assert len(loop.messages.pending("user")) == 1
    assert provider.calls == 2
    loop.store.close()


async def test_restart_reuses_completed_owner_response_before_dispatch(tmp_path, monkeypatch):
    from bloom41.demo import DemoProvider

    class Crash(BaseException):
        pass

    provider = DemoProvider()
    loop = Loop(tmp_path, provider, heartbeat=0.01, trusted_tools=sys.platform != "darwin")
    loop.models.grant(40)
    loop.messages.deliver("user", "astra", "Check the arithmetic.", message_id="question")
    dispatch = loop.dispatch

    def interrupt(actor, turn):
        if actor.agent == "astra":
            raise Crash()
        dispatch(actor, turn)

    monkeypatch.setattr(loop, "dispatch", interrupt)
    import pytest

    with pytest.raises(Crash):
        await loop.run(seconds=8)
    assert not [a for a in loop.agents.all() if a["role"] == "sol"]
    loop.store.close()
    reopened = Loop(tmp_path, provider, heartbeat=0.01, trusted_tools=sys.platform != "darwin")
    result = await reopened.run(seconds=8)
    assert not [t for t in result["turns"] if t["status"] == "blocked"], result
    assert len(reopened.messages.pending("user")) == 1
    assert len([r for r in provider.requests if r["model"].endswith("astra")]) == 2
    reopened.store.close()


async def test_demo_terra_uses_latest_request_when_results_arrive_after_it():
    from bloom41.demo import DemoProvider
    from bloom41.models import final_text

    def user(text):
        return {"role": "user", "content": [{"type": "input_text", "text": text}]}

    provider = DemoProvider()
    request = {
        "model": "gpt-6.1-terra",
        "input": [
            user("Gather only context useful to calculator"),
            user("Monitor the latest turn of astra"),
            {"type": "function_call_output", "call_id": "send", "output": "delivered"},
            user("Routine tool result follows the monitoring request."),
        ],
    }
    response, _ = await provider.generate(request)
    assert "target: astra" in final_text(response)
    assert "disposition: stage" in final_text(response)


async def test_queued_action_checks_new_input_even_after_it_was_consumed(tmp_path):
    from bloom41.store import uid

    loop = Loop(tmp_path, Conversation(), trusted_tools=sys.platform != "darwin")
    actor = Actor("astra")
    turn = {"id": "queued-turn", "scope": actor.scope, "input_ids": []}
    loop.store.event("turn.received", actor.scope, {}, key="turn-received:queued-turn")
    loop.save_turn(turn)
    job = loop.jobs.submit(
        actor, "edit_file", {"path": "stale.txt", "text": "stale"}, turn=turn["id"]
    )
    loop.messages.deliver("user", "astra", "Cancel the write", message_id="new-input")
    loop.messages.consume(loop.messages.pending(actor), "new-decision")
    await loop.jobs.start()
    try:
        await loop.jobs.pump()
        assert loop.jobs.get(job["id"])["status"] == "cancelled"
        assert not (loop.agents.path(actor) / "work/stale.txt").exists()
    finally:
        await loop.jobs.close()
        loop.store.close()


async def test_failed_purchase_parks_until_explicit_operator_resume(tmp_path):
    from bloom41.models import ModelFailure

    class Interrupted(Conversation):
        owner_calls = 0

        async def generate(self, request):
            if request["model"].endswith("terra"):
                return await super().generate(request)
            self.owner_calls += 1
            if self.owner_calls == 1:
                raise ModelFailure("Simulated interrupted stream")
            assert "Original question" in json.dumps(request)
            return {
                "status": "completed",
                "output": [final("---\ntarget: user\n---\nRecovered answer")],
            }, []

    provider = Interrupted()
    loop = Loop(tmp_path, provider, heartbeat=0.01, trusted_tools=sys.platform != "darwin")
    loop.models.grant(10)
    loop.messages.deliver("user", "astra", "Original question", message_id="question")
    result = await loop.run(seconds=3)
    assert [t for t in result["turns"] if t["status"] == "blocked"]
    await loop.run(seconds=0.1)
    assert provider.owner_calls == 1
    loop.resume(Actor("astra"), "Inspected failed receipt; request a new decision")
    result = await loop.run(seconds=3)
    assert provider.owner_calls == 2 and len(loop.messages.pending("user")) == 1, result
    assert len(loop.store.events("model.failed")) == 1
    loop.store.close()


async def test_blocked_dispatch_holds_prepared_effect_and_runner_returns(tmp_path):
    import asyncio

    loop = Loop(tmp_path, Conversation(), trusted_tools=sys.platform != "darwin")
    loop.save_turn(
        {
            "id": "blocked-dispatch",
            "scope": "astra",
            "agent": "astra",
            "branch": None,
            "status": "blocked",
            "input_ids": [],
            "error": "Interrupted output preparation",
        }
    )
    job = loop.jobs.submit(
        Actor("astra"),
        "edit_file",
        {"path": "uncommitted.txt", "text": "not admitted"},
        turn="blocked-dispatch",
    )
    await asyncio.wait_for(loop.run(), 1)
    assert loop.jobs.get(job["id"])["status"] == "prepared"
    assert not (loop.agents.path("astra") / "work/uncommitted.txt").exists()
    loop.store.close()


async def test_shutdown_records_terminal_receipts_for_tool_model_consultations(tmp_path):
    import asyncio
    from test_models_context import Scripted

    began = asyncio.Event()

    class Pending(Scripted):
        async def generate(self, request):
            began.set()
            await asyncio.Event().wait()

    loop = Loop(tmp_path, Pending(), trusted_tools=sys.platform != "darwin")
    loop.models.grant(1)
    (loop.agents.path("astra") / "tools/consult.py").write_text('''async def run(tool):
    """Consult a reader while retaining the owning job."""
    return await tool.model("Check this fixture")
''')
    loop.jobs.submit(Actor("astra"), "consult", {})
    runner = asyncio.create_task(loop.run(watch=True))
    await asyncio.wait_for(began.wait(), 3)
    loop.stopping = True
    await asyncio.wait_for(runner, 3)
    assert len(loop.store.events("model.started")) == 1
    assert len(loop.store.events("model.failed")) == 1
    assert not loop.jobs.connections
    loop.store.close()


def test_terra_does_not_merge_distinct_context_continuations_for_the_same_owner(tmp_path):
    loop = Loop(tmp_path, Conversation())
    terra = Actor("terra-astra")
    continuation = {
        "context_for": "astra",
        "request_id": "context-real",
        "original_ids": ["original"],
        "origin": "original",
    }
    activity = {
        "context_for": "astra",
        "request_id": "activity-other",
        "original_ids": [],
        "origin": "original",
    }
    loop.messages.deliver(
        "system",
        terra.scope,
        "Routine activity",
        message_id="activity",
        kind="activity",
        metadata={"context_request": activity},
    )
    loop.messages.deliver(
        "tool:send_message",
        terra.scope,
        "Prepared, now provide the final context note",
        message_id="continuation",
        kind="tool_result",
        metadata={"context_request": continuation},
    )
    first = loop.ready(terra)
    assert [p["id"] for p in first] == ["activity"]
    loop.messages.consume(first, "activity-turn")
    second = loop.ready(terra)
    assert [p["id"] for p in second] == ["continuation"]
    assert second[0]["metadata"]["context_request"]["request_id"] == "context-real"
    loop.store.close()
