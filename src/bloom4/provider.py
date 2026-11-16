"""Responses transports. Credentials stay in memory; no automatic inference retry."""

import asyncio
import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time

import httpx
from websockets.asyncio.client import connect

from .store import encoded


@dataclass
class Generation:
    response: dict
    trace: list = field(default_factory=list)
    continuations: list = field(default_factory=list)
    applied_inputs: list = field(default_factory=list)
    native_items: list | None = None


class ProviderFailure(RuntimeError):
    def __init__(self, message, *, trace=(), uncertain=True):
        super().__init__(message)
        self.trace = list(trace)
        self.uncertain = uncertain


class Responses:
    """HTTP/SSE by default; opt-in WebSocket steering with a durable reservation callback."""

    def __init__(self, *, kind="codex", base_url=None, transport="http", timeout=180,
                 output_tokens=32768, http_transport=None):
        self.kind = kind
        self.base = (base_url or ("https://chatgpt.com/backend-api/codex" if kind == "codex"
                                  else "https://api.openai.com/v1")).rstrip("/")
        self.transport = transport
        self.timeout = timeout
        self.output_tokens = output_tokens
        self.http_transport = http_transport
        self._secrets = []

    def clean(self, text):
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    async def headers(self):
        if self.kind == "openai":
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise ProviderFailure("OPENAI_API_KEY is not configured", uncertain=False)
            self._secrets = [key]
            return {"Authorization": "Bearer " + key}
        path = Path(os.environ.get("BLOOM_CODEX_AUTH_FILE", "~/.codex/auth.json")).expanduser()
        try:
            tokens = json.loads(path.read_text())["tokens"]
            token = tokens["access_token"]
            part = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except (OSError, KeyError, ValueError, IndexError) as exc:
            raise ProviderFailure("A valid existing Codex login is required", uncertain=False) from exc
        if claims.get("exp", 0) < time.time() + 60:
            raise ProviderFailure("Codex login expired; refresh with Codex before retrying", uncertain=False)
        self._secrets = [x for x in tokens.values() if isinstance(x, str)]
        return {"Authorization": "Bearer " + token, "ChatGPT-Account-Id": tokens.get("account_id", ""),
                "originator": "bloom_v4", "User-Agent": "bloom-v4-experimental/0.0.1"}

    def prepare(self, request):
        """Return the exact JSON wire payload, without authentication headers."""
        body = dict(request)
        # Codex's current compatibility endpoint does not accept max_output_tokens.
        # Local budget admission is conservative; it cannot enforce that server cap.
        if self.kind != "codex":
            body["max_output_tokens"] = self.output_tokens
        if self.transport == "websocket":
            body.pop("stream", None)
            body["type"] = "response.create"
        return body

    async def generate(self, request, *, on_text=None, steering=None, reserve_steer=None):
        headers = await self.headers()
        body = self.prepare(request)
        if self.transport == "websocket":
            return await self._websocket(body, headers, on_text, steering, reserve_steer)
        trace, completed = [], None
        try:
            async with asyncio.timeout(self.timeout), httpx.AsyncClient(timeout=self.timeout, transport=self.http_transport) as client:
                async with client.stream("POST", self.base + "/responses", headers={**headers, "Content-Type": "application/json"}, content=encoded(body)) as stream:
                    if stream.status_code >= 400:
                        detail = self.clean((await stream.aread()).decode())
                        raise ProviderFailure(f"Provider HTTP {stream.status_code}: {detail[:1200]}",
                                              uncertain=stream.status_code >= 500)
                    data = []
                    async for line in stream.aiter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            raw = "\n".join(data); data = []
                            if raw == "[DONE]": continue
                            event = json.loads(raw); trace.append(event)
                            if event["type"] == "response.output_text.delta" and on_text:
                                on_text(event["delta"])
                            if event["type"] == "response.completed":
                                completed = event["response"]; break
                            if event["type"] in ("response.failed", "response.incomplete", "error"):
                                raise ProviderFailure("Provider ended without a complete response", trace=trace)
        except ProviderFailure:
            raise
        except asyncio.CancelledError as exc:
            exc.trace = trace
            raise
        except Exception as exc:
            raise ProviderFailure(type(exc).__name__ + ": " + self.clean(str(exc)), trace=trace) from exc
        if completed is None:
            raise ProviderFailure("Stream ended without response.completed", trace=trace)
        return Generation(completed, trace)

    async def _websocket(self, body, headers, on_text, steering, reserve_steer):
        url = self.base.replace("https://", "wss://").replace("http://", "ws://") + "/responses"
        trace, responses, native, submitted, applied, reservations = [], [], [], [], [], []
        current, sent, accepted = None, [], []
        try:
            async with asyncio.timeout(self.timeout):
                async with connect(url, additional_headers=headers) as ws:
                    await ws.send(encoded(body).decode())
                    while True:
                        if current and steering and not steering.empty() and not sent:
                            entry = steering.get_nowait()
                            reservation = reserve_steer(entry, current) if reserve_steer else None
                            if reservation is not None:
                                reservations.append(reservation); submitted.append(entry); sent.append(entry)
                                await ws.send(encoded({"type": "response.steer", "previous_response_id": current,
                                                       "input": entry["data"]["text"]}).decode())
                        # Short receive timeout permits urgent local inputs to be submitted
                        # without cancelling a receive or opening a second inference.
                        try:
                            raw = await asyncio.wait_for(ws.recv(), .1)
                        except TimeoutError:
                            continue
                        event = json.loads(raw); trace.append(event)
                        kind = event.get("type")
                        if kind == "response.created":
                            if current and accepted:
                                for entry in accepted:
                                    native.append({"role": "user", "content": entry["data"]["text"]})
                                    applied.append(entry)
                                accepted.clear(); sent.clear()
                            current = event["response"]["id"]
                        elif kind == "response.steer.accepted":
                            accepted.extend(sent)
                        elif kind == "response.output_item.done":
                            native.append(event["item"])
                        elif kind == "response.output_text.delta" and on_text:
                            on_text(event["delta"])
                        elif kind == "response.incomplete" and event.get("response", {}).get("incomplete_details", {}).get("reason") == "steered":
                            responses.append(event["response"])
                        elif kind == "response.completed":
                            responses.append(event["response"])
                            if not sent and not accepted:
                                return Generation(responses[-1], trace, responses[:-1], applied,
                                                  native if native else responses[-1]["output"])
                        elif kind == "response.steer.pending":
                            # Do not pretend accepted input has been applied. Its local
                            # inbox receipt survives reconnection and tool settlement.
                            return Generation(responses[-1], trace, responses[:-1], applied, native)
                        elif kind in ("response.failed", "response.steer.failed", "error"):
                            raise ProviderFailure("WebSocket response/steering failed", trace=trace)
        except ProviderFailure:
            raise
        except asyncio.CancelledError as exc:
            exc.trace = trace
            raise
        except Exception as exc:
            raise ProviderFailure(type(exc).__name__ + ": " + self.clean(str(exc)), trace=trace) from exc


def public_text(response):
    return "\n\n".join(part["text"] for item in response.get("output", []) if item.get("type") == "message"
                       for part in item.get("content", []) if part.get("type") == "output_text")
