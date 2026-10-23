from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .privacy import clean, register
from .diagnostics import describe, label
from .store import now, uid


class ProviderError(RuntimeError):
    pass


class RetryableProviderError(ProviderError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def http_error(status, detail, retry_after=None):
    message = f"Provider HTTP {status}: " + clean(detail[:1500])
    try:
        delay = max(0, float(retry_after or 0))
    except ValueError:
        # Do not retry sooner than an unparsed server-directed wait.
        return ProviderError(message)
    if (status in (408, 429) or 500 <= status <= 599) and delay <= 60:
        return RetryableProviderError(message, delay)
    return ProviderError(message)


class Models:
    def __init__(self, store, transport=None):
        self.store = store
        self.config = store.config
        self.transport = transport

    async def auth_headers(self, provider):
        if provider["auth"] == "none":
            return {}
        if provider["auth"] == "env":
            key = os.environ.get(provider["key_env"])
            if not key:
                raise ProviderError(
                    "Missing credential environment variable: " + provider["key_env"]
                )
            register(key)
            return {"Authorization": "Bearer " + key}
        path = Path(provider["auth_file"]).expanduser()
        data = json.loads(path.read_text())
        tokens = data.get("tokens", {})
        for value in tokens.values():
            register(value)
        token = tokens.get("access_token", "")
        try:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (IndexError, ValueError) as exc:
            raise ProviderError("Codex login is missing or invalid; use codex login") from exc
        if claims.get("exp", 0) < time.time() + 60:
            # Codex alone owns refresh; never race writes to its shared auth.json.
            await self.refresh_codex()
            data = json.loads(path.read_text())
            tokens = data.get("tokens", {})
            token = tokens.get("access_token", "")
            for value in tokens.values():
                register(value)
        return {
            "Authorization": "Bearer " + token,
            "ChatGPT-Account-Id": tokens.get("account_id", ""),
            "originator": "bloom_v3",
            "User-Agent": "bloom/0.1.0",
        }

    async def refresh_codex(self):
        proc = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def request(number, method, params):
            proc.stdin.write(
                (json.dumps({"id": number, "method": method, "params": params}) + "\n").encode()
            )
            await proc.stdin.drain()
            while line := await proc.stdout.readline():
                value = json.loads(line)
                if value.get("id") == number:
                    if "error" in value:
                        raise ProviderError("Codex authentication refresh failed")
                    return value.get("result")
            raise ProviderError("Codex authentication helper exited")

        try:
            async with asyncio.timeout(45):
                await request(
                    1, "initialize", {"clientInfo": {"name": "bloom_v3", "version": "0.1.0"}}
                )
                proc.stdin.write(b'{"method":"initialized","params":{}}\n')
                await proc.stdin.drain()
                await request(2, "account/read", {"refreshToken": True})
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()

    def prepare(self, profile, instructions, items, tools):
        model = profile.get("model")
        if not isinstance(model, str) or not model.strip() or model.startswith("configure-"):
            raise ProviderError(
                "Choose an accessible model for each role in config/local.toml before starting inference"
            )
        provider = self.config.data["providers"][profile["provider"]]
        if provider["kind"] == "browser":
            return {
                "instructions": instructions,
                "input": items,
                "tools": tools,
                "model": profile["model"],
            }
        if provider["kind"] not in ("responses", "chat"):
            raise ProviderError("Unsupported provider kind: " + provider["kind"])
        if provider["kind"] == "chat":
            from .images import chat_messages

            messages = chat_messages(instructions, items)
            return {
                "model": profile["model"],
                "messages": messages,
                "tools": [
                    {
                        "type": "function",
                        "function": {k: v for k, v in t.items() if k not in ("type",)},
                    }
                    for t in tools
                    if t["type"] == "function"
                ],
                **profile.get("request_options", {}),
            }
        body = {
            "model": profile["model"],
            "instructions": instructions,
            "input": items,
            "tools": tools,
            "store": False,
            "stream": provider.get("stream", True),
            "parallel_tool_calls": True,
            "reasoning": {"effort": profile.get("effort", "medium"), "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
        }
        if provider.get("max_output_supported", True):
            body["max_output_tokens"] = profile["output_reserve"]
        return body

    async def infer(
        self,
        agent,
        task,
        instructions,
        items,
        tools,
        purpose="turn",
        call_id=None,
        profile_override=None,
        item_ids=None,
        retry_limit=None,
    ):
        from .turns import current as current_turn

        owner_turn = current_turn(self.store, task)
        if purpose == "turn" and item_ids:
            # Compaction can await while another owner input arrives. Attribute
            # this decision to the latest owner message in its actual snapshot.
            owner = self.store.one(
                "SELECT id FROM owner_turns WHERE task_id=? AND source_item<=? AND replied_at IS NULL ORDER BY source_item DESC LIMIT 1",
                (task, max(item_ids)),
            )
            if owner:
                owner_turn = owner["id"]
        role = self.store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"]
        profile = {**self.config.profile(role), **(profile_override or {})}
        provider = self.config.data["providers"][profile["provider"]]
        # Local functions cannot dispatch until a complete response is returned.
        # Browser/hosted mutation tools do not have that same retry boundary.
        safe_tools = all(
            t.get("type") in ("function", "web_search", "web_search_preview") for t in tools
        )
        retries = (
            provider.get("max_retries", 2)
            if provider["kind"] in ("responses", "chat") and safe_tools
            else 0
        )
        if retry_limit is not None:
            retries = min(retries, retry_limit)
        current = call_id or uid("call")

        def steering_epoch():
            return (
                self.store.one(
                    "SELECT max(id) AS n FROM events WHERE task_id=? AND kind='task.steered'",
                    (task,),
                )["n"]
                or 0
            )

        epoch = steering_epoch()

        def check_control():
            state = self.store.one("SELECT status FROM tasks WHERE id=?", (task,)) if task else None
            if (
                state and state["status"] in ("paused", "cancelled", "blocked")
            ) or steering_epoch() != epoch:
                raise InterruptedError(
                    "Owner control interrupted model retry before another request"
                )

        for attempt in range(retries + 1):
            if attempt:
                check_control()
            try:
                return await self._infer_once(
                    agent,
                    task,
                    instructions,
                    items,
                    tools,
                    purpose,
                    current,
                    profile_override,
                    item_ids,
                    owner_turn,
                )
            except (httpx.TransportError, RetryableProviderError) as exc:
                record = self.store.one("SELECT * FROM model_calls WHERE id=?", (current,))
                if (
                    attempt >= retries
                    or not record
                    or record["status"] not in ("failed", "uncertain")
                ):
                    raise
                check_control()
                delay = min(
                    60,
                    max(
                        provider.get("retry_delay_seconds", 1) * 2**attempt,
                        getattr(exc, "retry_after", 0),
                    ),
                )
                following = uid("call")
                with self.store.transaction():
                    self.store.db.execute(
                        "UPDATE model_calls SET status='failed',error=coalesce(error,'') || '; bounded retry scheduled; prior usage remains unknown if unreported' WHERE id=?",
                        (current,),
                    )
                    self.store.event(
                        "model.retry_scheduled",
                        {
                            "previous_call": current,
                            "next_call": following,
                            "attempt": attempt + 2,
                            "max_attempts": retries + 1,
                            "delay_seconds": delay,
                            "error": label(exc)[:2000],
                            "local_tools_dispatched": False,
                        },
                        task,
                        agent,
                    )
                await asyncio.sleep(delay)
                current = following

    async def _infer_once(
        self,
        agent,
        task,
        instructions,
        items,
        tools,
        purpose,
        call_id,
        profile_override,
        item_ids,
        owner_turn=None,
    ):
        role = self.store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"]
        profile = {**self.config.profile(role), **(profile_override or {})}
        provider = self.config.data["providers"][profile["provider"]]
        endpoint_parts = urlparse(provider["base_url"])
        if provider["auth"] == "codex" and (
            endpoint_parts.scheme,
            endpoint_parts.hostname,
            endpoint_parts.path.rstrip("/"),
        ) != ("https", "chatgpt.com", "/backend-api/codex"):
            raise ProviderError("Codex credentials may only be sent to the official Codex endpoint")
        if endpoint_parts.scheme != "https" and endpoint_parts.hostname not in (
            "localhost",
            "127.0.0.1",
        ):
            raise ProviderError("Provider credentials require HTTPS outside loopback")
        body = clean(self.prepare(profile, instructions, items, tools))
        call_id = call_id or uid("call")
        old = self.store.one("SELECT * FROM model_calls WHERE id=?", (call_id,))
        digest = self.store.put(body)
        if old:
            if old["request_hash"] != digest:
                raise ProviderError("Inference identity was reused with different input")
            if old["status"] == "completed":
                return call_id, self.store.get(old["response_hash"])
            if old["status"] != "prepared":
                raise ProviderError(
                    "An interrupted inference needs explicit reconciliation; it will not be repurchased automatically"
                )
        else:
            self.store.db.execute(
                "INSERT INTO model_calls(id,task_id,agent_id,purpose,model_requested,effort,provider,status,request_hash,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    call_id,
                    task,
                    agent,
                    purpose,
                    profile["model"],
                    profile.get("effort"),
                    profile["provider"],
                    "prepared",
                    digest,
                    now(),
                ),
            )
            if item_ids is not None:
                if len(item_ids) != len(items):
                    raise ValueError("Context item membership does not match the actual input")
                self.store.db.executemany(
                    "INSERT INTO model_call_items VALUES(?,?,?)",
                    [(call_id, item, i) for i, item in enumerate(item_ids)],
                )
        from .turns import attribute_call

        attribute_call(self.store, call_id, owner_turn)
        self.store.db.execute("UPDATE model_calls SET status='running' WHERE id=?", (call_id,))
        endpoint = provider["base_url"].rstrip("/") + (
            "/chat/completions" if provider["kind"] == "chat" else "/responses"
        )
        stream_events = []
        phase = "authentication"
        started = time.monotonic()
        last_event_at = None
        first_event_at = None
        from .live_preview import PublicStream

        public = PublicStream(self.store, agent, task, call_id, purpose)
        try:
            headers = await self.auth_headers(provider)
            if provider["auth"] == "codex":
                headers["session_id"] = "bloom-v3-" + agent
            phase = "connect"
            async with httpx.AsyncClient(
                timeout=provider.get("timeout_seconds", 1200), transport=self.transport
            ) as client:
                if provider["kind"] == "browser":
                    from .browser import Browser

                    response = await Browser(self.store, provider, self.transport).complete(
                        agent, call_id, profile, body
                    )
                elif body.get("stream"):
                    response = None
                    completed_items = {}
                    data_lines = []

                    def consume():
                        nonlocal response, last_event_at, first_event_at, phase
                        if not data_lines:
                            return
                        raw = "\n".join(data_lines)
                        data_lines.clear()
                        if raw.strip() == "[DONE]":
                            return
                        event = json.loads(raw)
                        last_event_at = time.monotonic()
                        first_event_at = first_event_at or last_event_at
                        phase = "streaming"
                        stream_events.append(clean(event))
                        public.event(event)
                        if event.get("type") == "response.created":
                            self.store.db.execute(
                                "UPDATE model_calls SET response_id=? WHERE id=?",
                                ((event.get("response") or {}).get("id"), call_id),
                            )
                        if event.get("type") == "response.output_item.done":
                            completed_items[event.get("output_index", len(completed_items))] = (
                                event["item"]
                            )
                        elif event.get("type") == "response.completed":
                            response = event["response"]
                        elif event.get("type") in (
                            "response.failed",
                            "response.incomplete",
                            "error",
                        ):
                            error = (
                                event.get("error")
                                or (event.get("response") or {}).get("error")
                                or event
                            )
                            transient = isinstance(error, dict) and error.get(
                                "code", error.get("type")
                            ) in ("server_error", "rate_limit_exceeded", "overloaded")
                            failure = (
                                RetryableProviderError
                                if event.get("type") != "response.incomplete" and transient
                                else ProviderError
                            )
                            raise failure(
                                "Provider did not complete: " + clean(json.dumps(event)[:1500])
                            )

                    try:
                        async with client.stream(
                            "POST", endpoint, headers=headers, json=body
                        ) as stream:
                            phase = "awaiting_first_event"
                            if stream.status_code >= 400:
                                raise http_error(
                                    stream.status_code,
                                    (await stream.aread()).decode(),
                                    stream.headers.get("retry-after"),
                                )
                            async for line in stream.aiter_lines():
                                if line.startswith("data:"):
                                    data_lines.append(line[5:].lstrip())
                                elif line == "":
                                    consume()
                                    if response is not None:
                                        break  # Completion is authoritative; do not wait for transport EOF.
                            consume()
                    except httpx.TransportError as exc:
                        if response is None:
                            raise
                        self.store.event(
                            "model.transport_closed_after_completion",
                            {"call": call_id, "error": clean(str(exc))[:2000]},
                            task,
                            agent,
                        )
                    if response is None:
                        raise RetryableProviderError(
                            "Provider stream ended without a completed response"
                        )
                    if not response.get("output") and completed_items:
                        response = {
                            **response,
                            "output": [completed_items[k] for k in sorted(completed_items)],
                        }
                else:
                    result = await client.post(endpoint, headers=headers, json=body)
                    phase = "response_decode"
                    if result.status_code >= 400:
                        raise http_error(
                            result.status_code, result.text, result.headers.get("retry-after")
                        )
                    response = result.json()
            if provider["kind"] == "chat":
                message = response["choices"][0]["message"]
                if response["choices"][0].get("finish_reason") not in ("stop", "tool_calls"):
                    raise ProviderError("Chat completion did not finish")
                output = []
                if message.get("content"):
                    output.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "phase": "commentary" if message.get("tool_calls") else "final_answer",
                            "content": [{"type": "output_text", "text": message["content"]}],
                        }
                    )
                output.extend(
                    {"type": "function_call", "call_id": x["id"], **x["function"]}
                    for x in message.get("tool_calls", [])
                )
                response = {**response, "output": output, "status": "completed"}
            if response.get("status", "completed") != "completed":
                raise ProviderError("Incomplete response cannot execute tools")
            response = clean(response)
            usage = response.get("usage") or {}
            input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
            output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
            cached = (
                usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
            ).get("cached_tokens")
            reasoning = (
                usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
            ).get("reasoning_tokens")
            self.store.db.execute(
                "UPDATE model_calls SET status=?,response_hash=?,response_id=?,model_reported=?,input_tokens=?,cached_tokens=?,output_tokens=?,reasoning_tokens=?,completed_at=? WHERE id=?",
                (
                    "completed",
                    self.store.put(response),
                    response.get("id"),
                    response.get("model"),
                    input_tokens,
                    cached,
                    output_tokens,
                    reasoning,
                    now(),
                    call_id,
                ),
            )
            if not any(
                item.get("type") in ("message", "function_call")
                for item in response.get("output", [])
            ):
                raise ProviderError(
                    "Completed response contained no message or callable tool; stopped instead of buying another identical turn"
                )
            public.write("completed")
            return call_id, response
        except BaseException as exc:
            # Cancellation cannot have run a model-requested tool: only a durable
            # completed response can reach dispatch. Usage remains unknown.
            status = (
                "uncertain"
                if isinstance(exc, (httpx.TransportError, asyncio.CancelledError))
                and phase != "authentication"
                else "failed"
            )
            detail = describe(
                exc,
                phase=phase,
                elapsed_seconds=round(time.monotonic() - started, 3),
                first_event_seconds=round(first_event_at - started, 3) if first_event_at else None,
                seconds_since_last_event=round(time.monotonic() - last_event_at, 3)
                if last_event_at
                else None,
                stream_events=len(stream_events),
                last_event=stream_events[-1].get("type") if stream_events else None,
                local_tools_dispatched=False,
                usage_reported=False,
            )
            self.store.db.execute(
                "INSERT OR REPLACE INTO model_call_diagnostics VALUES(?,?,?)",
                (call_id, self.store.put(detail), now()),
            )
            self.store.db.execute(
                "UPDATE model_calls SET status=?,error=?,completed_at=? WHERE id=?",
                (status, label(exc)[:2000], now(), call_id),
            )
            public.write(status)
            raise
        finally:
            if stream_events:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO model_call_streams VALUES(?,?)",
                    (call_id, self.store.put(stream_events)),
                )
