import base64
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

from bloom41.agents import Actor
from bloom41.context import Context
from bloom41.loop import Loop
from bloom_safari.service import BrowserService, canonical
from bloom_safari.web_document import page_document
from test_delivery import done
from test_models_context import Scripted, reply

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
)


class Captures:
    calls = []

    def __init__(self, workspace, operation):
        self.operation = operation

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def discover_page(self, url):
        start = int(parse_qs(urlsplit(url).query)["start"][0])
        return {
            "url": url,
            "results": [
                {
                    "url": f"https://example.test/page/{n}?utm_source=fixture",
                    "title": f"Document {n}",
                }
                for n in range(start, start + 10)
            ],
        }

    async def capture(self, url, destination, **geometry):
        self.calls.append((url, geometry))
        number = int(urlsplit(url).path.rsplit("/", 1)[1])
        if number == 19:
            raise PermissionError("Fixture page blocked")
        destination.mkdir(parents=True, exist_ok=True)
        image = destination / "native.png"
        image.write_bytes(PNG)
        text = (
            ("evidence from source zero\n" * 2200)
            if number == 0
            else f"Whole document {number}. Table: 2 | 4. Ref: https://example.test/original"
        )
        return {
            "status": "ok",
            "url": url,
            "title": f"Title {number}",
            "text": text,
            "text_format": "document_markdown",
            "tables": ["RAW_TABLE_SENTINEL"],
            "links": ["RAW_LINK_SENTINEL"],
            "headings": ["RAW_HEADING_SENTINEL"],
            "coverage": {"text": "whole loaded document"},
            "capture_coverage": [{"start_y": 0, "end_y": 512}],
            "images": [{"path": str(image), "mime": "image/png", "width": 1, "height": 1}],
            "more_below": True,
        }


class Reviewer(Scripted):
    async def generate(self, request):
        self.requests.append(request)
        text = json.dumps(request)
        assert (
            "RAW_TABLE_SENTINEL" not in text
            and "RAW_LINK_SENTINEL" not in text
            and "RAW_HEADING_SENTINEL" not in text
        )
        content = request["input"][0]["content"]
        packet = json.loads(content[1]["text"])
        number = int(packet["context"]["title"].split()[-1])
        assert len([p for p in content if p["type"] == "input_image"]) == 1
        assert request["reasoning"]["effort"] == "medium"
        relevant = number < 3
        first, last = packet["first_line"], packet["last_line"]
        summaries = []
        if not relevant:
            summaries = [
                {"start_line": first, "end_line": last, "summary": "Unrelated source material"}
            ]
        elif first < last:
            summaries = [
                {
                    "start_line": first + 1,
                    "end_line": last,
                    "summary": "Remaining context and repeated evidence",
                }
            ]
        return reply(
            json.dumps(
                {
                    "relevant": relevant,
                    "priority": 3 - number if relevant else 0,
                    "summary": f"Source {number} assessed for the actual question",
                    "passages": [{"start_line": first, "end_line": first}] if relevant else [],
                    "summaries": summaries,
                    "visual_relevant": number == 1,
                    "visual_reason": "Only source one adds a chart",
                    "limitations": [],
                }
            )
        ), []


