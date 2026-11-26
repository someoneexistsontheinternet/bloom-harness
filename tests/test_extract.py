import json
import sys

from bloom41.agents import Actor, Agents
from bloom41.evidence import NumberedSource
from bloom41.jobs import Jobs
from bloom41.store import Store
from test_delivery import done


def test_numbered_dense_unicode_source_preserves_ranges_and_crlf():
    original = "First\r\nSecond — café\r\n" + json.dumps({"values": list(range(1000))})
    document = NumberedSource.index(original)
    windows = list(document.windows(0, 100))
    assert "".join(original[w.start : w.end] for w in windows) == original
    for window in windows:
        quotes, invalid, _ = window.selections(
            [{"start_line": window.first, "end_line": window.last}], "fixture"
        )
        assert not invalid
        for q in quotes:
            assert q["text"] == original[q["start_char"] : q["end_char"]]


async def test_standalone_extract_reads_exact_source_through_scoped_model_api(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    actor = Actor("astra")
    source = agents.path(actor) / "work/filing.txt"
    source.write_bytes(b"Heading\r\nThe agreement is conditional.\r\nNo payment received.\r\n")
    requests = []

    async def model(principal, job, args):
        requests.append(args)
        assert principal == actor and args["profile"] == "luna"
        assert "2 | The agreement is conditional." in args["request"][-1]["text"]
        return {
            "text": json.dumps(
                {
                    "relevant": True,
                    "summary": "Conditional agreement, no received payment.",
                    "passages": [{"start_line": 2, "end_line": 3}],
                }
            ),
            "call_id": "fixture-luna",
        }

    jobs = Jobs(store, {"model": model}, trusted_tools=sys.platform != "darwin")
    await jobs.start()
    try:
        job = jobs.submit(
            actor, "extract", {"path": "filing.txt", "question": "What is actually committed?"}
        )
        result = await done(jobs, job["id"])
        assert result["status"] == "completed", result
        value = store.json(result["result"])
        assert len(requests) == 1
        assert (
            value["passages"][0]["text"] == "The agreement is conditional.\r\nNo payment received."
        )
        assert value["source_sha256"]
    finally:
        await jobs.close()
        store.close()
