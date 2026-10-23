"""Opt-in isolated cache A/B or configured Luna retrieval acceptance.

Select the code under test through BLOOM_SOURCE_ROOT/PYTHONPATH. A shared budget
file caps all sequential cases at eight purchases / 200k estimated input tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from bloom.config import Config
from bloom.context import Context
from bloom.models import Models
from bloom.runtime import Runtime
from bloom.store import Store, atomic, encoded, now


class CappedModels(Models):
    def __init__(self, store, budget, count):
        super().__init__(store)
        self.budget, self.count = budget, count

    async def infer(self, agent, task, instructions, items, tools, **kwargs):
        ledger = (
            json.loads(self.budget.read_text())
            if self.budget.exists()
            else {"calls": 0, "estimated_input": 0}
        )
        estimate = self.count([instructions, items, tools])
        if ledger["calls"] >= 8 or ledger["estimated_input"] + estimate > 200000:
            raise RuntimeError("Combined evaluation purchase/input cap reached")
        ledger["calls"] += 1
        ledger["estimated_input"] += estimate
        atomic(self.budget, encoded(ledger))
        return await super().infer(
            agent, task, instructions, items, tools, **{**kwargs, "retry_limit": 0}
        )


def json_answer(response):
    text = "\n".join(
        p["text"]
        for item in response["output"]
        if item.get("type") == "message"
        for p in item.get("content", [])
        if "text" in p
    )
    fence = chr(96) * 3
    return json.loads(
        text.strip().removeprefix(fence + "json").removeprefix(fence).removesuffix(fence).strip()
    )


async def evaluate(args):
    source = Path(os.environ["BLOOM_SOURCE_ROOT"]).resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.output / "project"
    root.mkdir()
    for name in ("config", "prompts"):
        shutil.copytree(source / name, root / name, ignore=shutil.ignore_patterns("local.toml"))
    (root / ".venv").symlink_to(args.project / ".venv", target_is_directory=True)
    config = Config(
        root,
        {
            "discord": {"enabled": False},
            "storage": {"enabled": False},
            "memory": {"retrieval_enabled": args.case == "recall", "retrieval_turns": 3},
        },
    )
    for provider in config.data["providers"].values():
        provider["max_retries"] = 0
    store = Store(config)
    context = Context(store, None)
    models = CappedModels(store, args.budget, context.count)
    context.models = models
    context.history.models = models
    runtime = Runtime(store, models)
    report = {
        "started_at": now(),
        "case": args.case,
        "root": str(root),
        "source": str(source),
        "source_hashes": {
            str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((source / "src/bloom").rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
        },
        "checks": {},
        "discord_enabled": False,
        "storage_enabled": False,
    }
    try:
        marker = "iris-" + uuid.uuid4().hex[:10]
        if args.case == "prefix":
            task = store.new_task(
                'This is a bounded local cache-probe validation. Read the supplied calibration reference and return only JSON {"marker":"the exact marker"}. No tools, explanations or external work. The cache-probe phase may advance; the reference marker stays the same.'
            )
            store.status(task, "running")
            reference = (
                "Calibration cache-probe reference table; the exact marker is " + marker + "\n"
            )
            reference += "\n".join(
                f"Calibration entry {i:04d}: channel {i % 19:02d}; sample {(i * 917) % 10000:04d}; reference group {i % 37:02d}."
                for i in range(800)
            )
            store.item(
                "astra",
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": reference}],
                },
                task,
            )
            requests = []
            calls = []
            for step in range(2):
                tools = runtime.tools.schemas("astra")
                instructions, items = await context.compose("astra", task, tools)
                call, response = await models.infer(
                    "astra", task, instructions, items, tools, item_ids=context.members["astra"]
                )
                assert json_answer(response) == {"marker": marker}
                assert not any(x.get("type") == "function_call" for x in response["output"])
                row = store.one("SELECT * FROM model_calls WHERE id=?", (call,))
                requests.append(store.get(row["request_hash"]))
                calls.append(call)
                runtime.ingest_response(task, call, response)
                if step == 0:
                    store.db.execute(
                        "UPDATE tasks SET plan=? WHERE id=?",
                        ("Cache-probe phase 1 checked; finish phase 2.", task),
                    )
                    store.item(
                        "astra",
                        {
                            "role": "user",
                            "content": "Cache-probe progress: phase 1 completed. Phase 2 checks the same reference; return the same exact marker as JSON. No tools or other work.",
                        },
                        task,
                        origin="runtime_notice",
                    )
                    # Reopen the context assembler; prefix identity is durable state.
                    context = Context(store, models)
            shared = 0
            for first, second in zip(requests[0]["input"], requests[1]["input"]):
                if first != second:
                    break
                shared += 1
            report["checks"] = {
                "correct_answers": True,
                "stable_instructions": requests[0]["instructions"] == requests[1]["instructions"],
                "stable_tools": requests[0]["tools"] == requests[1]["tools"],
                "entire_prior_input_preserved": shared == len(requests[0]["input"]),
            }
            report["request_comparison"] = {
                "matching_items": shared,
                "previous_items": len(requests[0]["input"]),
                "previous_input_estimated_tokens": context.count(requests[0]["input"]),
                "matching_items_estimated_tokens": context.count(requests[0]["input"][:shared]),
                "call_ids": calls,
            }
        else:
            original_schemas = runtime.tools.schemas
            runtime.tools.schemas = lambda agent: (
                [] if agent == "astra" else original_schemas(agent)
            )
            old = store.new_task(
                "Historical orchid calibration: the exact marker is " + marker + "."
            )
            store.status(old, "completed")
            original = store.one("SELECT id FROM items WHERE task_id=?", (old,))["id"]
            # Deliberately hide the original behind a fixture compaction boundary.
            # This is a retrieval test, not a claim of newly measured compaction.
            summary = store.item(
                "astra",
                {
                    "role": "user",
                    "content": "Fixture memory boundary: an older orchid calibration exists. Use memory tools for its exact marker.",
                },
                old,
                origin="compaction",
            )
            store.db.execute(
                "INSERT INTO contexts VALUES(?,?,?,?,?,?,?)",
                ("fixture-boundary", "astra", None, summary, original, None, now()),
            )
            store.db.execute(
                "INSERT INTO context_members VALUES(?,?,0)", ("fixture-boundary", summary)
            )
            task = store.new_task(
                'For this bounded memory-retrieval validation, recover the exact historical orchid calibration marker. Ask the retrieval worker to read exact evidence. Reply only JSON {"marker":"the exact marker"}; no external work or additional tools after the retrieval report.'
            )
            store.status(task, "running")
            runtime.recall.start(
                task,
                "explicit-evaluation-recall",
                "Recover the exact orchid calibration marker.",
                "Read the historical original and report its exact marker with a verified quote.",
            )
            job = store.one("SELECT * FROM memory_retrievals")
            while (
                store.one("SELECT status FROM tasks WHERE id=?", (job["task_id"],))["status"]
                == "ready"
            ):
                await runtime.step(job["task_id"])
            runtime.recall.flush(task)
            delivered = store.one(
                "SELECT text FROM items WHERE id=(SELECT report_item FROM memory_retrievals)"
            )
            value = json.loads(delivered["text"]) if delivered else {}
            assert value.get("status") == "found" and any(
                marker in s["quote"] for s in value["sources"]
            ), value
            await runtime.step(task)
            final = store.one(
                "SELECT envelope_hash FROM items WHERE task_id=? AND kind='message' AND role='assistant' ORDER BY id DESC LIMIT 1",
                (task,),
            )
            assert json_answer({"output": [store.get(final["envelope_hash"])]}) == {
                "marker": marker
            }
            assert (
                store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "completed"
            )
            report["checks"] = {
                "configured_luna_exact_retrieval": True,
                "source_quotes_verified": True,
                "astra_used_report": True,
                "explicit_fixture_assignment": True,
                "worker_is_read_only": {x["name"] for x in runtime.tools.schemas(job["agent_id"])}
                == {"memory_search", "memory_read"},
            }
        report["accepted"] = True
    except BaseException as exc:
        from bloom.privacy import clean

        report.update(accepted=False, error=clean(str(exc)), error_type=type(exc).__name__)
        raise
    finally:
        report["completed_at"] = now()
        report["calls"] = store.rows(
            "SELECT id,agent_id,purpose,model_requested,status,input_tokens,cached_tokens,output_tokens,error FROM model_calls ORDER BY rowid"
        )
        report["budget"] = json.loads(args.budget.read_text()) if args.budget.exists() else {}
        atomic(args.output / "result.json", encoded(report))
        store.close()
        print(
            json.dumps(
                {
                    k: report.get(k)
                    for k in ("case", "accepted", "checks", "calls", "budget", "error")
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("prefix", "recall"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("Configured inference requires --live")
    asyncio.run(evaluate(args))
