import asyncio
import json
import sys

import pytest

from bloom41.agents import Actor, Agents
from bloom41.delivery import Delivery
from bloom41.evidence import NumberedSource
from bloom41.jobs import Jobs
from bloom41.loop import Loop
from bloom41.models import BudgetExceeded, ModelFailure
from bloom41.store import Store
from test_delivery import done
from test_models_context import Scripted, reply


def test_reader_view_covers_prefix_gaps_and_suffix_without_inventing_missing_summaries():
    text = "".join(f"Line {n}\r\n" for n in range(1, 11))
    window = NumberedSource.index(text).window(0, len(text))
    result = {
        "summary": "Two relevant excerpts",
        "passages": [{"start_line": 3, "end_line": 4}, {"start_line": 7, "end_line": 7}],
        "summaries": [
            {"start_line": 1, "end_line": 2, "summary": "Introduction"},
            {"start_line": 5, "end_line": 6, "summary": "Intervening context"},
            {"start_line": 8, "end_line": 10, "summary": "Remaining context"},
        ],
    }
    view = window.view(result, "source:fixture")
    assert view["summary_complete"]
    assert [s["kind"] for s in view["sections"]] == [
        "summary",
        "quote",
        "summary",
        "quote",
        "summary",
    ]
    for quote in view["passages"]:
        assert quote["text"] == text[quote["start_char"] : quote["end_char"]]
    result["summaries"].pop(1)
    missing = window.view(result, "source:fixture")
    assert not missing["summary_complete"]
    assert missing["sections"][2]["kind"] == "unavailable_summary"
    assert missing["passages"] == view["passages"]


async def test_reader_windows_start_independently_and_preserve_requested_character_bounds(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    text = (
        "Prefix excluded\n" + ("Original material remains on disk.\n" * 8000) + "Suffix excluded\n"
    )
    path = agents.path("astra") / "work/source.txt"
    path.write_text(text)
    left, right = len("Prefix excluded\n"), len(text) - len("Suffix excluded\n")
    document = NumberedSource.index(text)
    expected = sum(w.start < right for w in document.windows(left, 32_000))
    assert expected > 1
    arrived, ready = [], asyncio.Event()

    async def reader(actor, job, args):
        metadata = json.loads(args["request"][1]["text"])
        assert metadata["question"] == "What exact material remains?"
        assert "Prefix excluded" not in args["request"][-1]["text"]
        assert "Suffix excluded" not in args["request"][-1]["text"]
        arrived.append(metadata)
        if len(arrived) == expected:
            ready.set()
        await asyncio.wait_for(ready.wait(), 3)
        first, last = metadata["first_line"], metadata["last_line"]
        return {
            "text": json.dumps(
                {
                    "relevant": True,
                    "summary": "Retained material",
                    "passages": [{"start_line": first, "end_line": first}],
                    "summaries": [
                        {
                            "start_line": first + 1,
                            "end_line": last,
                            "summary": "Repeated original material",
                        }
                    ]
                    if first < last
                    else [],
                }
            ),
            "call_id": f"window-{first}",
        }

    jobs = Jobs(store, {"model": reader}, trusted_tools=sys.platform != "darwin")
    jobs.handlers.update(Delivery(store, jobs).handlers())
    await jobs.start()
    try:
        job = jobs.submit(
            Actor("astra"),
            "extract",
            {
                "path": str(path),
                "question": "What exact material remains?",
                "start_char": left,
                "end_char": right,
            },
        )
        terminal = await done(jobs, job["id"])
        assert terminal["status"] == "completed", terminal
        value = store.json(terminal["result"])
        assert len(arrived) == expected == len(value["calls"])
        assert all(c["status"] == "read" and c["summary_complete"] for c in value["coverage"])
        assert value["coverage"][0]["start_char"] == left
        assert value["coverage"][-1]["end_char"] == right
        assert all(q["text"] == text[q["start_char"] : q["end_char"]] for q in value["passages"])
    finally:
        await jobs.close()
        store.close()


async def test_identical_readers_reuse_one_purchase_across_forks_and_reopen(tmp_path):
    provider = Scripted(reply("Selected exact evidence"))
    loop = Loop(tmp_path, provider)
    branch = loop.agents.fork(Actor("astra"), "child", {"input": []}, "Independent reading")
    a = loop.jobs.submit(
        Actor("astra"), "extract", {"path": "memory/L1.md", "question": "question"}
    )
    b = loop.jobs.submit(branch, "extract", a["arguments"])
    args = {"request": "The exact same source and question", "ordinal": 1, "profile": "luna"}
    loop.models.grant(1)
    x, y = await asyncio.gather(
        loop.model_api(Actor("astra"), a, args), loop.model_api(branch, b, args)
    )
    assert x["text"] == y["text"] and x["call_id"] == y["call_id"]
    assert len(provider.requests) == 1
    assert len(loop.store.events("reader.reused")) == 1
    loop.store.close()
    reopened = Loop(tmp_path, provider)
    c = reopened.jobs.submit(Actor("astra"), "extract", a["arguments"])
    z = await reopened.model_api(Actor("astra"), c, args)
    assert z["call_id"] == x["call_id"] and len(provider.requests) == 1
    assert len(reopened.store.events("model.started")) == 1
    reopened.store.close()


async def test_uncertain_reader_purchase_is_not_retried_by_a_new_job(tmp_path):
    class Uncertain:
        simulated = True
        calls = 0

        async def generate(self, request):
            self.calls += 1
            raise ModelFailure("Connection lost after acceptance")

    provider = Uncertain()
    loop = Loop(tmp_path, provider)
    loop.models.grant(3)
    args = {"request": "Identical source", "ordinal": 1}
    for _ in range(2):
        job = loop.jobs.submit(Actor("astra"), "extract", {"path": "memory/L1.md", "question": "q"})
        with pytest.raises(ModelFailure):
            await loop.model_api(Actor("astra"), job, args)
    assert provider.calls == len(loop.store.events("model.started")) == 1
    loop.store.close()


async def test_unpurchased_reader_plan_does_not_misattribute_a_later_purchase(tmp_path):
    loop = Loop(tmp_path, Scripted())
    first = loop.jobs.submit(Actor("astra"), "extract", {"path": "memory/L1.md", "question": "q"})
    args = {"request": "Same read", "ordinal": 1}
    with pytest.raises(BudgetExceeded):
        await loop.model_api(Actor("astra"), first, args)
    loop.models.grant(1)
    second = loop.jobs.submit(Actor("astra"), "extract", first["arguments"])
    await loop.model_api(Actor("astra"), second, args)
    assert loop.store.events("reader.sources")[0]["data"]["job"] == second["id"]
    loop.store.close()
