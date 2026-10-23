import json

import httpx
import pytest

from bloom.browser import Browser


async def test_browser_checks_visible_model_and_keeps_conversation(store):
    state = {"count": 0, "sent": 0}

    def handler(request):
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path == "/session":
            value = {"sessionId": "browser-session"}
        elif path.endswith("/execute/sync"):
            script = body["script"]
            if "?.innerText || null" in script:
                value = "Selected model: Example Pro"
            elif "querySelectorAll(arguments[0]).length" in script:
                value = state["count"]
            else:
                value = {
                    "count": state["count"],
                    "busy": False,
                    "url": "https://example.test/c/persistent",
                    "text": json.dumps(
                        {
                            "messages": [{"phase": "final_answer", "text": "A visible reply"}],
                            "calls": [],
                        }
                    ),
                }
        elif path.endswith("/element"):
            value = {"element-6066-11e4-a52e-4f735466cecf": "element"}
        elif path.endswith("/click"):
            state["count"] += 1
            state["sent"] += 1
            value = None
        else:
            value = "https://example.test/c/persistent" if request.method == "GET" else None
        return httpx.Response(200, json={"value": value})

    provider = {
        "base_url": "http://127.0.0.1:9451",
        "url": "https://example.test/",
        "selectors": {k: k for k in ("prompt", "submit", "assistant", "busy", "model")},
    }
    browser = Browser(store, provider, httpx.MockTransport(handler))
    profile = {"browser_model_label": "Example Pro"}
    body = {"instructions": "A test", "tools": [], "input": [{"role": "user", "content": "First"}]}
    first = await browser.complete("astra", "browser-call-1", profile, body)
    second = {
        "instructions": "A test",
        "tools": [],
        "input": body["input"] + first["output"] + [{"role": "user", "content": "Second"}],
    }
    await browser.complete("astra", "browser-call-2", profile, second)
    assert state["sent"] == 2
    assert (
        store.one("SELECT driver_session FROM browser_sessions")["driver_session"]
        == "browser-session"
    )
    assert first["model"] is None and first["usage"] is None
    assert first["browser_metadata"]["visible_model_label"] == "Selected model: Example Pro"
    with pytest.raises(RuntimeError, match="not verified"):
        await browser.complete("astra", "wrong-model", {"browser_model_label": "Different"}, second)
    assert state["sent"] == 2


def test_image_transport_is_not_counted_as_base64_text_tokens(store):
    from bloom.context import Context

    context = Context(store, None)
    item = [
        {
            "type": "function_call_output",
            "output": [
                {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 4_000_000}
            ],
        }
    ]
    assert context.count(item) < 9000
