"""CLI inspection and filesystem input publishing; gateways are deliberately separate."""

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from .agents import Agents
from .context import Context
from .memory import Memory
from .budget import Budget
from .provider import Responses
from .runtime import Runtime
from .mailbox import Mailbox
from .store import Store


def main():
    parser = argparse.ArgumentParser(prog="bloom4")
    parser.add_argument("--workspace", default=".bloom")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("demo")
    post = sub.add_parser("post")
    post.add_argument("text")
    post.add_argument("--to", default="astra")
    post.add_argument("--urgent", action="store_true")
    post.add_argument("--id")
    observe = sub.add_parser("observe")
    observe.add_argument("text")
    observe.add_argument("--to", default="astra")
    observe.add_argument("--task")
    observe.add_argument("--id")
    observe.add_argument("--source", action="append", default=[])
    sub.add_parser("ingest")
    timeline = sub.add_parser("timeline")
    timeline.add_argument("--agent")
    timeline.add_argument("--task")
    timeline.add_argument("--kind")
    timeline.add_argument("--limit", type=int, default=50)
    timeline.add_argument("--full", action="store_true")
    search = sub.add_parser("search")
    search.add_argument("query")
    show = sub.add_parser("show")
    show.add_argument("event")
    sub.add_parser("reindex")
    source = sub.add_parser("source")
    source.add_argument("event")
    source.add_argument("--field", default="source")
    export = sub.add_parser("export-call")
    export.add_argument("event")
    export.add_argument("--to", type=Path, required=True)
    agent = sub.add_parser("agent")
    agent.add_argument("action", choices=["list", "create", "fork"])
    agent.add_argument("name", nargs="?")
    agent.add_argument("--role", choices=["astra", "sol", "terra", "luna"], default="sol")
    agent.add_argument("--responsibility", default="")
    agent.add_argument("--from-agent", default="astra")
    memory = sub.add_parser("memory")
    memory.add_argument("action", choices=["show", "publish"])
    memory.add_argument("--agent", default="astra")
    memory.add_argument("--layer", choices=["L0", "L1", "L2"], default="L1")
    memory.add_argument("--key", default="working")
    memory.add_argument("--file", type=Path)
    memory.add_argument("--expected", type=int, default=0)
    budget = sub.add_parser("budget")
    budget.add_argument("--calls", type=int)
    budget.add_argument("--output-tokens", type=int, default=131072)
    budget.add_argument("--input-tokens", type=int, default=8_000_000)
    sub.add_parser("status")
    usage = sub.add_parser("usage")
    usage.add_argument("--task")
    for name in ("run", "ask", "chat", "compact"):
        run = sub.add_parser(name)
        if name == "ask":
            run.add_argument("text")
        run.add_argument("--to", default="astra")
        run.add_argument("--provider", choices=["codex", "openai"], default="codex")
        run.add_argument("--transport", choices=["http", "websocket"], default="http")
        run.add_argument("--output-reserve", type=int, default=32768)
        run.add_argument("--timeout", type=float, default=180)
        run.add_argument("--seconds", type=float)
        run.add_argument("--allow-processes", action="store_true")
        run.add_argument("--no-auto-recall", action="store_true")
        run.add_argument("--watch", action="store_true")
    for name in ("pause", "resume"):
        control = sub.add_parser(name)
        control.add_argument("task")
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("call")
    reconcile.add_argument("--reason", required=True)
    cancel = sub.add_parser("cancel-process")
    cancel.add_argument("handle")
    archive = sub.add_parser("archive")
    archive.add_argument("--destination", type=Path)
    archive.add_argument("--days", type=float, default=7)
    backup = sub.add_parser("backup")
    backup.add_argument("path", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("path", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "restore":
        from .storage import restore

        print(json.dumps(restore(args.path, args.destination), indent=2))
        return
    store = Store(args.workspace)
    try:
        match args.command:
            case "init":
                Agents(store).bootstrap()
                result = {"workspace": str(store.root), "events": len(store.events())}
            case "demo":
                from .demo import run_demo

                result = asyncio.run(run_demo(store))
            case "post":
                result = Mailbox(store).post(
                    args.text, args.to, urgent=args.urgent, request_id=args.id
                )
            case "ingest":
                result = Mailbox(store).ingest()
            case "observe":
                result = Mailbox(store).post(
                    args.text,
                    args.to,
                    request_id=args.id,
                    task=args.task,
                    kind="observation",
                    sources=args.source,
                )
            case "timeline":
                if args.limit < 1:
                    parser.error("--limit must be positive")
                events = store.events(args.kind, target=args.agent, task=args.task)[-args.limit :]
                result = (
                    events
                    if args.full
                    else [
                        {k: e[k] for k in ("seq", "id", "at", "kind", "actor", "target", "task")}
                        | {
                            "preview": str(
                                e["data"].get(
                                    "text", e["data"].get("message", e["data"].get("summary", ""))
                                )
                            )[:200]
                        }
                        for e in events
                    ]
                )
            case "search":
                result = store.search(args.query)
            case "show":
                result = store.get(args.event)
            case "source":
                event = store.get(args.event)
                ref = event["data"].get(args.field)
                if not isinstance(ref, dict) or "sha256" not in ref:
                    raise ValueError("Selected field is not a retained source reference")
                print(store.read(ref).decode())
                return
            case "export-call":
                from .inspection import export_call

                result = export_call(store, args.event, args.to)
            case "reindex":
                store.sync(rebuild=True)
                result = {"indexed": len(store.events())}
            case "agent":
                agents = Agents(store)
                if args.action == "list":
                    result = [asdict(a) for a in agents.all()]
                elif args.action == "create":
                    result = asdict(agents.create(args.name, args.role, args.responsibility))
                else:
                    result = asdict(
                        Context(store).fork(args.from_agent, args.name, args.responsibility)
                    )
            case "memory":
                if args.action == "show":
                    result = Memory(store).packet(args.agent)
                else:
                    if args.file is None:
                        parser.error("memory publish requires --file")
                    result = Memory(store).publish(
                        "owner",
                        args.agent,
                        args.layer,
                        args.key,
                        args.file.read_text(),
                        expected=args.expected,
                    )
            case "budget":
                budget = Budget(store)
                if args.calls is not None:
                    budget.grant(args.calls, args.output_tokens, args.input_tokens)
                result = budget.status()
            case "status":
                result = Runtime(store, None).status()
            case "usage":
                result = usage_report(store, args.task)
            case "archive":
                from .storage import Archive

                archive = Archive(store)
                if args.destination:
                    archive.configure(args.destination, args.days)
                result = archive.sweep()
            case "backup":
                from .storage import backup

                result = backup(store, args.path)
            case "pause" | "resume":
                task = Runtime(store, None).tasks().get(args.task)
                if not task:
                    raise ValueError("Unknown task")
                if task["status"] == "completed":
                    raise ValueError("A completed task stays complete; post a follow-up")
                abandoned = {e["data"]["call"] for e in store.events("model.abandoned")}
                uncertain = [
                    e
                    for e in store.events("model.failed", task=args.task)
                    if e["data"].get("uncertain") and e["data"]["call"] not in abandoned
                ]
                if args.command == "resume" and uncertain:
                    raise ValueError("Reconcile uncertain calls explicitly before resuming")
                result = store.append(
                    "task.paused" if args.command == "pause" else "task.ready",
                    {},
                    actor="owner",
                    target=task["agent"],
                    task=args.task,
                )
            case "reconcile":
                call = store.get(args.call)
                if call["kind"] != "model.started":
                    raise ValueError("Select a model.started event")
                if any(e["data"]["call"] == args.call for e in store.events("model.completed")):
                    raise ValueError("Completed calls are reused, not abandoned")
                result = store.append(
                    "model.abandoned",
                    {"call": args.call, "reason": args.reason},
                    actor="owner",
                    target=call["target"],
                    task=call["task"],
                    dedup="abandon:" + args.call,
                )
            case "cancel-process":
                from .tools import Tools

                Tools(store).cancel(args.handle)
                result = {"handle": args.handle, "signal": "SIGTERM"}
            case "run" | "ask" | "chat" | "compact":
                provider = Responses(
                    kind=args.provider,
                    transport=args.transport,
                    timeout=args.timeout,
                    output_tokens=args.output_reserve,
                )
                last_agent = [None]

                def output(agent, text):
                    if Agents(store).get(agent).role in ("astra", "sol"):
                        if last_agent[0] != agent:
                            print("\n[" + agent + "] ", end="", flush=True)
                            last_agent[0] = agent
                        print(text, end="", flush=True)

                runtime = Runtime(
                    store,
                    provider,
                    allow_processes=args.allow_processes,
                    auto_recall=not args.no_auto_recall,
                    output=output,
                )
                if args.command == "ask":
                    Mailbox(store).post(args.text, args.to)
                if args.command == "chat":
                    result = asyncio.run(chat(runtime, args))
                elif args.command == "compact":
                    snapshot = asyncio.run(runtime.compact(args.to, "manual_compaction"))
                    result = {
                        "snapshot": snapshot["id"],
                        "timeline": snapshot["data"]["timeline"],
                        "through": snapshot["data"]["through"],
                    }
                else:
                    result = asyncio.run(
                        runtime.run(
                            watch=args.watch,
                            seconds=args.seconds
                            if args.seconds is not None
                            else (float("inf") if args.watch else 600),
                        )
                    )
                print()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


def usage_report(store, task=None):
    """Report actual per-model usage; unknown attempts never become free calls."""
    totals = {}
    included = None
    if task:
        included = {task}
        previous = set()
        while previous != included:
            previous = set(included)
            included.update(
                t["id"] for t in store.index.tasks().values() if t.get("parent_task") in included
            )
    for call in store.events("model.started"):
        if included is not None and call["task"] not in included:
            continue
        complete = next(
            (e for e in store.events("model.completed") if e["data"]["call"] == call["id"]), None
        )
        model = complete["data"].get("model_reported") if complete else None
        model = model or call["data"]["model"]
        values = totals.setdefault(
            model, {"calls": 0, "cached": 0, "uncached": 0, "output": 0, "unknown": 0}
        )
        values["calls"] += 1
        usage = (complete["data"].get("usage") or {}) if complete else {}
        cached = usage.get("input_tokens_details", {}).get("cached_tokens")
        if cached is not None and usage.get("input_tokens") is not None:
            values["cached"] += cached
            values["uncached"] += usage["input_tokens"] - cached
        if (
            cached is None
            or usage.get("input_tokens") is None
            or usage.get("output_tokens") is None
        ):
            values["unknown"] += 1
        if usage.get("output_tokens") is not None:
            values["output"] += usage["output_tokens"]
        values["last_context"] = usage.get("input_tokens")
    return totals


async def chat(runtime, args):
    """Keep input responsive while agents work; /status and /quit require no inference."""
    print("CLI chat: /status, /usage, /urgent TEXT, /quit. Other input goes to " + args.to)
    runner = asyncio.create_task(
        runtime.run(watch=True, seconds=args.seconds if args.seconds is not None else float("inf"))
    )
    try:
        while not runner.done():
            try:
                line = await asyncio.to_thread(input, "\nbloom> ")
            except EOFError:
                break
            if line == "/quit":
                break
            if line == "/status":
                print(json.dumps(runtime.status(), indent=2))
                continue
            if line == "/usage":
                print(json.dumps(usage_report(runtime.store), indent=2))
                continue
            if not line.strip():
                continue
            urgent = line.startswith("/urgent ")
            runtime.mailbox.post(line[8:] if urgent else line, args.to, urgent=urgent)
    finally:
        runtime.stopping = True
        await runner
    return runtime.status()


if __name__ == "__main__":
    main()
