import base64
import json
import os
import struct
import time
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from bloom.images import chat_messages
from bloom.runtime import Runtime
from bloom.storage import Archive, ColdStorageUnavailable
from bloom.web import Web, read_saved, saved_file
from test_control_provenance import invoke
from test_storage import archive


def png():
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


class Pages:
    captures = []
    discovery = []
    fail_rank = None

    def __init__(self, *args):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def discover_page(self, url):
        self.discovery.append(url)
        start = int(parse_qs(urlsplit(url).query)["start"][0])
        return {
            "url": url,
            "results": [
                {"url": f"https://evidence.test/{i}", "title": f"Source {i}", "snippet": "preview"}
                for i in range(start + 1, start + 11)
            ],
        }

    async def capture(self, url, destination, **geometry):
        self.captures.append(url)
        Path(destination).mkdir(parents=True, exist_ok=True)
        rank = int(url.rsplit("/", 1)[1])
        if rank == self.fail_rank:
            raise TimeoutError("Fixture source could not load")
        p = Path(destination) / "render.png"
        p.write_bytes(png())
        return {
            "url": url,
            "title": f"Source {rank}",
            "status": "ok",
            "text": f"Full evidence from source {rank}.\n" + "precise original facts\n" * 1000,
            "tables": [{"rows": [["label", "value"], ["needle", str(rank)]]}],
            "images": [
                {
                    "path": str(p),
                    "mime": "image/png",
                    "detail": "high",
                    "width": 1,
                    "height": 1,
                    "start_y": 0,
                    "end_y": 1,
                }
            ],
            "coverage": {"text": "fixture complete"},
        }


def reviewer(store, runtime, monkeypatch, *, invalid=False, fail=False):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    store.config.data["profiles"]["luna"]["provider"] = "openai"
    store.config.data["providers"]["openai"]["stream"] = False
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        if fail:
            return httpx.Response(503, json={"error": "fixture unavailable"})
        if isinstance(body["input"][0]["content"], str):
            data = json.loads(body["input"][0]["content"])
            assert "source_segments" in data and "owner_question" in data
            result = {
                "status": "found",
                "summary": "Relevant precise facts from the selected sources.",
                "evidence": [
                    {"quote": "precise original facts", "location": "selected source text"}
                ],
                "coverage": "Source text preserved; selected images remain native input.",
            }
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": json.dumps(result)}],
                        }
                    ],
                },
            )
        sources = []
        for part in body["input"][0]["content"][1:]:
            if part["type"] == "input_text":
                value = json.loads(part["text"])
                if "id" in value:
                    sources.append(value)
        usable = [s for s in sources if s["status"] == "ok"]
        result = {
            "selected": [s["id"] for s in usable[:3]],
            "visual": [usable[2]["id"]],
            "sources": [
                {
                    "id": s["id"],
                    "summary": "Observed evidence"
                    if s["status"] == "ok"
                    else "Not inspected: capture failed",
                    "reason": "Selected or excluded by evidence relevance",
                    "visual_reason": "The figure adds non-text evidence"
                    if s == usable[2]
                    else "Text suffices",
                }
                for s in sources
            ],
            "coverage": "All supplied candidates reviewed; captures cover stated ranges",
            "limitations": [],
            "insufficient_relevant_sources": False,
        }
        if invalid:
            result["sources"].pop()
        return httpx.Response(
            200,
            json={
                "id": "review",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": json.dumps(result)}],
                    }
                ],
                "usage": {
                    "input_tokens": 30,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 20,
                },
            },
        )

    runtime.models.transport = httpx.MockTransport(handle)
    Pages.captures = []
    Pages.discovery = []
    Pages.fail_rank = None
    runtime.tools.web.browser_factory = Pages
    return calls


