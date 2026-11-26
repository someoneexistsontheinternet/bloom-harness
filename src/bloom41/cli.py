"""CLI gateway; installation/import never starts inference or browser automation."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from .agents import Agents
from .loop import Loop
from .messages import Messages
from .models import Responses
from .store import Store, uid


def summary(store):
    turns, jobs = {}, {}
    for kind, destination in (("turn.state", turns), ("job.state", jobs)):
        for row in store.events(kind):
            destination[row["subject"]] = {
                k: row["data"].get(k) for k in ("id", "scope", "agent", "name", "status", "error")
            }
    return {
        "agents": Agents(store).all(),
        "turns": list(turns.values()),
        "jobs": list(jobs.values()),
        "requests": len(store.events("model.started")),
        "responses": len(store.events("model.completed")),
        "failures": len(store.events("model.failed")),
    }


async def readline():
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def ready():
        if not future.done():
            future.set_result(sys.stdin.readline())

    loop.add_reader(sys.stdin.fileno(), ready)
    try:
        return await future
    finally:
        loop.remove_reader(sys.stdin.fileno())


async def execute(args):
    if args.command == "demo":
        from .demo import DemoProvider

        with_store = Store(args.root)
        occupied = bool(with_store.events())
        with_store.close()
        if occupied:
            raise ValueError("Use a fresh --root for the explicitly scripted demo")
        provider = DemoProvider()
    else:
        provider = Responses(args.provider)
    loop = Loop(
        args.root,
        provider,
        heartbeat=0.01 if args.command == "demo" else args.heartbeat,
        trusted_tools=args.trusted_tools,
        output=lambda text: print(text, flush=True),
    )
    try:
        if args.command == "demo":
            loop.models.grant(40)
            loop.messages.deliver(
                "user",
                "astra",
                "Use a specialist to calculate 2+2 and check the evidence.",
                message_id="demo-question",
            )
            await loop.run(seconds=30)
            answers = loop.messages.pending("user")
            if (
                len(answers) != 1
                or "result is 4" not in loop.store.read(answers[0]["body"]).decode()
            ):
                raise RuntimeError(
                    "Demo did not produce its checked final answer: " + json.dumps(loop.status())
                )
            calls = len(provider.requests)
            await loop.run(seconds=0.1)
            if len(provider.requests) != calls:
                raise AssertionError("Idle demo unexpectedly bought another model request")
            print(
                json.dumps(
                    {
                        "simulated_models": True,
                        "external_model_requests": 0,
                        "scripted_requests": calls,
                        "verified_user_messages": len(answers),
                        "workspace": str(loop.store.root),
                    }
                )
            )
        elif args.command == "resume":
            print(json.dumps(loop.resume(loop.messages.resolve(args.actor), args.reason)))
        elif args.command == "chat":
            runner = asyncio.create_task(loop.run(watch=True, seconds=args.seconds))
            try:
                while not runner.done():
                    print("bloom> ", end="", flush=True)
                    pending_input = asyncio.create_task(readline())
                    done, _ = await asyncio.wait(
                        [pending_input, runner], return_when=asyncio.FIRST_COMPLETED
                    )
                    if runner in done:
                        pending_input.cancel()
                        await asyncio.gather(pending_input, return_exceptions=True)
                        break
                    line = pending_input.result().strip()
                    if line in ("/quit", ""):
                        break
                    if line == "/status":
                        print(json.dumps(loop.status(), indent=2))
                    else:
                        urgent = line.startswith("/urgent ")
                        loop.messages.deliver(
                            "user",
                            "astra",
                            line.removeprefix("/urgent ") if urgent else line,
                            message_id=uid("input"),
                            metadata={"urgent": urgent},
                        )
            finally:
                loop.stopping = True
                await runner
        else:
            result = await loop.run(watch=args.watch, seconds=args.seconds)
            if result["blocked_actors"] or any(t["status"] == "blocked" for t in result["turns"]):
                print(json.dumps(result, indent=2))
                return 2
        return 0
    finally:
        loop.store.close()


def main():
    parser = argparse.ArgumentParser(description="Bloom v4.1 filesystem-agent CLI")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--provider", choices=("codex", "openai"), default="codex")
    parser.add_argument(
        "--trusted-tools",
        action="store_true",
        help="Explicitly allow trusted scripts when no OS sandbox exists",
    )
    parser.add_argument("--heartbeat", type=float, default=5.0)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("usage")
    sub.add_parser("reindex")
    sub.add_parser("demo")
    resume = sub.add_parser(
        "resume", help="Inspect receipts first; explicitly permit another decision after failure"
    )
    resume.add_argument("actor")
    resume.add_argument("--reason", required=True)
    post = sub.add_parser("post")
    post.add_argument("text")
    post.add_argument("--to", default="astra")
    post.add_argument("--id")
    post.add_argument("--urgent", action="store_true")
    budget = sub.add_parser("budget")
    allowance = budget.add_mutually_exclusive_group(required=True)
    allowance.add_argument("--calls", type=int)
    allowance.add_argument("--unlimited", action="store_true")
    for name in ("run", "chat"):
        run = sub.add_parser(name)
        run.add_argument("--seconds", type=float)
        run.add_argument("--watch", action="store_true")
    inbox = sub.add_parser("inbox")
    inbox.add_argument("recipient", nargs="?", default="user")
    tools = sub.add_parser("tools")
    tools.add_argument("agent", nargs="?", default="astra")
    args = parser.parse_args()
    try:
        if args.command in ("run", "chat", "demo", "resume"):
            raise SystemExit(asyncio.run(execute(args)))
        store = Store(args.root)
        try:
            if args.command == "init":
                Agents(store).bootstrap()
                print(
                    json.dumps(
                        {
                            "root": str(store.root),
                            "agents": [a["id"] for a in Agents(store).all()],
                            "model_calls": 0,
                        }
                    )
                )
            elif args.command == "post":
                print(
                    json.dumps(
                        Messages(store).deliver(
                            "user",
                            args.to,
                            args.text,
                            message_id=args.id or uid("input"),
                            metadata={"urgent": args.urgent},
                        )
                    )
                )
            elif args.command == "budget":
                if args.calls is not None and args.calls < 1:
                    raise ValueError("Call allowance must be positive")
                store.event(
                    "budget.granted",
                    "workspace",
                    {"calls": None if args.unlimited else args.calls},
                    key=uid("grant"),
                )
                print("Allowance recorded; no model request started.")
            elif args.command == "status":
                print(json.dumps(summary(store), indent=2))
            elif args.command == "usage":
                print(
                    json.dumps(
                        {
                            "requests": [
                                {"scope": r["subject"], **r["data"]}
                                for r in store.events("model.started")
                            ],
                            "reported_usage": [
                                r["data"].get("usage") for r in store.events("model.completed")
                            ],
                        },
                        indent=2,
                    )
                )
            elif args.command == "reindex":
                store.sync(rebuild=True)
                print(json.dumps({"events": len(store.events()), "status": "rebuilt_from_files"}))
            elif args.command == "inbox":
                for package in Messages(store).pending(args.recipient):
                    print(store.read(package["body"]).decode())
            elif args.command == "tools":
                from .tool import discover

                print(json.dumps(discover(Agents(store).path(args.agent) / "tools"), indent=2))
        finally:
            store.close()
    except (ValueError, RuntimeError, PermissionError) as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
