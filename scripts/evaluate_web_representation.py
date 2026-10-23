"""Offline retained-search replay and opt-in real Safari document regression.

No model inference or Discord messages. Original captures/requests are read only.
Run --browser from the GUI service context with the existing native helper grant.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import tiktoken

from bloom.config import Config
from bloom.reset import reader
from bloom.safari import Safari
from bloom.service import process_identity
from bloom.store import atomic, encoded, now
from bloom.web import review_input
from bloom.web_document import page_document, result_document
from bloom.results import text_of


def replay(root):
    tokenizer = tiktoken.get_encoding("o200k_base")
    count = lambda text: len(tokenizer.encode(text, disallowed_special=()))
    reports = []
    with reader(Config(root)) as store:
        store.db.execute("BEGIN")
        original_bytes = store.bytes
        store.bytes = lambda digest: original_bytes(digest, touch=False, cache=False)
        store.get = lambda digest: json.loads(store.bytes(digest))
        for run in store.rows(
            "SELECT * FROM web_runs WHERE kind='search' AND review_call IS NOT NULL ORDER BY created_at"
        ):
            call = store.one("SELECT * FROM model_calls WHERE id=?", (run["review_call"],))
            if not call or not call["request_hash"]:
                continue
            old = store.get(call["request_hash"])
            sources = []
            for row in store.rows(
                "SELECT * FROM web_sources WHERE operation_id=? ORDER BY rank",
                (run["operation_id"],),
            ):
                page = store.get(row["manifest_hash"])
                page["text"] = store.bytes(page["text_hash"]).decode()
                assert hashlib.sha256(page["text"].encode()).hexdigest() == page["text_hash"]
                sources.append(page)
            items = review_input(store, run["query"], store.get(run["context_hash"]), sources)

            def parts(request_items):
                return [
                    part
                    for item in request_items
                    for part in item.get("content", [])
                    if isinstance(part, dict)
                ]

            old_parts, new_parts = parts(old["input"]), parts(items)
            old_images = [part for part in old_parts if part["type"] == "input_image"]
            new_images = [part for part in new_parts if part["type"] == "input_image"]
            assert old_images == new_images, "Native image bytes/order/detail changed"
            documents = [
                json.loads(part["text"])
                for part in new_parts
                if part["type"] == "input_text" and "text_hash" in json.loads(part["text"])
            ]
            assert len(documents) == len(sources)
            for source, document in zip(sources, documents, strict=True):
                assert document["text"] == source["text"]
                assert (
                    not {"tables", "links", "headings", "visual_regions", "images"}
                    & document.keys()
                )
            before = sum(count(part["text"]) for part in old_parts if part["type"] == "input_text")
            after = sum(count(part["text"]) for part in new_parts if part["type"] == "input_text")
            raw = store.get(run["result_hash"]) if run["result_hash"] else None
            projected = result_document(raw) if raw else None
            if projected:
                for source in projected.get("selected", []):
                    assert not {"tables", "links"} & source.keys()
                curation_text = text_of(raw)[0]
                assert "Structured page data:" not in curation_text
            reports.append(
                {
                    "operation": run["operation_id"],
                    "created_at": run["created_at"],
                    "sources": len(sources),
                    "native_images": len(new_images),
                    "text_tokens_before": before,
                    "text_tokens_after": after,
                    "saved_text_tokens": before - after,
                    "reduction_percent": round(100 * (before - after) / before, 2) if before else 0,
                    "original_source_characters": sum(len(p["text"]) for p in sources),
                    "full_text_identical": True,
                    "native_images_identical": True,
                    "original_request_hash": call["request_hash"],
                    "reported_original_input_tokens": call["input_tokens"],
                }
            )
    return {
        "accepted": bool(reports)
        and all(r["full_text_identical"] and r["native_images_identical"] for r in reports),
        "searches": reports,
        "measurement": "o200k_base on actual request text parts; image parts unchanged. No new billing or model-quality measurement.",
        "model_calls": 0,
        "external_posts": 0,
    }


def fixture_pages():
    comments = "".join(
        f'<tr class="comtr" id="{700000 + i}"><td><table><tr><td>'
        f'<a class="hnuser" href="https://news.ycombinator.com/user?id=reader">reader</a> '
        f'<span class="age"><a href="https://news.ycombinator.com/item?id={700000 + i}">1 day ago</a></span>'
        f"<p>bloom-comment-{i:04d}: complete substantive discussion with distinct evidence preserved.</p>"
        '<a href="https://sources.example/report">Original report</a> '
        f'<a href="https://news.ycombinator.com/reply?id={700000 + i}">reply</a>'
        "</td></tr></table></td></tr>"
        for i in range(483)
    )
    return {
        "/discussion": '<table role="presentation"><tr><td><table>'
        + comments
        + "</table></td></tr></table>",
        "/tables": """<main><p>Before the complete results.</p>