async def test_twenty_candidates_preserve_full_sources_but_curate_model_view_and_reuse_receipts(
    store, monkeypatch
):
    runtime = Runtime(store)
    task = store.new_task("Find the exact needle and inspect its plot")
    store.item(
        "astra",
        {
            "role": "assistant",
            "phase": "final_answer",
            "content": [
                {
                    "type": "output_text",
                    "text": "Prior tool found a contradiction; compare primary sources.",
                }
            ],
        },
        task,
    )
    calls = reviewer(store, runtime, monkeypatch)
    op = await invoke(runtime, store, task, "web_search", {"query": "evidence needle"}, "search")
    assert op["status"] == "completed"
    result = store.get(op["result_hash"])
    assert (
        result["status"] == "ok"
        and result["candidate_count"] == 20
        and result["discovery_complete"]
    )
    assert len(result["selected"]) == 3 and len(result["_images"]) == 1
    assert sum(len(s["text"]) for s in result["selected"]) > 50000
    assert len(result["candidates"]) == 20 and all(
        c["review"]["reason"] for c in result["candidates"]
    )
    assert result["_images"][0]["source_id"] == result["selected"][2]["id"]
    assert len(Pages.captures) == 20 and len(Pages.discovery) == 2 and len(calls) == 2
    parts = calls[0]["input"][0]["content"]
    context = json.loads(parts[0]["text"])
    assert context["owner_question"] == "Find the exact needle and inspect its plot"
    assert "contradiction" in context["intermediate_summaries"][0]["text"]
    assert sum(p["type"] == "input_image" for p in parts) == 20
    documents = [
        json.loads(p["text"])
        for p in parts
        if p["type"] == "input_text" and "text_hash" in json.loads(p["text"])
    ]
    assert len(documents) == 20
    assert all("tables" not in p and "links" not in p and "images" not in p for p in documents)
    assert all(p["text"].count("precise original facts") == 1000 for p in documents)
    assert calls[0]["tools"] == [] and calls[0]["model"] == store.config.profile("luna")["model"]
    receipt = store.one("SELECT * FROM model_calls WHERE purpose='web_review'")
    assert (
        store.one("SELECT turn_id FROM model_call_turns WHERE call_id=?", (receipt["id"],))[
            "turn_id"
        ]
        == result["search_receipt"]["turn_id"]
    )
    envelope = store.get(
        store.one(
            "SELECT envelope_hash FROM items WHERE kind='function_call_output' ORDER BY id DESC"
        )["envelope_hash"]
    )
    assert any(p["type"] == "input_image" for p in envelope["output"])
    assert all("base64" not in p.get("text", "") for p in envelope["output"])
    view = json.loads(envelope["output"][0]["text"])
    assert view["curation"]["status"] == "found" and len(view["source"]["web_sources"]) == 3
    assert view["curation"]["evidence"][0]["verified_location"]["path"] == view["full_result_path"]
    assert len(view["curation"]["evidence"][0]["matching_sources"]) == 3
    assert sum(len(p.get("text", "")) for p in envelope["output"]) < 50000
    assert "precise original facts\n" * 100 not in json.dumps(envelope)
    again = await Web(store, runtime.models, Pages).search(
        "astra", task, op["id"], "evidence needle"
    )
    assert again == result and len(calls) == 2 and len(Pages.captures) == 20
    # Recovery after review completion but before output publication must not
    # require browser access, or purchase a second review.
    store.db.execute("UPDATE web_runs SET result_hash=NULL WHERE operation_id=?", (op["id"],))

    def no_browser(*args):
        raise AssertionError("Settled captures must be reused")

    recovered = await Web(store, runtime.models, no_browser).search(
        "astra", task, op["id"], "evidence needle"
    )
    assert recovered["selected"] == result["selected"] and len(calls) == 2


