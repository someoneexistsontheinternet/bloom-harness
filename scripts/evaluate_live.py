"""Explicitly budgeted live checks against an isolated synthetic workspace.

Run through the project interpreter. The workspace's existing allowance is
reused across cases; this script never replenishes a consumed allowance.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from bloom4.agents import Agents
from bloom4.budget import Budget
from bloom4.mailbox import Mailbox
from bloom4.provider import Responses
from bloom4.runtime import Runtime
from bloom4.store import Store


async def main(args):
    receipt_dir = Path(args.workspace).parent
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / (args.case + "-process.json")).write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "start": subprocess.check_output(
                    ["ps", "-p", str(os.getpid()), "-o", "lstart="], text=True
                ).strip(),
                "command": "evaluate_live.py --case " + args.case,
                "workspace": args.workspace,
                "module": __import__("bloom4").__file__,
            },
            indent=2,
        )
    )
    store = Store(args.workspace)
    agents = Agents(store)
    agents.bootstrap()
    if not Budget(store).status()["grant"]:
        Budget(store).grant(12, output_tokens=49152, input_tokens=1_000_000)
    provider = Responses(
        kind="codex",
        transport="websocket" if args.case in ("steering", "controller", "compact") else "http",
        timeout=90,
        output_tokens=4096,
    )
    runtime = Runtime(store, provider, allow_processes=True, auto_recall=False)
    before = len(store.events("model.started"))
    if args.case == "reader":
        agents.create("reader", "luna", "Read synthetic integration evidence", parent="astra")
        (agents.scope("astra") / "sentinel.txt").write_text("bloom-v4-synthetic-42\n")
        request = Mailbox(store).post(
            "Use read_file to read sentinel.txt, then report its exact marker. This is a tiny read-only check.",
            "reader",
        )
    elif args.case == "owner":
        request = Mailbox(store).post(
            "Delegate to persistent Sol named builder, responsibility 'Maintain a synthetic arithmetic example'. "
            "Have it write add.py with add(a,b) returning their sum, then run one small assertion with the project Python "
            + sys.executable
            + ". Wait for its evidence, then give a concise final report. Do not create further agents, "
            "browse, install packages or write permanent memory for this synthetic test.",
            "astra",
        )
    elif args.case == "steering":
        agents.create(
            "steering_probe",
            "luna",
            "Test urgent steering with no external effects",
            parent="astra",
        )
        request = Mailbox(store).post(
            "Think through how to organize a short CLI manual, then give a short outline. Do not use tools.",
            "steering_probe",
        )
    elif args.case == "controller":
        parent = store.events("answer", target="astra")[-1]["task"]
        evidence = store.append(
            "evidence.ready",
            {
                "summary": "An unrelated archived scratch note uses different spelling. It changes no arithmetic result or obligation.",
                "sources": [],
                "status": "complete",
            },
            target="astra",
            task=parent,
        )
        task = runtime.open(
            "terra",
            "The owner already received the correct arithmetic result. A late note about spelling in an unrelated scratch file changes nothing. "
            "There is enough evidence here; do not retrieve anything. Use attention exactly once to stage a brief appendix with a reason. Do not wake Astra.",
            task="live_controller_check",
            purpose="attention",
            evidence=evidence["id"],
            read_owner="astra",
            origin="test",
            parent_task=parent,
        )
        request = {"task": task}
    elif args.case == "compact":
        request = {"task": "live_compaction_check"}
    else:
        raise ValueError(args.case)
    work = asyncio.create_task(
        runtime.compact("astra", request["task"])
        if args.case == "compact"
        else runtime.run(seconds=180)
    )
    if args.case == "steering":
        deadline = time.monotonic() + 20
        while not runtime.active and not work.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        Mailbox(store).post(
            "Urgent correction: make the entire final reply exactly STEERING_CONFIRMED. Do not use tools.",
            "steering_probe",
            urgent=True,
        )
    await work
    result = runtime.status()
    answers = [e for e in store.events("answer") if e["task"] == request["task"]]
    errors = [t for t in result["tasks"] if t["status"] == "blocked"]
    receipt = {
        "case": args.case,
        "module": __import__("bloom4").__file__,
        "new_attempts": len(store.events("model.started")) - before,
        "budget": result["budget"],
        "answers": [e["id"] for e in answers],
        "blocked": [{"id": t["id"], "error": t.get("error")} for t in errors],
        "steered_inputs": len(store.events("input.steered")),
    }
    if args.case == "reader":
        receipt["verified"] = bool(
            answers and "bloom-v4-synthetic-42" in answers[-1]["data"]["text"]
        )
    elif args.case == "owner":
        receipt["verified"] = bool(
            answers
            and (store.root / "work/builder/add.py").is_file()
            and any(e["data"].get("exit_code") == 0 for e in store.events("process.settled"))
        )
    elif args.case == "steering":
        receipt["verified"] = bool(
            answers
            and answers[-1]["data"]["text"].strip() == "STEERING_CONFIRMED"
            and store.events("input.steered")
        )
    elif args.case == "controller":
        receipt["verified"] = any(
            e["data"].get("decision") == "stage" and e["data"].get("evidence") == evidence["id"]
            for e in store.events("appendix.staged", target="astra")
        )
    else:
        snapshots = store.events("context.snapshot", target="astra")
        receipt["verified"] = bool(
            snapshots[-1]["data"].get("timeline")
            and not any(i.get("type") == "function_call" for i in runtime.context.items("astra"))
        )
        receipt["timeline"] = snapshots[-1]["data"].get("timeline")
    output = Path(args.workspace).parent / (args.case + "-result.json")
    output.write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt, indent=2))
    store.close()
    return 0 if receipt["verified"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument(
        "--case", choices=["reader", "owner", "steering", "controller", "compact"], required=True
    )
    raise SystemExit(asyncio.run(main(parser.parse_args())))
