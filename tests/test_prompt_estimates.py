import base64
import json
import struct

import httpx
import pytest

from bloom.context import Context
from bloom.models import Models
from bloom.privacy import register
from bloom.token_estimate import Estimator, image_tokens


def png_url(width, height):
    header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", width, height)
    return "data:image/png;base64," + base64.b64encode(header).decode()


def test_native_image_dimensions_and_detail_are_estimated_without_wire_changes(store):
    image = {"type": "input_image", "image_url": png_url(512, 2048), "detail": "high"}
    before = json.dumps(image)
    assert image_tokens(image, "gpt-6-astra") == (1229, "astra_patch_estimate")
    assert image_tokens({**image, "detail": "low"}, "gpt-6-astra")[0] == 77
    assert image_tokens({**image, "image_url": png_url(2048, 2048)}, "gpt-6-astra")[0] == 3000
    assert image_tokens(image, "unknown-model")[1] == "unknown_model_or_dimensions"
    assert Context(store, None).count([image]) < 1300
    assert json.dumps(image) == before


async def test_reported_prefix_anchors_estimate_and_ciphertext_length_never_counts(
    store, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    reasoning = {
        "type": "reasoning",
        "id": "rs_count",
        "summary": [],
        "encrypted_content": "ciphertext",
    }
    response = {
        "status": "completed",
        "output": [
            reasoning,
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done"}],
            },
        ],
        "usage": {
            "input_tokens": 40000,
            "output_tokens": 1000,
            "output_tokens_details": {"reasoning_tokens": 980},
        },
    }
    store.config.data["providers"]["openai"]["stream"] = False
    model = Models(store, httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    task = store.new_task("estimate")
    call, _ = await model.infer(
        "astra",
        task,
        "same",
        [{"role": "user", "content": "original"}],
        [],
        profile_override={"provider": "openai"},
    )
    store.item("astra", reasoning, task, call)
    counter = Estimator(store)
    small = counter.measure([reasoning])
    large = counter.measure([{**reasoning, "encrypted_content": "encoded" * 100000}])
    assert small == large and small["reasoning"] == 980 and small["unknown_reasoning"] == 0
    items = [
        {"role": "user", "content": "original"},
        reasoning,
        {"role": "user", "content": "new input"},
    ]
    sized = counter.request("astra", "same", [], items, store.config.profile("astra")["model"])
    assert sized["method"] == "reported_prefix_plus_new_items" and 40980 < sized["tokens"] < 41100
    assert (
        counter.request("astra", "changed", [], items, store.config.profile("astra")["model"])[
            "method"
        ]
        == "local_structured_estimate"
    )


async def test_empty_transport_error_keeps_type_and_sanitized_diagnostics(store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    secret = "fixture-secret-do-not-persist"
    register(secret)

    def fail(request):
        try:
            raise ValueError(secret)
        except ValueError as cause:
            raise httpx.ReadError("") from cause

    model = Models(store, httpx.MockTransport(fail))
    task = store.new_task("transport")
    with pytest.raises(httpx.ReadError):
        await model.infer(
            "astra", task, "test", [], [], profile_override={"provider": "openai"}, retry_limit=0
        )
    row = store.one("SELECT * FROM model_calls")
    assert row["status"] == "uncertain" and "ReadError" in row["error"]
    detail = store.get(store.one("SELECT detail_hash FROM model_call_diagnostics")["detail_hash"])
    assert detail["exception"]["type"] == "httpx.ReadError"
    assert secret not in json.dumps(detail) and "[REDACTED]" in json.dumps(detail)
    assert not store.rows("SELECT * FROM tool_calls")
