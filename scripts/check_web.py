"""Retained-capture replay and opt-in local Safari regression; zero model requests."""

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
import threading

from bloom41.store import atomic, digest, encoded, uid
from bloom_safari.safari import Safari
from bloom_safari.web_document import page_document


def inspect(page):
    document = page_document(page)
    checks = {
        "text_identical": document["text"] == page["text"],
        "no_dom_inventories": not {"tables", "links", "images", "headings", "visual_regions"}
        & document.keys(),
        "format": document.get("text_format") == "markdown-v1",
    }
    images = [
        {"sha256": digest(Path(p["path"]).read_bytes()), "path": p["path"]}
        for p in page.get("images", [])
    ]
    text = document["text"]
    if "bloom-comment-" in text:
        checks.update(
            all_comments_once=all(text.count(f"bloom-comment-{i:04d}") == 1 for i in range(483)),
            stable_comment_ids=all(f"Comment {700000 + i}:" in text for i in range(483)),
        )
    elif "Unique results caption" in text:
        checks.update(
            caption_once=text.count("Unique results caption") == 1,
            values_once=all(text.count(v) == 1 for v in ("17", "29", "31", "43", "71", "83")),
            spans="rowspan=2" in text and "colspan=2" in text and "↳" in text,
        )
    else:
        checks.update(
            references_attached="[primary report][ref-1]" in text
            and "[same source][ref-1]" in text,
            fragment_target="#methods" in text,
            no_hidden_script="HIDDEN-FIXTURE-MARKER" not in text
            and "SCRIPT-FIXTURE-MARKER" not in text,
        )
    checks["native_images_retained"] = bool(images)
    return {
        "accepted": all(checks.values()),
        "checks": checks,
        "characters": len(text),
        "images": images,
    }


async def live(root, output):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from web_fixtures import fixture_pages

    pages = fixture_pages()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = (
                '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                "<title>Bloom document regression</title><style>body{font:16px sans-serif}td,th{padding:4px}</style>"
                + pages.get(self.path, "Missing fixture")
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cases = []
    try:
        async with Safari(root / "workspace", uid("fixture")) as browser:
            for path in pages:
                folder = output / path[1:]
                page = await browser.capture(
                    f"http://127.0.0.1:{server.server_port}{path}", folder, height=512
                )
                atomic(folder / "capture.json", encoded(page))
                cases.append({"fixture": path, **inspect(page)})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--retained", type=Path)
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    args.root, args.output = args.root.resolve(), args.output.resolve()
    if not args.output.is_relative_to(args.root):
        parser.error("Keep reports under the project root")
    args.output.mkdir(parents=True, exist_ok=False)
    cases, error = [], None
    try:
        if args.retained:
            cases += [
                {"capture": str(p), **inspect(json.loads(p.read_bytes()))}
                for p in sorted(args.retained.glob("*/capture.json"))
            ]
        if args.browser:
            cases += asyncio.run(live(args.root, args.output))
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
    result = {
        "accepted": bool(cases) and not error and all(c["accepted"] for c in cases),
        "cases": cases,
        "error": error,
        "model_requests": 0,
    }
    atomic(args.output / "report.json", encoded(result))
    print(
        json.dumps(
            {
                "accepted": result["accepted"],
                "error": error,
                "report": str(args.output / "report.json"),
            }
        )
    )
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