async def test_redirect_failure_retries_once_and_does_not_poison_later_pages(store, monkeypatch):
    from bloom.safari import NavigationChanged

    runtime = Runtime(store)
    task = store.new_task("Redirect resilience")
    reviewer(store, runtime, monkeypatch)

    class RedirectPages(Pages):
        changed = False
        resets = 0

        async def capture(self, url, destination, **geometry):
            if url.endswith("/6") and not self.changed:
                self.changed = True
                raise NavigationChanged("Automatic challenge redirect")
            return await super().capture(url, destination, **geometry)

        async def reset(self):
            self.resets += 1

    runtime.tools.web.browser_factory = RedirectPages
    result = await runtime.tools.web.search("astra", task, "redirect-test", "evidence")
    assert result["status"] == "ok" and len(Pages.captures) == 20
    assert len(store.rows("SELECT id FROM events WHERE kind='web.navigation_retry'")) == 1


@pytest.mark.parametrize("failure", ["missing_decision", "provider"])
async def test_review_failure_preserves_all_candidates_without_fabricated_selection(
    store, monkeypatch, failure
):
    runtime = Runtime(store)
    task = store.new_task("Retain evidence if review fails")
    calls = reviewer(
        store,
        runtime,
        monkeypatch,
        invalid=failure == "missing_decision",
        fail=failure == "provider",
    )
    Pages.fail_rank = 7
    result = await runtime.tools.web.search("astra", task, "operation-review-failure", "evidence")
    assert (
        result["status"] == "review_failed"
        and result["selected"] == []
        and len(result["candidates"]) == 20
    )
    assert result["candidates"][6]["status"] == "capture_failed"
    assert all(Path(s["metadata_path"]).is_file() for s in result["candidates"])
    again = await runtime.tools.web.search("astra", task, "operation-review-failure", "evidence")
    assert again == result and len(calls) == (2 if failure == "missing_decision" else 1)


async def test_saved_captures_scope_integrity_and_file_image_transport(store, monkeypatch):
    runtime = Runtime(store)
    task = store.new_task("Image file behavior")
    reviewer(store, runtime, monkeypatch)
    page = await runtime.tools.web.open("astra", task, "open-image", "https://evidence.test/1")
    p = page["page"]["images"][0]["path"]
    op = await invoke(runtime, store, task, "read_file", {"path": p}, "image-read")
    assert op["status"] == "completed" and store.get(op["result_hash"])["_mime"] == "image/png"
    envelope = store.get(
        store.one(
            "SELECT envelope_hash FROM items WHERE kind='function_call_output' ORDER BY id DESC"
        )["envelope_hash"]
    )
    native = [p for p in envelope["output"] if p["type"] == "input_image"][0]
    assert base64.b64decode(native["image_url"].split(",", 1)[1]) == png()
    by_hash = await invoke(
        runtime,
        store,
        task,
        "read_artifact",
        {"hash": page["page"]["images"][0]["hash"]},
        "image-hash",
    )
    assert (
        by_hash["status"] == "completed"
        and store.get(by_hash["result_hash"])["_mime"] == "image/png"
    )
    bad = await invoke(runtime, store, task, "read_file", {"path": p, "start": 1}, "image-lines")
    assert bad["status"] == "failed"
    sol = await runtime.request(
        "astra", task, "sol", {"operation": "delegate", "role": "sol", "objective": "Scoped child"}
    )
    with pytest.raises(PermissionError, match="agent tree"):
        saved_file(store, sol["agent_id"], p)
    Path(p).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="SHA-256"):
        read_saved(store, saved_file(store, "astra", p))


