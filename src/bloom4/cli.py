"""CLI inspection and filesystem input publishing; gateways are deliberately separate."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .agents import Agents
from .context import Context
from .memory import Memory
from .mailbox import Mailbox
from .store import Store


def main():
    parser = argparse.ArgumentParser(prog="bloom4")
    parser.add_argument("--workspace", default=".bloom")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    post = sub.add_parser("post")
    post.add_argument("text")
    post.add_argument("--to", default="astra")
    post.add_argument("--urgent", action="store_true")
    post.add_argument("--id")
    sub.add_parser("ingest")
    timeline = sub.add_parser("timeline")
    timeline.add_argument("--agent")
    timeline.add_argument("--task")
    search = sub.add_parser("search")
    search.add_argument("query")
    show = sub.add_parser("show")
    show.add_argument("event")
    sub.add_parser("reindex")
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
    args = parser.parse_args()
    store = Store(args.workspace)
    try:
        match args.command:
            case "init":
                Agents(store).bootstrap()
                result = {"workspace": str(store.root), "events": len(store.events())}
            case "post": result = Mailbox(store).post(args.text, args.to, urgent=args.urgent, request_id=args.id)
            case "ingest": result = Mailbox(store).ingest()
            case "timeline": result = store.events(target=args.agent, task=args.task)
            case "search": result = store.search(args.query)
            case "show": result = store.get(args.event)
            case "reindex": store.sync(rebuild=True); result = {"indexed": len(store.events())}
            case "agent":
                agents = Agents(store)
                if args.action == "list": result = [asdict(a) for a in agents.all()]
                elif args.action == "create":
                    result = asdict(agents.create(args.name, args.role, args.responsibility))
                else:
                    result = asdict(Context(store).fork(args.from_agent, args.name, args.responsibility))
            case "memory":
                if args.action == "show": result = Memory(store).packet(args.agent)
                else:
                    if args.file is None: parser.error("memory publish requires --file")
                    result = Memory(store).publish("owner", args.agent, args.layer, args.key,
                        args.file.read_text(), expected=args.expected)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
