import hashlib
import json
from pathlib import Path

import httpx
import pytest

from bloom.runtime import Runtime
from bloom.results import saved_file
from test_control_provenance import invoke


def last_result(store):
    return json.loads(
        store.one("SELECT text FROM items WHERE kind='function_call_output' ORDER BY id DESC")[
            "text"
        ]
    )["result"]


def curator(
    runtime, store, monkeypatch, status="found", fail=False, coverage="Entire supplied result."
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    store.config.data["profiles"]["luna"]["provider"] = "openai"
    store.config.data["providers"]["openai"]["stream"] = False
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if fail:
            return httpx.Response(503, json={"error": "fixture failure"})
        text = json.dumps(
            {
                "status": status,
                "summary": "Found the relevant needle.",
                "evidence": [{"quote": "needle", "location": "source section"}],
                "coverage": coverage,
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "fixture-curation",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "usage": {
                    "input_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 5},
                    "output_tokens": 10,
                },
            },
        )

    runtime.models.transport = httpx.MockTransport(handler)
    return requests


async def test_character_threshold_context_saved_metadata_and_restart_reuse(store, monkeypatch):
    runtime = Runtime(store)
    task = store.new_task("Find the needle in this output")
    requests = curator(runtime, store, monkeypatch)
    (store.config.root / "small.txt").write_text("汉" * 50000)
    await invoke(runtime, store, task, "read_file", {"path": "small.txt"}, "small")
    assert len(last_result(store)["text"]) == 50000 and not requests
    source = "汉" * 50001 + "\nneedle\n"
    (store.config.root / "large.txt").write_text(source)
    operation = await invoke(
        runtime, store, task, "read_file", {"path": "large.txt", "exact": True}, "large"
    )
    result = last_result(store)
    path = Path(result["full_result_path"])
    assert path.read_text() == source and not result["raw_bypass_available"]
    assert path.name.endswith("-read_file.txt") and operation["id"] in path.name
    assert result["source"]["start"] == 1 and result["source"]["line_count"] == 2
    meta = json.loads(Path(result["metadata_path"]).read_text())
    assert meta["text_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert meta["turn_id"] and path.name.startswith(
        hashlib.sha256(meta["turn_id"].encode()).hexdigest()[:4]
    )
    assert meta["curation_status"] == "found" and meta["curator_call"]
    evidence = json.loads(requests[0]["input"][0]["content"])
    assert evidence["owner_question"] == "Find the needle in this output"
    assert (
        "".join(x["text"] for x in evidence["source_segments"]) == source
        and requests[0]["tools"] == []
    )
    tool = store.one("SELECT * FROM tool_calls WHERE operation_id=?", (operation["id"],))
    # Crash after the provider completion but before publishing its derived view.
    store.db.execute(
        "UPDATE tool_result_files SET status='prepared',summary_hash=NULL WHERE operation_id=?",
        (operation["id"],),
    )
    await Runtime(store, models=runtime.models).execute(task, tool)
    await invoke(
        runtime, store, task, "read_file", {"path": str(path), "exact": True}, "denied-raw"
    )
    assert last_result(store)["curation"]["status"] == "found" and len(requests) == 1
    await invoke(
        runtime, store, task, "read_file", {"path": str(path), "start": 2, "end": 2}, "small-range"
    )
    assert last_result(store)["text"] == "needle\n" and len(requests) == 1
    await invoke(
        runtime, store, task, "read_artifact", {"hash": operation["result_hash"]}, "artifact"
    )
    assert "full_result_path" in last_result(store) and len(requests) == 2


async def test_failed_luna_allows_only_saved_file_raw_fallback_and_hash_checks(store, monkeypatch):
    runtime = Runtime(store)
    task = store.new_task("Find evidence")
    requests = curator(runtime, store, monkeypatch, fail=True)
    source = "line with evidence\n" * 4000
    (store.config.root / "large.txt").write_text(source)
    await invoke(runtime, store, task, "read_file", {"path": "large.txt"}, "large")
    result = last_result(store)
    path = Path(result["full_result_path"])
    assert result["raw_bypass_available"] and len(requests) == 1
    await invoke(runtime, store, task, "read_file", {"path": str(path), "exact": True}, "fallback")
    assert last_result(store)["text"] == source and len(requests) == 1
    await invoke(
        runtime, store, task, "read_file", {"path": "large.txt", "exact": True}, "original"
    )
    assert "full_result_path" in last_result(store) and len(requests) == 2
    path.write_text("tampered")
    bad = await invoke(
        runtime, store, task, "read_file", {"path": str(path), "exact": True}, "tampered"
    )
    assert bad["status"] == "failed" and "SHA-256" in last_result(store)["error"]
    schema = {s["name"]: s for s in runtime.tools.schemas("astra")}
    assert "exact" not in schema["web_fetch"]["parameters"]["properties"]
    assert "exact" not in schema["observe_process"]["parameters"]["properties"]


async def test_luna_workers_are_exempt_and_sibling_saved_files_are_scoped(store, monkeypatch):
    runtime = Runtime(store)
    task = store.new_task("Evidence assignment")
    requests = curator(runtime, store, monkeypatch)
    (store.config.root / "large.txt").write_text("candidate evidence\n" * 4000)
    sol = await runtime.request(
        "astra", task, "sol", {"operation": "delegate", "role": "sol", "objective": "Read evidence"}
    )
    luna = await runtime.request(
        sol["agent_id"],
        sol["task_id"],
        "luna",
        {"operation": "delegate", "role": "luna", "objective": "Read evidence"},
    )
    await invoke(runtime, store, luna["task_id"], "read_file", {"path": "large.txt"}, "luna-large")
    assert len(last_result(store)["text"]) > 50000 and not requests
    await invoke(runtime, store, task, "read_file", {"path": "large.txt"}, "astra-large")
    path = last_result(store)["full_result_path"]
    with pytest.raises(PermissionError):
        saved_file(store, luna["agent_id"], path)


async def test_structured_coverage_is_preserved_and_quote_lines_are_resolved_by_backend(
    store, monkeypatch
):
    runtime = Runtime(store)
    task = store.new_task("Find the needle")
    coverage = {"reviewed": "All supplied text", "limitations": ["No unseen pages inspected"]}
    curator(runtime, store, monkeypatch, coverage=coverage)
    source = "background\n" * 6000 + "needle\n"
    (store.config.root / "large.txt").write_text(source)
    await invoke(runtime, store, task, "read_file", {"path": "large.txt"}, "structured")
    result = last_result(store)
    assert result["curation"]["coverage"] == coverage
    assert result["curation"]["evidence"][0]["verified_location"]["start_line"] == 6001
    assert not result["raw_bypass_available"]


@pytest.mark.parametrize("corrected", [True, False])
async def test_invalid_context_quote_gets_one_receipted_correction_then_exact_validation(
    store, monkeypatch, corrected
):
    runtime = Runtime(store)
    task = store.new_task("A context claim is not source evidence")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    store.config.profile("luna")["provider"] = "openai"
    store.config.data["providers"]["openai"]["stream"] = False
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        source = json.loads(body["input"][0]["content"])
        quote = (
            "needle"
            if corrected and len(requests) == 2
            else "A context claim is not source evidence"
        )
        if len(requests) == 2:
            assert "correction" in source and "source_segments ONLY" in body["instructions"]
        result = {
            "status": "found",
            "summary": "Review of the source",
            "evidence": [{"quote": quote, "location": "source"}],
            "coverage": "Captured source only",
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

    runtime.models.transport = httpx.MockTransport(handler)
    source = "background\n" * 6000 + "needle\n"
    (store.config.root / "large.txt").write_text(source)
    op = await invoke(runtime, store, task, "read_file", {"path": "large.txt"}, "correct")
    result = last_result(store)
    assert len(requests) == 2 and result["curation"]["status"] == (
        "found" if corrected else "unable"
    )
    assert Path(result["full_result_path"]).read_text() == source
    if corrected:
        assert result["curation"]["evidence"][0]["verified_location"]["start_line"] == 6001
    else:
        assert result["raw_bypass_available"] and not result["curation"]["evidence"]
    before = store.rows("SELECT id,response_hash FROM model_calls WHERE purpose='curation'")
    store.db.execute("UPDATE tool_result_files SET status='prepared',summary_hash=NULL")
    await runtime.execute(
        task, store.one("SELECT * FROM tool_calls WHERE operation_id=?", (op["id"],))
    )
    assert (
        len(requests) == 2
        and store.rows("SELECT id,response_hash FROM model_calls WHERE purpose='curation'")
        == before
    )


def test_segment_selection_copies_original_bytes_and_rejects_fabricated_ids_or_quotes(store):
    from bloom.results import Results

    results = Results(store, None, lambda value: 0)
    text = "Repeated passage. " * 100 + "\nExact needle after the repeated passages."
    row = {"path": "/retained/evidence.txt"}

    def response(evidence):
        value = {
            "status": "found",
            "summary": "Relevant source",
            "evidence": evidence,
            "coverage": "Supplied segments",
        }
        return {"output": [{"type": "message", "content": [{"text": json.dumps(value)}]}]}

    selected = results.validate(
        response([{"segment": 1, "reason": "Relevant original context"}]), text, {}, row
    )["evidence"][0]
    assert (
        selected["quote"] == text[1000:2000] and selected["verified_location"]["start_char"] == 1000
    )
    assert (
        text[
            selected["verified_location"]["start_char"] : selected["verified_location"]["end_char"]
        ]
        == selected["quote"]
    )
    for invalid in (
        {"segment": 99},
        {"segment": True},
        {"segment": 1, "quote": "invented paraphrase"},
    ):
        with pytest.raises(ValueError):
            results.validate(response([invalid]), text, {}, row)