@pytest.mark.parametrize("role", ["astra", "sol", "luna"])
async def test_large_dom_inventories_never_reach_any_role_or_trigger_curation(
    store, monkeypatch, role
):
    runtime = Runtime(store)
    task = store.new_task("Find the precise original facts in the table")
    calls = reviewer(store, runtime, monkeypatch)
    if role != "astra":
        child = await runtime.request(
            "astra",
            task,
            "child",
            {
                "operation": "delegate",
                "role": role,
                "objective": "Read the page",
                "network": True,
                "purpose": "evidence",
            },
        )
        task = child["task_id"]

    class TablePages(Pages):
        async def capture(self, *args, **kwargs):
            page = await super().capture(*args, **kwargs)
            page["text"] = (
                "Complete discussion.\n| Fact | Value |\n| --- | --- |\n| precise original facts | 42 |"
            )
            page["tables"] = [
                {"rows": [["duplicated-layout-inventory", str(i)] for i in range(3000)]}
            ]
            page["links"] = [
                {"text": "reply", "url": f"https://evidence.test/reply?id={i}"} for i in range(3000)
            ]
            page["future_dom_inventory"] = ["future-metadata-must-stay-on-disk"] * 3000
            return page

    runtime.tools.web.browser_factory = TablePages
    op = await invoke(
        runtime,
        store,
        task,
        "web_open",
        {"url": "https://evidence.test/1", "include_image": True},
        "table-page",
    )
    assert len(store.get(op["result_hash"])["page"]["text"]) < 50000
    assert not calls
    original = store.get(op["result_hash"])
    assert (
        original["page"]["tables"]
        and original["page"]["links"]
        and original["page"]["future_dom_inventory"]
    )
    result = store.get(
        store.one(
            "SELECT envelope_hash FROM items WHERE kind='function_call_output' ORDER BY id DESC"
        )["envelope_hash"]
    )
    text = "\n".join(p.get("text", "") for p in result["output"])
    assert len(text) < 5000 and "precise original facts" in text
    assert (
        "duplicated-layout-inventory" not in text
        and "future-metadata-must-stay-on-disk" not in text
    )
    assert "/reply?id=" not in text and '"tables"' not in text and '"links"' not in text
    assert sum(p["type"] == "input_image" for p in result["output"]) == 1


async def test_curation_receives_one_document_and_recovers_original_segment_coordinates(
    store, monkeypatch
):
    from bloom import results

    runtime = Runtime(store)
    task = store.new_task("Find precise original facts")
    calls = reviewer(store, runtime, monkeypatch)

    class LargePages(Pages):
        async def capture(self, *args, **kwargs):
            page = await super().capture(*args, **kwargs)
            page["text"] += "preserve the entire legitimate discussion\n" * 2000
            page["tables"] = [{"text": "DUPLICATE-LAYOUT-MARKER" * 10000}]
            page["links"] = [
                {"url": "https://evidence.test/reply", "text": "LINK-INVENTORY-MARKER"}
            ] * 1000
            return page

    runtime.tools.web.browser_factory = LargePages
    op = await invoke(runtime, store, task, "web_open", {"url": "https://evidence.test/1"}, "large")
    assert len(calls) == 1
    data = json.loads(calls[0]["input"][0]["content"])
    serialized = json.dumps(data)
    assert "DUPLICATE-LAYOUT-MARKER" not in serialized and "LINK-INVENTORY-MARKER" not in serialized
    row = store.one("SELECT * FROM tool_result_files WHERE operation_id=?", (op["id"],))
    saved = Path(row["path"]).read_bytes()
    assert saved.count(b"preserve the entire legitimate discussion") == 2000
    assert store.get(row["summary_hash"])["status"] == "found"
    # Simulate an upgrade after accepted curation but before summary publication.
    store.db.execute("DELETE FROM research_views WHERE operation_id=?", (op["id"],))
    store.db.execute(
        "UPDATE tool_result_files SET summary_hash=NULL,status='prepared' WHERE operation_id=?",
        (op["id"],),
    )
    monkeypatch.setattr(
        results,
        "text_of",
        lambda raw: ("new serialization without the accepted quote", "browser_sources"),
    )
    call = store.one("SELECT * FROM tool_calls WHERE operation_id=?", (op["id"],))
    view = await runtime.results.present(
        "astra", task, call, store.get(op["result_hash"]), op["result_hash"]
    )
    assert view["curation"]["status"] == "found" and len(calls) == 1
    loc = view["curation"]["evidence"][0]["verified_location"]
    assert saved.decode()[loc["start_char"] : loc["end_char"]] == "precise original facts"
    assert Path(row["path"]).read_bytes() == saved