async def test_search_captures_twenty_reviews_native_images_and_curates_large_selection(tmp_path):
    provider = Reviewer()
    loop = Loop(tmp_path, provider, trusted_tools=sys.platform != "darwin")
    loop.models.grant(25)
    browser_workspace = tmp_path / "existing-granted-helper/workspace"

    class ExistingHelper(Captures):
        def __init__(self, workspace, operation):
            assert workspace == browser_workspace
            super().__init__(workspace, operation)

    loop.services["safari"] = BrowserService(
        loop.store, ExistingHelper, browser_workspace=browser_workspace
    )
    await loop.jobs.start()
    try:
        job = loop.jobs.submit(Actor("astra"), "search", {"query": "fixture evidence"})
        completed = await done(loop.jobs, job["id"])
        assert completed["status"] == "completed", completed
        result = loop.store.json(completed["result"])
        assert result["status"] == "completed", result
        assert len(result["candidates"]) == 20 and len(result["selected"]) == 3
        large = result["selected"][0]
        assert "text" not in large and "evidence" in large, large
        assert large["evidence"]["passages"][0]["text"] == "evidence from source zero"
        assert result["files"] and len(provider.requests) == 19
        # A later user-facing tool result embeds selected native bytes and no full large text.
        await loop.notify_jobs()
        package = loop.messages.pending(Actor("astra"))[0]
        content = Context(loop.store).content(package)["content"]
        assert len([p for p in content if p["type"] == "input_image"]) == 1
        assert "RAW_TABLE_SENTINEL" not in json.dumps(content)
        source = loop.store.events("web.source")[0]
        original = loop.store.json(source["data"]["source"])
        assert original["tables"] == ["RAW_TABLE_SENTINEL"]
        assert (
            Path(original["original_text_path"]).read_text() == "evidence from source zero\n" * 2200
        )
        assert all(
            r["data"]["purpose"] == "tool_reader" for r in loop.store.events("model.started")
        )
    finally:
        await loop.jobs.close()
        loop.store.close()


async def test_missing_browser_permission_produces_explicit_result_without_model_calls(tmp_path):
    class Denied(Captures):
        async def __aenter__(self):
            raise PermissionError("Separate v4.1 Automation grant required")

    provider = Scripted()
    loop = Loop(tmp_path, provider, trusted_tools=sys.platform != "darwin")
    loop.services["safari"] = BrowserService(loop.store, Denied)
    await loop.jobs.start()
    try:
        job = loop.jobs.submit(Actor("astra"), "search", {"query": "fixture"})
        state = await done(loop.jobs, job["id"])
        assert state["status"] == "completed", state
        result = loop.store.json(state["result"])
        assert result["status"] == "capture_failed" and "Automation grant" in result["error"]
        assert not provider.requests and loop.store.events("web.completed")
    finally:
        await loop.jobs.close()
        loop.store.close()


def test_document_projection_and_canonical_url():
    source = {
        "text": "full legitimate discussion",
        "tables": ["duplicate"],
        "links": ["duplicate"],
        "images": ["native metadata"],
    }
    assert page_document(source)["text"] == source["text"]
    assert not set(("tables", "links", "images")) & page_document(source).keys()
    assert (
        canonical("https://example.test/a?x=1&utm_source=z#anchor") == "https://example.test/a?x=1"
    )
    with pytest.raises(ValueError):
        canonical("https://private:password@example.test")


async def test_internal_safari_error_page_is_not_reviewed_as_a_successful_source(tmp_path):
    class ErrorPage(Captures):
        async def capture(self, url, destination, **geometry):
            page = await super().capture(url, destination, **geometry)
            return {
                **page,
                "url": "safari-resource:/ErrorPage.html",
                "text": "Safari could not open the requested page.",
            }

    provider = Scripted()
    loop = Loop(tmp_path, provider, trusted_tools=sys.platform != "darwin")
    loop.services["safari"] = BrowserService(loop.store, ErrorPage)
    await loop.jobs.start()
    try:
        job = loop.jobs.submit(
            Actor("astra"), "search", {"query": "fixture", "urls": ["https://example.test/page/0"]}
        )
        state = await done(loop.jobs, job["id"])
        result = loop.store.json(state["result"])
        assert result["status"] == "capture_failed" and not provider.requests
        manifest = loop.store.json(loop.store.events("web.source")[0]["data"]["source"])
        assert manifest["status"] == "capture_failed" and "internal error page" in manifest["error"]
        assert (
            Path(manifest["text_path"]).read_text() == "Safari could not open the requested page."
        )
        assert Path(manifest["images"][0]["path"]).read_bytes() == PNG
    finally:
        await loop.jobs.close()
        loop.store.close()