<table><caption>Unique results caption</caption><thead>
<tr><th rowspan="2">Region</th><th colspan="2">Annual results</th></tr>
<tr><th>2025</th><th>2026</th></tr></thead><tbody>
<tr><th scope="row">North</th><td>17</td><td>29</td></tr>
<tr><th scope="row">South</th><td>31</td><td>43</td></tr></tbody></table>
<table><tr><td>alpha</td><td>71</td></tr><tr><td>beta</td><td>83</td></tr></table>
<p>After the complete results.</p></main>""",
        "/links": """<main><p>Use the <a href="https://sources.example/report">primary report</a>.</p>
<p>Compare its <a href="https://sources.example/report">same source</a> and <a href="#methods">methods section</a>.</p>
<p><a role="button" href="/share">share this page</a> <a href="/reply?id=999">reply</a>
<a href="https://news.ycombinator.com/user?id=author">author</a></p>
<section id="methods"><h2>Methods</h2><p>Every substantive paragraph remains available.</p></section>
<p hidden>HIDDEN-FIXTURE-MARKER</p><script>const ignored='SCRIPT-FIXTURE-MARKER';</script></main>""",
    }


async def browser_check(root, directory):
    pages = fixture_pages()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
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
        operation = "document-regression-" + directory.parent.name + "-" + directory.name
        async with Safari(root / "workspace", operation) as browser:
            for path in pages:
                folder = directory / path.lstrip("/")
                page = await browser.capture(
                    f"http://127.0.0.1:{server.server_port}{path}", folder, height=512
                )
                atomic(folder / "capture.json", encoded(page))
                document = page_document(page)
                atomic(folder / "document.json", encoded(document))
                text = document["text"]
                checks = {
                    "one_document": not {"tables", "links", "images", "headings", "visual_regions"}
                    & document.keys(),
                    "format": page.get("text_format") == "markdown-v1",
                    "native_image": bool(page.get("images")),
                }
                if path == "/discussion":
                    checks.update(
                        all_comments_once=all(
                            text.count(f"bloom-comment-{i:04d}") == 1 for i in range(483)
                        ),
                        stable_comment_ids=all(
                            f"Comment {700000 + i}:" in text for i in range(483)
                        ),
                        useful_url_once=text.count("https://sources.example/report") == 1,
                        no_action_or_profile_urls="/reply?id=" not in text
                        and "/user?id=" not in text
                        and "/item?id=" not in text,
                    )
                elif path == "/tables":
                    checks.update(
                        caption_once=text.count("Unique results caption") == 1,
                        headers_once=text.count("Annual results") == 1
                        and text.count("**Region**") == 1,
                        values_once=all(
                            text.count(value) == 1 for value in ("17", "29", "31", "43", "71", "83")
                        ),
                        spans="rowspan=2" in text and "colspan=2" in text and "↳" in text,
                        rows="| **North** | 17 | 29 |" in text and "| alpha | 71 |" in text,
                        surrounding_text="Before the complete results." in text
                        and "After the complete results." in text,
                    )
                else:
                    checks.update(
                        useful_url_once=text.count("https://sources.example/report") == 1,
                        references_attached="[primary report][ref-1]" in text
                        and "[same source][ref-1]" in text,
                        fragment_target="#methods" in text,
                        no_controls="/reply?id=" not in text
                        and "/user?id=" not in text
                        and "/share" not in text,
                        no_hidden_script="HIDDEN-FIXTURE-MARKER" not in text
                        and "SCRIPT-FIXTURE-MARKER" not in text,
                    )
                cases.append(
                    {
                        "fixture": path,
                        "checks": checks,
                        "accepted": all(checks.values()),
                        "characters": len(text),
                        "method": page.get("method"),
                        "retained_layout_tables": len(page.get("tables", [])),
                        "retained_links": len(page.get("links", [])),
                        "image_path": page["images"][0]["path"],
                    }
                )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return {
        "accepted": all(c["accepted"] for c in cases),
        "cases": cases,
        "model_calls": 0,
        "external_posts": 0,
    }


async def main(args):
    args.root = args.root.resolve()
    args.directory = args.directory.resolve()
    args.directory.mkdir(parents=True, exist_ok=False)
    atomic(
        args.directory / "launch.json",
        encoded(
            {
                "at": now(),
                "pid": os.getpid(),
                "start_identity": process_identity(os.getpid()),
                "command": os.sys.argv,
                "source": str(Path(__file__).resolve()),
                "model_calls": 0,
                "external_posts": 0,
            }
        ),
    )
    report = {}
    if args.replay:
        report["replay"] = replay(args.root)
    if args.browser:
        report["browser"] = await browser_check(args.root, args.directory)
    report["accepted"] = all(value["accepted"] for value in report.values())
    atomic(args.directory / "report.json", encoded(report))
    print(
        json.dumps({"accepted": report["accepted"], "report": str(args.directory / "report.json")}),
        flush=True,
    )
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    if not (args.replay or args.browser):
        parser.error("Select --replay and/or --browser")
    raise SystemExit(asyncio.run(main(args)))
