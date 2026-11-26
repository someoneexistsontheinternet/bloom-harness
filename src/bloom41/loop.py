"""The common filesystem inbox → Terra → agent → outbound loop."""

import asyncio
import base64
import contextlib
import json
from pathlib import Path
import time

from .agents import Actor, Agents, PROFILES, resource_directory
from .context import Context
from .delivery import Delivery
from .jobs import Jobs, TERMINAL
from .messages import Messages, addressed
from .models import BudgetExceeded, ModelFailure, Models, final_text
from .store import Store, atomic, digest, encoded, uid


class Loop:
    def __init__(
        self, root, provider, *, heartbeat=5.0, trusted_tools=False, services=None, output=None
    ):
        self.store = Store(root)
        self.agents = Agents(self.store)
        self.agents.bootstrap()
        self.context = Context(self.store)
        self.messages = Messages(self.store)
        self.models = Models(self.store, provider)
        self.jobs = Jobs(self.store, trusted_tools=trusted_tools)
        self.jobs.admit = self.admit_job
        self.delivery = Delivery(self.store, self.jobs)
        self.jobs.handlers.update(self.delivery.handlers())
        self.jobs.handlers.update(
            {
                "model": self.model_api,
                "service": self.service_api,
                "create_agent": self.create_api,
                "fork_self": self.fork_api,
                "memory_write": self.memory_api,
            }
        )
        self.services = services or {}
        self.heartbeat = heartbeat
        self.next_tick = 0
        self.active = {}
        self.stopping = False
        self.output = output

    def scopes(self):
        return [Actor(a["id"]) for a in self.agents.all()] + [
            Actor(r["data"]["owner"], r["subject"]) for r in self.store.events("branch.created")
        ]

    def turn(self, ident):
        row = self.store.latest("turn.state", ident)
        return row["data"] if row else None

    def turns(self, actor=None):
        latest = {}
        for row in self.store.events("turn.state"):
            if actor is None or row["data"]["scope"] == actor.scope:
                latest[row["subject"]] = row["data"]
        return list(latest.values())

    def save_turn(self, turn, **changes):
        value = {**turn, **changes}
        self.store.event("turn.state", turn["id"], value, key=uid("turn-state"))
        return value

    async def model_api(self, actor, job, args):
        role = args.get("profile", "luna")
        if role not in ("luna", "sol"):
            raise PermissionError("Tool model calls are scoped read-only Luna/Sol consultations")
        profile = PROFILES[role]
        request = args["request"]
        parts = (
            request
            if isinstance(request, list)
            else [
                {
                    "type": "input_text",
                    "text": request
                    if isinstance(request, str)
                    else json.dumps(request, ensure_ascii=False),
                }
            ]
        )
        if any(
            not isinstance(p, dict)
            or p.get("type") != "input_text"
            or not isinstance(p.get("text"), str)
            for p in parts
        ):
            raise ValueError("Supply text parts; native image files use sources")
        sources = []
        for name in args.get("sources", []):
            path = self.delivery.path(actor, name)
            data = path.read_bytes()
            media = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }.get(path.suffix.lower())
            if media:
                parts.append({"type": "input_text", "text": "Native image source: " + str(path)})
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": "data:" + media + ";base64," + base64.b64encode(data).decode(),
                        "detail": "high",
                    }
                )
            else:
                parts.append({"type": "input_text", "text": data.decode()})
            sources.append(
                {"path": str(path), "source": self.store.put(data, media or "text/plain")}
            )
        call_id = "reader_" + digest(encoded([job["id"], args["ordinal"]]))[:24]
        turn = self.turn(job["turn"]) if job.get("turn") else None
        body = {
            "model": profile["model"],
            "instructions": (resource_directory("profiles") / "luna.md").read_text(),
            "input": [{"role": "user", "content": parts}],
            "tools": [],
            "store": False,
            "stream": True,
            "reasoning": {"effort": profile["effort"]},
            "include": ["reasoning.encrypted_content"],
        }
        result = await self.models.call(
            actor,
            body,
            call_id=call_id,
            purpose="tool_reader",
            profile=role,
            origin=(turn or {}).get("origin"),
        )
        self.store.event(
            "reader.sources",
            actor.scope,
            {"call": call_id, "sources": sources, "job": job["id"]},
            key="reader-sources:" + call_id,
        )
        return {"text": final_text(result), "call_id": call_id, "sources": sources}

    async def service_api(self, actor, job, args):
        service = self.services.get(args["name"])
        if service is None:
            raise PermissionError("Requested service is not configured")
        key = "service:" + digest(encoded([job["id"], args["ordinal"]]))
        complete = self.store.by_key(key + ":completed")
        if complete:
            return self.store.json(complete["data"]["result"])
        if self.store.by_key(key + ":started"):
            raise RuntimeError("Service request has an unsettled receipt; inspect before repeating")
        self.store.event(
            "service.started",
            actor.scope,
            {"name": args["name"], "arguments": args["arguments"], "job": job["id"]},
            key=key + ":started",
        )
        try:
            result = await service(actor, job, args["arguments"])
        except Exception as exc:
            self.store.event(
                "service.failed",
                actor.scope,
                {"error": self.jobs.clean(str(exc)), "job": job["id"]},
                key=key + ":failed",
            )
            raise
        self.store.event(
            "service.completed",
            actor.scope,
            {"result": self.store.put(result), "job": job["id"]},
            key=key + ":completed",
        )
        return result

    async def create_api(self, actor, job, args):
        result = self.agents.create(args["name"], "sol", args["responsibility"], principal=actor)
        return {
            "agent": result["id"],
            "status": "created",
            "responsibility": result["responsibility"],
        }

    async def fork_api(self, actor, job, args):
        turn = self.turn(job.get("turn"))
        if not turn or turn["scope"] != actor.scope:
            raise PermissionError("Fork requires this actor's exact model decision input")
        if not isinstance(args.get("question"), str) or not args["question"].strip():
            raise ValueError("Give the fork a concrete question")
        branch_id = "branch_" + digest(job["id"].encode())[:24]
        branch = self.agents.fork(
            actor,
            branch_id,
            {"request": turn["request"], "turn": turn["id"], "tools": turn["tools"]},
            args["question"],
        )
        self.messages.deliver(
            actor.scope,
            branch.scope,
            args["question"],
            message_id="assignment_" + digest(job["id"].encode())[:24],
            reply_to=actor.scope,
            metadata={"origin": turn["origin"], "parent_scope": actor.scope},
        )
        return {
            "branch": branch.scope,
            "owner": actor.agent,
            "status": "started",
            "delivery": "Its final message returns to your inbox",
        }

    async def memory_api(self, actor, job, args):
        turn = self.turn(job.get("turn"))
        completed = self.store.by_key("model-completed:" + turn["id"]) if turn else None
        if not turn or turn["scope"] != actor.scope or not completed:
            raise PermissionError("Memory changes need the owning model's completed decision")
        response = self.store.json(completed["data"]["response"])
        approvals = [
            c
            for c in response.get("output", [])
            if c.get("type") == "function_call"
            and c.get("name") == "edit_file"
            and c.get("status", "completed") == "completed"
        ]
        if not any(
            "job_" + digest(encoded([turn["id"], c["call_id"]]))[:24] == job["id"]
            and json.loads(c["arguments"]) == job["arguments"]
            for c in approvals
        ):
            raise PermissionError("Memory approval is not the owner's exact native edit call")
        return self.context.publish_memory(actor, job, args)

    def package(self, ident):
        row = self.store.by_key("message-prepare:" + ident)
        if not row:
            raise ValueError("Missing original message")
        return row["data"]

    def ready(self, actor):
        pending = self.messages.pending(actor)
        if not pending:
            return []
        role = self.agents.get(actor)["role"]
        if role in ("terra", "luna"):

            def destination(package):
                meta = package["metadata"]
                context = (
                    meta
                    if package["kind"] == "context_request"
                    else meta.get("context_request", {})
                ) or {}
                return context.get("context_for"), context.get("request_id")

            selected = destination(pending[0])
            return [p for p in pending if destination(p) == selected]
        contexts = [
            p
            for p in pending
            if p["kind"] == "context" and p["metadata"].get("context_for") == actor.scope
        ]
        if not contexts:
            return []
        chosen = contexts[0]
        originals = [
            self.package(i)
            for i in chosen["metadata"]["original_ids"]
            if not self.store.by_key("message-consumed:" + i)
            and not self.store.by_key("message-filed:" + i)
        ]
        request_id = chosen["metadata"]["request_id"]
        if chosen["metadata"].get("disposition") == "stage" and all(
            p["kind"] == "activity" for p in originals
        ):
            self.context.append(
                actor, [self.context.content(chosen)], key="staged-context:" + chosen["id"]
            )
            self.messages.file([*originals, chosen], "Terra staged routine activity")
            self.store.event(
                "context.finished",
                actor.scope,
                {"request": request_id},
                key="context-finished:" + request_id,
            )
            return []
        # Fresh direct instructions arriving while Terra gathered context need not wait again.
        extras = [
            p
            for p in pending
            if p["id"] not in {o["id"] for o in originals} and p["kind"] == "message"
        ]
        return [*originals, *extras, chosen]

    def request_contexts(self):
        for actor in self.scopes():
            if self.agents.get(actor)["role"] not in ("astra", "sol"):
                continue
            pending = self.messages.pending(actor)
            if not pending or any(
                p["kind"] == "context" and p["metadata"].get("context_for") == actor.scope
                for p in pending
            ):
                continue
            outstanding = [
                r
                for r in self.store.events("context.requested", actor.scope)
                if not self.store.by_key("context-finished:" + r["data"]["request_id"])
            ]
            if outstanding:
                continue
            ids = [p["id"] for p in pending]
            ident = "context_" + digest(encoded([actor.scope, ids]))[:24]
            text = (
                "Gather only context useful to "
                + actor.scope
                + " for its next response. Do not perform its task. The original incoming messages follow. Return a context note addressed to "
                + actor.scope
                + ".\n\n"
                + "\n\n".join(
                    "Message "
                    + p["id"]
                    + " from "
                    + p["sender"]
                    + ":\n"
                    + self.store.read(p["body"]).decode()
                    for p in pending
                )
            )
            attachments = [{**a, "name": a["name"]} for p in pending for a in p["attachments"]]
            self.messages.deliver(
                "system",
                "terra-" + actor.agent,
                text,
                message_id=ident,
                attachments=attachments,
                kind="context_request",
                reply_to=actor.scope,
                metadata={
                    "context_for": actor.scope,
                    "original_ids": ids,
                    "request_id": ident,
                    "origin": pending[0]["metadata"].get("origin") or pending[0]["id"],
                },
            )
            self.store.event(
                "context.requested",
                actor.scope,
                {"request_id": ident, "original_ids": ids},
                key="context-request:" + ident,
            )

    async def notify_jobs(self):
        for job in self.jobs.states():
            if (
                job["status"] not in TERMINAL
                or job.get("parent")
                or self.store.by_key("job-notified:" + job["id"])
            ):
                continue
            value = (
                self.store.json(job["result"])
                if job.get("result")
                else {"status": job["status"], "error": job.get("error")}
            )
            wake = job["wake"] and (not isinstance(value, dict) or value.get("wake", True))
            if (
                job["status"] != "completed"
                or isinstance(value, dict)
                and value.get("status") == "needs_final_message"
            ):
                wake = True
            if wake:
                actor = self.jobs.actor(job)
                item = await self.delivery.attach_value(
                    actor, job, {"name": job["name"] + ".json", "value": value}
                )
                attachments = [
                    self.store.by_key(i)["data"] for i in [item["id"], *item.get("related", [])]
                ]
                if item["needs_extraction"]:
                    curated = self.jobs.submit(
                        actor,
                        "extract",
                        {
                            "path": item["snapshot_path"],
                            "question": "Prepare this tool result for its owner. Preserve requested findings, failures, unfinished work and exact source paths. Original tool: "
                            + job["name"],
                        },
                        job_id="curate_" + digest(job["id"].encode())[:24],
                        turn=job.get("turn"),
                        wake=False,
                    )
                    if curated["status"] not in TERMINAL:
                        continue
                    selected = (
                        self.store.json(curated["result"])
                        if curated.get("result")
                        else {
                            "curation_error": curated.get("error") or curated["status"],
                            "notice": "Full original retained as a reference. Request a narrower extract if needed.",
                        }
                    )
                    prepared = await self.delivery.attach_value(
                        actor, job, {"name": "selected-result.json", "value": selected}
                    )
                    attachments.extend(
                        self.store.by_key(i)["data"]
                        for i in [prepared["id"], *prepared.get("related", [])]
                    )
                source_turn = self.turn(job["turn"]) if job.get("turn") else None
                self.messages.deliver(
                    "tool:" + job["name"],
                    actor.scope,
                    "Tool "
                    + job["name"]
                    + " settled with status "
                    + job["status"]
                    + ". Result is attached. Job: "
                    + job["id"],
                    message_id="result_" + digest(job["id"].encode())[:24],
                    attachments=attachments,
                    kind="tool_result",
                    metadata={
                        "job": job["id"],
                        "origin": (source_turn or {}).get("origin"),
                        "context_request": (source_turn or {}).get("context_request"),
                    },
                )
            self.store.event(
                "job.notified", job["id"], {"woke": bool(wake)}, key="job-notified:" + job["id"]
            )

    def preempted(self, actor, turn):
        seen = set(turn["input_ids"])
        received = self.store.by_key("turn-received:" + turn["id"])
        return [
            row["data"]
            for row in self.store.events(
                "message.prepared", after=received["seq"] if received else 0
            )
            if (p := row["data"])["recipient"] == actor.scope
            and p["id"] not in seen
            and (p["sender"] == "user" or p["metadata"].get("urgent"))
        ]

    def admit_job(self, job):
        turn = self.turn(job.get("turn"))
        if not turn:
            return True
        if self.preempted(self.jobs.actor(job), turn):
            return False
        return True if turn.get("status", "completed") == "completed" else None

    def actor_blocked(self, actor):
        blocked = self.store.latest("actor.blocked", actor.scope)
        resumed = self.store.latest("actor.resumed", actor.scope)
        return blocked if blocked and (not resumed or resumed["seq"] < blocked["seq"]) else None

    def resume(self, actor, reason):
        """Explicit operator recovery: reuse completed output; retain failed purchases."""
        self.agents.get(actor)
        blocked = [t for t in self.turns(actor) if t["status"] == "blocked"]
        if not blocked and not self.actor_blocked(actor):
            raise ValueError("This actor has no blocked work")
        if not reason.strip():
            raise ValueError("Record the reason for resuming after inspecting the receipts")
        row = self.store.event("actor.resumed", actor.scope, {"reason": reason}, key=uid("resume"))
        abandoned = []
        for turn in blocked:
            completed = self.store.by_key("model-completed:" + turn["id"])
            if completed:
                response = self.store.json(completed["data"]["response"])
                self.save_turn(
                    turn,
                    status="responded",
                    response=completed["data"]["response"],
                    final=self.store.put(final_text(response), "text/plain"),
                    error=None,
                )
            elif not self.store.by_key("model-started:" + turn["id"]):
                self.save_turn(turn, status="prepared", error=None, budget=False)
            else:
                self.save_turn(turn, status="abandoned", resolution=row["key"])
                abandoned.append(turn["id"])
        if abandoned:
            self.messages.deliver(
                "system",
                actor.scope,
                "The operator authorized a new decision after inspecting failed requests "
                + ", ".join(abandoned)
                + ". Reason: "
                + reason
                + ". Original inputs and receipts remain. No uncertain effect was replayed.",
                message_id=uid("recovery"),
                kind="message",
            )
        return {"actor": actor.scope, "resumed": len(blocked), "abandoned_requests": abandoned}

    async def advance(self, actor, packages=None, pending_turn=None):
        turn = pending_turn
        try:
            if turn is None:
                await self.context.compact(actor, self.models)
                ids = [p["id"] for p in packages]
                ident = "turn_" + digest(encoded([actor.scope, ids]))[:24]
                existing = self.turn(ident)
                if existing:
                    return
                request, definitions = self.context.request(actor, packages)
                request_context = next(
                    (p["metadata"] for p in packages if p["kind"] == "context_request"), None
                )
                request_context = request_context or next(
                    (
                        p["metadata"].get("context_request")
                        for p in packages
                        if p["metadata"].get("context_request")
                    ),
                    None,
                )
                origin = next(
                    (p["metadata"].get("origin") for p in packages if p["metadata"].get("origin")),
                    packages[0]["id"],
                )
                parent = (
                    (self.store.latest("branch.created", actor.branch) or {})
                    .get("data", {})
                    .get("parent_scope")
                    if actor.branch
                    else None
                )
                default_target = parent or ("user" if actor.agent == "astra" else "astra")
                turn = {
                    "id": ident,
                    "agent": actor.agent,
                    "branch": actor.branch,
                    "scope": actor.scope,
                    "status": "prepared",
                    "input_ids": ids,
                    "request": self.store.put(request),
                    "tools": definitions,
                    "origin": origin,
                    "reply_to": default_target,
                    "context_request": request_context,
                }
                self.store.event(
                    "turn.received",
                    actor.scope,
                    {"turn": ident, "inputs": ids},
                    key="turn-received:" + ident,
                )
                self.save_turn(turn)
            if turn["status"] == "prepared":
                response = await self.models.call(
                    actor,
                    self.store.json(turn["request"]),
                    call_id=turn["id"],
                    purpose="agent",
                    profile=self.agents.get(actor)["role"],
                    origin=turn["origin"],
                )
                text = final_text(response)
                turn = self.save_turn(
                    turn,
                    status="responded",
                    response=self.store.put(response),
                    final=self.store.put(text, "text/plain"),
                )
            if turn["status"] == "responded":
                self.dispatch(actor, turn)
        except (Exception, asyncio.CancelledError) as exc:
            if turn:
                self.save_turn(
                    turn,
                    status="blocked",
                    error=self.jobs.clean(type(exc).__name__ + ": " + str(exc)),
                    budget=isinstance(exc, BudgetExceeded),
                )
            else:
                self.store.event(
                    "actor.blocked",
                    actor.scope,
                    {"error": self.jobs.clean(type(exc).__name__ + ": " + str(exc))},
                    key=uid("actor-blocked"),
                )

    def dispatch(self, actor, turn):
        response = self.store.json(turn["response"])
        text = self.store.read(turn["final"]).decode()
        calls = [i for i in response.get("output", []) if i.get("type") == "function_call"]
        if not calls and not text.strip():
            raise ValueError("Model returned neither a final message nor complete tool calls")
        if len({c["call_id"] for c in calls}) != len(calls):
            raise ValueError("Duplicate native tool call IDs")
        definitions = {d["name"]: d for d in turn["tools"]}
        outbound = self.agents.path(actor) / "outbound" / turn["id"]
        atomic(outbound / "message.txt", text.encode())
        target, header = addressed(text) if text else (None, {})
        info = turn.get("context_request")
        message_metadata = {"urgent": bool(header.get("urgent", False))}
        if info:
            message_metadata.update({**info, "disposition": header.get("disposition", "deliver")})
        turn = self.save_turn(
            turn, message_metadata=message_metadata, message_kind="context" if info else "message"
        )
        stale = bool(self.preempted(actor, turn))
        outputs = []
        handles_message = False
        explicit_recipients = set()
        for call in calls:
            atomic(
                outbound / "calls" / ("call-" + digest(call["call_id"].encode())[:24] + ".json"),
                encoded(call),
            )
            try:
                if stale:
                    raise ValueError("New owner input superseded this unstarted action")
                if call.get("status", "completed") != "completed":
                    raise ValueError("Incomplete tool arguments are not executable")
                spec = definitions[call["name"]]
                args = json.loads(call["arguments"])
                job = self.jobs.submit(
                    actor,
                    call["name"],
                    args,
                    job_id="job_" + digest(encoded([turn["id"], call["call_id"]]))[:24],
                    turn=turn["id"],
                    wake=spec["notify_on_success"],
                    frozen=spec,
                )
                value = {
                    "status": "accepted",
                    "job": job["id"],
                    "delivery": "Result enters your inbox; no polling turn is needed",
                }
                handles_message |= spec["handles_message"]
                if call["name"] == "send_message":
                    explicit_recipients.add(args["recipient"])
            except Exception as exc:
                value = {"status": "not_executed", "error": str(exc)}
                self.messages.deliver(
                    "system",
                    actor.scope,
                    "Tool request could not execute: " + json.dumps(value),
                    message_id="rejected_" + digest(encoded([turn["id"], call["call_id"]]))[:24],
                    kind="tool_result",
                    metadata={"origin": turn["origin"]},
                )
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": json.dumps(value),
                }
            )
        if text.strip() and not stale:
            default_target = info["context_for"] if info else target or turn["reply_to"]
            if (info and default_target not in explicit_recipients) or not handles_message:
                spec = definitions["send_message"]
                self.jobs.submit(
                    actor,
                    "send_message",
                    {"recipient": default_target},
                    job_id="send_" + digest(encoded([turn["id"], default_target]))[:24],
                    turn=turn["id"],
                    wake=False,
                    frozen=spec,
                )
        packages = [self.package(i) for i in turn["input_ids"]]
        self.context.append(
            actor,
            [self.context.content(p) for p in packages] + response.get("output", []) + outputs,
            key="turn-history:" + turn["id"],
        )
        self.messages.consume(packages, turn["id"])
        for p in packages:
            if p["kind"] == "context" and p["metadata"].get("request_id"):
                request_id = p["metadata"]["request_id"]
                self.store.event(
                    "context.finished",
                    actor.scope,
                    {"request": request_id},
                    key="context-finished:" + request_id,
                )
        self.save_turn(turn, status="completed", superseded=stale)
        if self.agents.get(actor)["role"] == "sol" and calls and not text.strip() and not stale:
            ident = "activity_" + digest(turn["id"].encode())[:24]
            info = {
                "context_for": actor.scope,
                "original_ids": [],
                "request_id": ident,
                "origin": turn["origin"],
                "activity_only": True,
            }
            description = (
                "Monitor the latest turn of "
                + actor.scope
                + ". Stage ordinary activity; deliver private advice for a consequential gap, or send Astra a concrete decision requiring attention. Do not do the task. Operations: "
                + ", ".join(c["name"] for c in calls)
                + ". Exact requests are in "
                + str(outbound / "calls")
                + ". The owner's original instructions remain in the agent history."
            )
            self.messages.deliver(
                "system",
                "terra-" + actor.agent,
                description,
                message_id=ident,
                kind="activity",
                reply_to=actor.scope,
                metadata={"context_request": info, "origin": turn["origin"]},
            )

    async def run(self, *, watch=False, seconds=None):
        await self.jobs.start()
        self.models.recover()
        self.messages.recover()
        began = time.monotonic()
        try:
            while not self.stopping and (seconds is None or time.monotonic() - began < seconds):
                self.store.sync()
                for scope, task in list(self.active.items()):
                    if task.done():
                        await task
                        del self.active[scope]
                await self.notify_jobs()
                if time.monotonic() >= self.next_tick:
                    self.request_contexts()
                    self.next_tick = time.monotonic() + self.heartbeat
                for actor in self.scopes():
                    if len(self.active) >= 4 or actor.scope in self.active:
                        continue
                    if self.actor_blocked(actor):
                        continue
                    turns = self.turns(actor)
                    if any(t["status"] == "blocked" for t in turns):
                        continue
                    for blocked_turn in turns:
                        if (
                            blocked_turn["status"] == "blocked"
                            and blocked_turn.get("budget")
                            and not self.store.by_key("model-started:" + blocked_turn["id"])
                            and self.models.allowance() != 0
                        ):
                            self.save_turn(
                                blocked_turn, status="prepared", error=None, budget=False
                            )
                    turns = self.turns(actor)
                    unsettled = next(
                        (t for t in turns if t["status"] in ("prepared", "responded")), None
                    )
                    if unsettled:
                        self.active[actor.scope] = asyncio.create_task(
                            self.advance(actor, pending_turn=unsettled)
                        )
                        continue
                    packages = self.ready(actor)
                    if packages and not self.turn(
                        "turn_" + digest(encoded([actor.scope, [p["id"] for p in packages]]))[:24]
                    ):
                        self.active[actor.scope] = asyncio.create_task(
                            self.advance(actor, packages)
                        )
                await self.jobs.pump()
                if self.output:
                    for package in self.messages.pending("user"):
                        key = "cli-output:" + package["id"]
                        if not self.store.by_key(key):
                            self.output(self.store.read(package["body"]).decode())
                            self.store.event(
                                "channel.delivered", package["id"], {"channel": "cli"}, key=key
                            )
                runnable_jobs = any(
                    j["status"] in ("running", "launching")
                    or j["status"] == "prepared"
                    and self.admit_job(j) is not None
                    for j in self.jobs.states()
                )
                pending_inboxes = any(self.messages.pending(a) for a in self.scopes())
                blocked = any(t["status"] == "blocked" for t in self.turns()) or any(
                    self.actor_blocked(a) for a in self.scopes()
                )
                if (
                    not watch
                    and not self.active
                    and not runnable_jobs
                    and (not pending_inboxes or blocked)
                ):
                    break
                await asyncio.sleep(
                    min(max(self.heartbeat, 0.02), 0.25 if self.active or runnable_jobs else 1.0)
                )
        finally:
            for task in self.active.values():
                task.cancel()
            await asyncio.gather(*self.active.values(), return_exceptions=True)
            await self.jobs.close()
        return self.status()

    def status(self):
        return {
            "agents": self.agents.all(),
            "turns": [
                {k: t.get(k) for k in ("id", "scope", "status", "error")} for t in self.turns()
            ],
            "jobs": [
                {k: j.get(k) for k in ("id", "agent", "branch", "name", "status", "error")}
                for j in self.jobs.states()
            ],
            "remaining_calls": self.models.allowance(),
            "blocked_actors": [r for a in self.scopes() if (r := self.actor_blocked(a))],
        }
