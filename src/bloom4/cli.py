"""CLI inspection and filesystem input publishing; gateways are deliberately separate."""

import argparse
import json

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
    args = parser.parse_args()
    store = Store(args.workspace)
    try:
        match args.command:
            case "init": result = {"workspace": str(store.root), "events": len(store.events())}
            case "post": result = Mailbox(store).post(args.text, args.to, urgent=args.urgent, request_id=args.id)
            case "ingest": result = Mailbox(store).ingest()
            case "timeline": result = store.events(target=args.agent, task=args.task)
            case "search": result = store.search(args.query)
            case "show": result = store.get(args.event)
            case "reindex": store.sync(rebuild=True); result = {"indexed": len(store.events())}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