def test_text_browser_adapter_does_not_advertise_multimodal_web_tools(store):
    store.config.data["providers"]["text_browser"] = {"kind": "browser"}
    store.config.data["profiles"]["astra"]["provider"] = "text_browser"
    names = {s["name"] for s in Runtime(store).tools.schemas("astra")}
    assert (
        "read_file" in names and not {"web_search", "web_open", "view_image", "web_fetch"} & names
    )


def test_remote_file_image_path_emits_image_bytes_without_text_decoding(tmp_path):
    from bloom.remote_files import handle

    p = tmp_path / "photo.png"
    p.write_bytes(png())
    result = handle({"root": str(tmp_path), "path": str(p), "name": "read_file"})
    assert result["_mime"] == "image/png" and base64.b64decode(result["_image"]) == png()
    with pytest.raises(ValueError, match="text-only"):
        handle({"root": str(tmp_path), "path": str(p), "name": "read_file", "start": 1})


async def test_fetch_pdf_is_not_decoded_into_model_text(store, monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=b"%PDF-1.7\nfixture bytes",
                    headers={"content-type": "application/pdf"},
                )
            ),
            **kwargs,
        ),
    )
    runtime = Runtime(store)
    task = store.new_task("Explicit PDF limitation")
    result = await runtime.tools.run(
        "astra", task, "pdf-fetch", "web_fetch", {"url": "https://evidence.test/paper.pdf"}
    )
    assert result["status"] == "unsupported_pdf" and "text" not in result
    assert store.bytes(result["original_artifact"]).startswith(b"%PDF-")


def test_chat_preserves_images_and_waits_until_all_tool_results():
    uri = "data:image/png;base64," + base64.b64encode(png()).decode()
    items = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Inspect this"},
                {"type": "input_image", "image_url": uri, "detail": "high"},
            ],
        },
        *[
            {"type": "function_call", "call_id": i, "name": "read_file", "arguments": "{}"}
            for i in ("a", "b")
        ],
        {
            "type": "function_call_output",
            "call_id": "a",
            "output": [
                {"type": "input_text", "text": "image metadata"},
                {"type": "input_image", "image_url": uri},
            ],
        },
        {"type": "function_call_output", "call_id": "b", "output": "plain text"},
    ]
    messages = chat_messages("instructions", items)
    assert messages[1]["content"][1]["type"] == "image_url"
    assert len(messages[2]["tool_calls"]) == 2
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "tool", "user"]
    assert messages[-1]["content"][1]["image_url"]["url"] == uri


async def test_web_evidence_archival_is_pinned_searchable_and_readable(store, archive, monkeypatch):
    runtime = Runtime(store)
    task = store.new_task("Cold web evidence")
    reviewer(store, runtime, monkeypatch)
    page = await runtime.tools.web.open("astra", task, "open-archive", "https://evidence.test/1")
    paths = [Path(page["page"]["text_path"]), Path(page["page"]["images"][0]["path"])]
    stamp = time.time() - 9 * 86400
    for p in paths:
        os.utime(p, (stamp, stamp))
    store.db.execute("UPDATE web_files SET accessed_at=?", (stamp,))
    assert all(not archive.eligible(p, time.time() - 7 * 86400, archive.pins()) for p in paths)
    store.status(task, "completed")
    # Archive only named files and their source objects, leaving other fixture artifacts hot.
    for row in store.rows("SELECT hash FROM web_files"):
        p = store.artifact_path(row["hash"])
        os.utime(p, (stamp, stamp))
    store.db.execute("UPDATE artifact_access SET accessed_at=?", (stamp,))
    monkeypatch.setattr(Archive, "volume_ready", lambda self: archive.online["value"])
    result = archive.sweep()
    assert result["status"] == "completed" and all(not p.exists() for p in paths)
    assert archive.search("precise original facts")
    archive.online["value"] = False
    assert archive.search("precise original facts")
    with pytest.raises(ColdStorageUnavailable):
        read_saved(store, saved_file(store, "astra", str(paths[0])))
    archive.online["value"] = True
    assert read_saved(store, saved_file(store, "astra", str(paths[1]))) == png()
