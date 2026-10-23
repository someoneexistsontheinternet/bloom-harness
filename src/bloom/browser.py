"""Persistent browser inference through an explicitly configured WebDriver session.

Only visible DOM content is read. No internal ChatGPT endpoints or cookie export.
The selected model/effort labels must be supplied and verified in the UI.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

import httpx

from .privacy import clean
from .store import encoded, now


class Browser:
    def __init__(self, store, provider, transport=None):
        self.store = store
        self.provider = provider
        self.transport = transport

    async def rpc(self, method, path, body=None):
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            response = await client.request(
                method, self.provider["base_url"].rstrip("/") + path, json=body
            )
            response.raise_for_status()
            value = response.json().get("value")
            if isinstance(value, dict) and value.get("error"):
                raise RuntimeError("WebDriver: " + value["error"] + ": " + value.get("message", ""))
            return value

    async def script(self, session, script, args=None):
        return await self.rpc(
            "POST", "/session/" + session + "/execute/sync", {"script": script, "args": args or []}
        )

    async def complete(self, agent, call, profile, body):
        selectors = self.provider.get("selectors", {})
        needed = ("prompt", "submit", "assistant", "busy", "model")
        if any(not selectors.get(x) for x in needed):
            raise RuntimeError(
                "Browser provider needs inspected DOM selectors: " + ", ".join(needed)
            )
        expected = profile.get("browser_model_label")
        if not expected:
            raise RuntimeError(
                "Configure browser_model_label to verify the actual selected UI model"
            )
        state = self.store.one("SELECT * FROM browser_sessions WHERE agent_id=?", (agent,))
        if state and state["pending_call"]:
            raise RuntimeError(
                "Browser has an unacknowledged prior submission; inspect it before another send"
            )
        if state:
            session = state["driver_session"]
            # A dead session is a recoverable setup gap, never permission to replay a prompt.
            await self.rpc("GET", "/session/" + session + "/url")
        else:
            value = await self.rpc(
                "POST",
                "/session",
                {
                    "capabilities": {
                        "alwaysMatch": {"browserName": self.provider.get("browser_name", "safari")}
                    }
                },
            )
            session = value["sessionId"]
            await self.rpc(
                "POST",
                "/session/" + session + "/url",
                {"url": self.provider.get("url", "https://chatgpt.com/")},
            )
            self.store.db.execute(
                "INSERT INTO browser_sessions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    agent,
                    session,
                    self.provider.get("url", "https://chatgpt.com/"),
                    0,
                    "",
                    None,
                    None,
                    None,
                    now(),
                ),
            )
            state = self.store.one("SELECT * FROM browser_sessions WHERE agent_id=?", (agent,))
        label = await self.script(
            session,
            "return document.querySelector(arguments[0])?.innerText || null",
            [selectors["model"]],
        )
        if not label or expected not in label:
            raise RuntimeError("Browser model is not verified; expected visible label " + expected)
        effort = profile.get("browser_effort_label")
        if effort:
            actual = await self.script(
                session,
                "return document.querySelector(arguments[0])?.innerText || null",
                [selectors.get("effort", "")],
            )
            if not actual or effort not in actual:
                raise RuntimeError("Browser effort selection is not verified")
        items = body["input"]
        if state["input_count"]:
            prefix = hashlib.sha256(encoded(items[: state["input_count"]])).hexdigest()
            if prefix != state["prefix_hash"]:
                raise RuntimeError(
                    "Browser context prefix changed after compaction/branching; deliberately bind a new browser conversation"
                )
            delta = items[state["input_count"] :]
        else:
            delta = items
        # Images require a configured upload port, rather than silently becoming text.
        if any("input_image" in json.dumps(item) for item in delta):
            raise RuntimeError(
                "This browser route has no verified image upload port; use the Responses route for visual CUA"
            )
        template = self.provider.get(
            "prompt_template",
            "{instructions}\n\nTools: {tools}\n\nNew conversation items: {items}\n\nReply as a JSON object with messages (text and phase: commentary or final_answer) and calls (name and arguments). Do not execute tools yourself.",
        )
        prompt = template.format(
            instructions=body["instructions"],
            tools=json.dumps(body["tools"], ensure_ascii=False),
            items=json.dumps(delta, ensure_ascii=False),
        )
        count = await self.script(
            session,
            "return document.querySelectorAll(arguments[0]).length",
            [selectors["assistant"]],
        )
        field = await self.rpc(
            "POST",
            "/session/" + session + "/element",
            {"using": "css selector", "value": selectors["prompt"]},
        )
        element = field["element-6066-11e4-a52e-4f735466cecf"]
        await self.rpc("POST", f"/session/{session}/element/{element}/clear", {})
        await self.rpc("POST", f"/session/{session}/element/{element}/value", {"text": prompt})
        # Journal before the actual send. Lost acknowledgements are never blindly replayed.
        self.store.db.execute(
            "UPDATE browser_sessions SET pending_call=?,model_label=?,effort_label=?,updated_at=? WHERE agent_id=?",
            (call, label, effort, now(), agent),
        )
        button = await self.rpc(
            "POST",
            "/session/" + session + "/element",
            {"using": "css selector", "value": selectors["submit"]},
        )
        await self.rpc(
            "POST",
            f"/session/{session}/element/{button['element-6066-11e4-a52e-4f735466cecf']}/click",
            {},
        )
        started = time.monotonic()
        while time.monotonic() - started < self.provider.get("timeout_seconds", 1200):
            snapshot = await self.script(
                session,
                'const nodes=[...document.querySelectorAll(arguments[0])];return {count:nodes.length,text:nodes.at(-1)?.innerText||"",busy:!!document.querySelector(arguments[1]),url:location.href}',
                [selectors["assistant"], selectors["busy"]],
            )
            if snapshot["count"] > count and snapshot["text"] and not snapshot["busy"]:
                raw = snapshot["text"].strip()
                if raw.startswith("```") and raw.endswith("```"):
                    raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
                self.store.event(
                    "browser.public_reply",
                    {"call": call, "text": clean(snapshot["text"]), "url": snapshot["url"]},
                    agent=agent,
                )
                value = json.loads(raw)
                output = []
                for message in value.get("messages", []):
                    if message["phase"] not in ("commentary", "final_answer"):
                        raise ValueError("Invalid browser message phase")
                    output.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "phase": message["phase"],
                            "content": [{"type": "output_text", "text": message["text"]}],
                        }
                    )
                for index, tool in enumerate(value.get("calls", [])):
                    output.append(
                        {
                            "type": "function_call",
                            "call_id": call + ":" + str(index),
                            "name": tool["name"],
                            "arguments": json.dumps(tool["arguments"]),
                        }
                    )
                prefix = items + output
                self.store.db.execute(
                    "UPDATE browser_sessions SET conversation_url=?,input_count=?,prefix_hash=?,pending_call=NULL,updated_at=? WHERE agent_id=?",
                    (
                        snapshot["url"],
                        len(prefix),
                        hashlib.sha256(encoded(prefix)).hexdigest(),
                        now(),
                        agent,
                    ),
                )
                return {
                    "id": call,
                    "status": "completed",
                    "output": output,
                    "usage": None,
                    "model": None,
                    "browser_metadata": {
                        "visible_model_label": label,
                        "visible_effort_label": effort,
                        "url": snapshot["url"],
                        "wire_format": "bloom-browser-json-v1",
                    },
                }
            await asyncio.sleep(1)
        raise TimeoutError("Browser reply not acknowledged; pending submission retained")
