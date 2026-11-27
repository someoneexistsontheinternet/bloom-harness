"""One model caller: independent requests, durable budgets and no uncertain retries."""

import asyncio
import base64
import json
import os
from pathlib import Path
import time

import httpx

from .store import encoded, uid
from .tokens import count


class ModelFailure(RuntimeError):
    def __init__(self, message, *, uncertain=True, trace=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.trace = trace or []


class BudgetExceeded(RuntimeError):
    pass


def final_text(response):
    if response.get("status", "completed") != "completed":
        return ""
    messages = [i for i in response.get("output", []) if i.get("type") == "message"]
    if any(i.get("phase") for i in messages):
        messages = [i for i in messages if i.get("phase") == "final_answer"]
    return "\n\n".join(
        p["text"] for i in messages for p in i.get("content", []) if p.get("type") == "output_text"
    )


def visible(value):
    if isinstance(value, dict):
        return {
            k: visible(v) for k, v in value.items() if k not in ("encrypted_content", "image_url")
        }
    if isinstance(value, list):
        return [visible(v) for v in value]
    return value


class Responses:
    simulated = False

    def __init__(
        self,
        kind="codex",
        *,
        base_url=None,
        timeout=180,
        deadline=900,
        transport=None,
        output_tokens=32768,
    ):
        if kind not in ("codex", "openai"):
            raise ValueError("Choose codex or openai")
        self.kind = kind
        self.base = (
            base_url
            or (
                "https://chatgpt.com/backend-api/codex"
                if kind == "codex"
                else "https://api.openai.com/v1"
            )
        ).rstrip("/")
        self.timeout = timeout
        self.deadline = deadline
        self.transport = transport
        self.output_tokens = output_tokens
        self.secrets = []

    def clean(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
        elif isinstance(value, dict):
            value = {k: self.clean(v) for k, v in value.items()}
        elif isinstance(value, list):
            value = [self.clean(v) for v in value]
        return value

    def headers(self):
        if self.kind == "openai":
            token = os.environ.get("OPENAI_API_KEY")
            if not token:
                raise ModelFailure("OPENAI_API_KEY is not configured", uncertain=False)
            self.secrets = [token]
            return {"Authorization": "Bearer " + token}
        path = Path(os.environ.get("BLOOM_CODEX_AUTH_FILE", "~/.codex/auth.json")).expanduser()
        try:
            tokens = json.loads(path.read_bytes())["tokens"]
            token = tokens["access_token"]
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (OSError, ValueError, KeyError, IndexError) as exc:
            raise ModelFailure("A valid existing Codex login is required", uncertain=False) from exc
        self.secrets = [v for v in tokens.values() if isinstance(v, str) and v]
        if claims.get("exp", 0) < time.time() + 60:
            raise ModelFailure("Codex login expired; refresh it with Codex", uncertain=False)
        return {
            "Authorization": "Bearer " + token,
            "ChatGPT-Account-Id": tokens.get("account_id", ""),
            "originator": "bloom_v41",
            "User-Agent": "bloom-v41-experimental/0.1.0",
        }

    def prepare(self, request):
        body = dict(request)
        if self.kind != "codex":
            body["max_output_tokens"] = self.output_tokens
        return body

    async def generate(self, request):
        headers = self.headers()
        trace = []
        try:
            async with (
                asyncio.timeout(self.deadline),
                httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client,
            ):
                async with client.stream(
                    "POST",
                    self.base + "/responses",
                    headers={**headers, "Content-Type": "application/json"},
                    content=encoded(request),
                ) as stream:
                    if stream.status_code >= 400:
                        detail = self.clean((await stream.aread()).decode(errors="replace"))
                        raise ModelFailure(
                            f"Provider HTTP {stream.status_code}: {detail[:1500]}",
                            uncertain=stream.status_code >= 500,
                        )
                    if "application/json" in stream.headers.get("content-type", ""):
                        response = json.loads(await stream.aread())
                        if response.get("status", "completed") != "completed":
                            raise ModelFailure(
                                "Provider returned an incomplete response", trace=[response]
                            )
                        return self.clean(response), trace
                    data = []
                    async for line in stream.aiter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            raw = "\n".join(data)
                            data = []
                            if raw == "[DONE]":
                                continue
                            event = self.clean(json.loads(raw))
                            trace.append(event)
                            if event.get("type") == "response.completed":
                                return event["response"], trace
                            if event.get("type") in (
                                "response.failed",
                                "response.incomplete",
                                "error",
                            ):
                                raise ModelFailure(
                                    "Provider ended without a complete response", trace=trace
                                )
        except ModelFailure:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            raise ModelFailure(
                type(exc).__name__ + ": " + self.clean(str(exc)), trace=trace
            ) from exc
        raise ModelFailure("Stream ended without response.completed", trace=trace)


class Models:
    def __init__(self, store, provider):
        self.store = store
        self.provider = provider
        self.inflight = {}
        simulated = bool(getattr(provider, "simulated", False))
        if not simulated and store.events("workspace.simulated"):
            raise ValueError("A scripted workspace cannot be opened with a real model provider")
        if simulated and not store.events("workspace.simulated"):
            store.event("workspace.simulated", "workspace", {"simulated": True}, key="simulation")

    def grant(self, calls):
        if calls is not None and (type(calls) is not int or calls < 1):
            raise ValueError("Grant a positive call allowance or explicitly unlimited")
        return self.store.event("budget.granted", "workspace", {"calls": calls}, key=uid("grant"))

    def recover(self):
        """Call only after acquiring the exclusive runner lock."""
        for row in self.store.events("model.started"):
            ident = row["data"]["id"]
            if not self.store.by_key("model-completed:" + ident) and not self.store.by_key(
                "model-failed:" + ident
            ):
                self.store.event(
                    "model.failed",
                    row["subject"],
                    {
                        "id": ident,
                        "uncertain": True,
                        "error": "Previous runner stopped without a terminal response; purchase retained without replay",
                        "trace": self.store.put([]),
                    },
                    key="model-failed:" + ident,
                )

    def allowance(self):
        grants = self.store.events("budget.granted")
        if any(g["data"]["calls"] is None for g in grants):
            return None
        return sum(g["data"]["calls"] for g in grants) - len(self.store.events("model.started"))

    async def call(self, actor, request, *, call_id, purpose="agent", profile=None, origin=None):
        async with self.inflight.setdefault(call_id, asyncio.Lock()):
            return await self._call(
                actor,
                request,
                call_id=call_id,
                purpose=purpose,
                profile=profile,
                origin=origin,
            )

    async def _call(self, actor, request, *, call_id, purpose, profile, origin):
        body = self.provider.prepare(request) if hasattr(self.provider, "prepare") else request
        started = self.store.by_key("model-started:" + call_id)
        if started and (
            started["subject"] != actor.scope or self.store.json(started["data"]["request"]) != body
        ):
            raise ValueError("Model call identity reused with a different request")
        old = self.store.by_key("model-completed:" + call_id)
        if old:
            return self.store.json(old["data"]["response"])
        if self.store.by_key("model-started:" + call_id):
            raise ModelFailure(
                "This call already has a purchase intent; inspect its receipt before any new attempt"
            )
        estimate = count(encoded(visible(body)).decode())
        if estimate > 1_000_000 - 32768:
            raise ValueError("Request exceeds the configured 1M window; originals retained")
        # No await separates the allowance check from the durable purchase intent.
        # Independent calls can overlap without overspending the single runner's budget.
        allowance = self.allowance()
        if allowance is not None and allowance <= 0:
            raise BudgetExceeded("No model call allowance remains")
        self.store.event(
            "model.started",
            actor.scope,
            {
                "id": call_id,
                "agent": actor.agent,
                "branch": actor.branch,
                "purpose": purpose,
                "profile": profile,
                "origin": origin,
                "input_estimate": estimate,
                "request": self.store.put(body),
            },
            key="model-started:" + call_id,
        )
        try:
            response, trace = await self.provider.generate(body)
            if response.get("status", "completed") != "completed":
                raise ModelFailure("Incomplete response", trace=trace)
            self.store.event(
                "model.completed",
                actor.scope,
                {
                    "id": call_id,
                    "response": self.store.put(response),
                    "trace": self.store.put(trace),
                    "usage": response.get("usage"),
                    "profile": profile,
                    "origin": origin,
                },
                key="model-completed:" + call_id,
            )
            return response
        except (Exception, asyncio.CancelledError) as exc:
            cleaner = getattr(self.provider, "clean", lambda value: value)
            self.store.event(
                "model.failed",
                actor.scope,
                {
                    "id": call_id,
                    "error": cleaner(type(exc).__name__ + ": " + str(exc)),
                    "uncertain": getattr(exc, "uncertain", True),
                    "trace": self.store.put(cleaner(getattr(exc, "trace", []))),
                },
                key="model-failed:" + call_id,
            )
            raise
