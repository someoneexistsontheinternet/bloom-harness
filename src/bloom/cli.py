from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from .config import Config
from .delivery import Discord
from .desktop import Desktop
from .operations import export_calls, report
from .runtime import Runtime
from .store import Store


async def command(args):
    config = Config(Path(args.root))
    store = Store(config)
    try:
        runtime = Runtime(store)
        if args.command == "init":
            result = {
                "database": str(store.path),
                "status": "initialized",
                "discord_enabled": config.data["discord"]["enabled"],
            }
        elif args.command == "doctor":
            result = {
                "python": sys.executable,
                "sqlite": store.one("SELECT sqlite_version() AS version"),
                "profiles": {
                    name: {
                        "model": p["model"],
                        "context_window_configured": p["context_limit"],
                        "compact_at": p["compact_at"],
                        "compact_to": p["compact_to"],
                    }
                    for name, p in config.data["profiles"].items()
                },
            }
            try:
                result["desktop"] = Desktop(config.workspace).capabilities()
            except Exception as exc:
                result["desktop"] = {"error": str(exc)}
            if args.discord:
                result["discord"] = await Discord(store).inspect()
        elif args.command == "status":
            result = report(store)
        elif args.command == "sql":
            result = store.readonly(args.query)
        elif args.command == "export-calls":
            result = export_calls(store)
        elif args.command == "memory":
            history = runtime.context.history
            if args.action == "status":
                result = {
                    "state": history.state("astra", args.query, args.task),
                    "batches": store.rows(
                        "SELECT id,status,error,call_id FROM memory_batches ORDER BY rowid DESC LIMIT 10"
                    ),
                }
            else:
                with store.runtime_lock():
                    if args.action == "index":
                        result = {"indexed": history.index()}
                    elif args.action == "search":
                        result = history.search(
                            "astra", args.query, task=args.task, parent=args.node
                        )
                    elif args.action == "read":
                        result = history.read(
                            "astra", args.node, task=args.task, start=args.start, limit=args.limit
                        )
                    elif args.action == "retry":
                        result = history.retry(args.batch)
                    else:
                        result = await history.consolidate(
                            task=args.task, max_batches=args.max_batches
                        )
        elif args.command in ("pause", "resume", "steer"):
            if args.command == "pause":
                runtime.pause(args.task)
            elif args.command == "resume":
                runtime.resume(args.task)
            else:
                runtime.steer(args.task, args.text)
            result = store.one("SELECT id,status,error FROM tasks WHERE id=?", (args.task,))
        elif args.command == "compact":
            row = store.one("SELECT * FROM tasks WHERE id=?", (args.task,))
            with store.runtime_lock():
                result = {"context_id": await runtime.context.compact(row["agent_id"], row["id"])}
        elif args.command in ("ask", "run"):
            with store.runtime_lock():
                task = store.new_task(args.text) if args.command == "ask" else None
                loop = asyncio.get_running_loop()
                for signum in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(signum, setattr, runtime, "stopping", True)
                await runtime.run(until_idle=args.command == "ask" or args.once)
                export_calls(store)
                result = report(store)
                if task:
                    result["requested_task"] = task
                    result["answer"] = store.rows(
                        "SELECT text FROM items WHERE task_id=? AND role='assistant' AND kind='message' ORDER BY id",
                        (task,),
                    )
        elif args.command == "reconcile-model":
            from .reconciliation import abandon

            with store.runtime_lock():
                result = abandon(store, args.call, args.reason)
        elif args.command == "resolve":
            # Explicit owner reconciliation is recorded as an event; never guessed on restart.
            row = store.one("SELECT * FROM operations WHERE id=?", (args.operation,))
            if not row or row["status"] != "uncertain":
                raise ValueError("Select an uncertain operation")
            data = json.loads(Path(args.result).read_text())
            store.db.execute(
                "UPDATE operations SET status='completed',result_hash=?,error=NULL WHERE id=?",
                (store.put(data), args.operation),
            )
            store.event(
                "owner.reconciled_operation",
                {"operation": args.operation, "result": data},
                row["task_id"],
            )
            task = store.one("SELECT agent_id FROM tasks WHERE id=?", (row["task_id"],))
            store.item(
                task["agent_id"],
                {
                    "role": "user",
                    "content": "Owner reconciled operation "
                    + args.operation
                    + ":\n"
                    + json.dumps(data),
                },
                row["task_id"],
            )
            store.status(row["task_id"], "ready")
            result = {"resolved": args.operation}
        else:
            raise ValueError("Unknown command")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description="Bloom v3")
    parser.add_argument(
        "--root", default=os.environ.get("BLOOM_ROOT", str(Path(__file__).resolve().parents[2]))
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "export-calls"):
        sub.add_parser(name)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--discord", action="store_true")
    ask = sub.add_parser("ask")
    ask.add_argument("text")
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    sql = sub.add_parser("sql")
    sql.add_argument("query")
    memory = sub.add_parser("memory")
    memory.add_argument(
        "action", choices=("status", "index", "search", "read", "consolidate", "retry")
    )
    memory.add_argument("--task")
    memory.add_argument("--query", default="")
    memory.add_argument("--node")
    memory.add_argument("--batch")
    memory.add_argument("--start", type=int, default=0)
    memory.add_argument("--limit", type=int, default=30000)
    memory.add_argument("--max-batches", type=int, default=4)
    for name in ("pause", "resume", "compact", "steer"):
        p = sub.add_parser(name)
        p.add_argument("task")
        if name == "steer":
            p.add_argument("text")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("operation")
    resolve.add_argument("--result", required=True)
    reconcile = sub.add_parser("reconcile-model")
    reconcile.add_argument("call")
    reconcile.add_argument("--reason", required=True)
    args = parser.parse_args()
    if os.environ.get("BLOOM_SERVICE") == "1" and args.command == "run":
        from .service import redirect_service_logs

        redirect_service_logs(Path(args.root).resolve())
    try:
        asyncio.run(command(args))
    except Exception as exc:
        from .privacy import clean

        print(json.dumps({"error": clean(str(exc)), "type": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
