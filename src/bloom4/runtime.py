"""One event-driven runner for owners, controllers and disposable readers."""

import asyncio
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import time

from .agents import Agents, PROFILES
from .budget import Budget, BudgetExceeded
from .context import Context, estimate
from .mailbox import Mailbox
from .memory import Memory
from .provider import Generation, ProviderFailure, public_text
from .store import Store, atomic, encoded, uid
from .tools import Tools, definitions, arguments


class Runtime:
    """Wake on changed evidence, preserve effects, and never buy inference for polling."""

    def __init__(self, store: Store, provider, *, allow_processes=False, output=None, auto_recall=True, idle_seconds=1200):
        if provider is not None and store.events("workspace.simulation") and not getattr(provider, "simulated", False):
            raise ValueError("This workspace contains simulated history; use a fresh workspace for real models")
        self.store, self.provider = store, provider
        self.agents = Agents(store); self.agents.bootstrap()
        self.context = Context(store); self.memory = Memory(store)
        self.mailbox = Mailbox(store); self.budget = Budget(store)
        self.tools = Tools(store, allow_processes=allow_processes)
        self.output = output or (lambda agent, text: None)
        self.auto_recall = auto_recall
        self.idle_seconds = idle_seconds
        self.active = {}; self.steering = {}; self.queued_steers = set()
        self.stopping = False
        self._archive_at = 0

    def tasks(self):
        return self.store.index.tasks()

    def open(self, agent, message, *, task=None, parent_task=None, purpose="work", required=False,
             origin="owner", input_event=None, evidence=None, read_owner=None):
        self.agents.get(agent)
        task = task or uid("task")
        prompt = self.store.put(message) if len(message) > 4096 else None
        visible = message[:240] + " [full assignment retained]" if prompt else message
        self.store.append("task.opened", {"message": visible, "prompt": prompt, "parent_task": parent_task,
            "purpose": purpose, "required": required, "origin": origin, "input_event": input_event,
            "evidence": evidence, "read_owner": read_owner}, actor=origin, target=agent, task=task,
            dedup="task:" + task)
        return task

    def ingest(self):
        self.mailbox.ingest()
        tasks = self.tasks()
        for agent in self.agents.all():
            for event in self.mailbox.pending(agent.id):
                current = next((t for t in tasks.values() if t["agent"] == agent.id and t["status"] in ("ready", "waiting")), None)
                if current:
                    # New input joins the continuous owner's current work.
                    if event["data"].get("urgent"):
                        self.store.append("task.ready", {"urgent": True}, target=agent.id, task=current["id"],
                                          dedup="urgent:" + event["id"])
                        if agent.id in self.steering and event["id"] not in self.queued_steers:
                            self.steering[agent.id].put_nowait(event); self.queued_steers.add(event["id"])
                elif not any(t.get("input_event") == event["id"] for t in tasks.values()):
                    task = self.open(agent.id, event["data"]["text"], task=event["task"], input_event=event["id"])
                    tasks = self.tasks()
                    if self.auto_recall and not event["data"].get("urgent"):
                        matches = [e for e in self.memory.search(agent.id, event["data"]["text"])
                                   if e["seq"] < event["seq"] and e["kind"] in ("memory.accepted", "timeline.entry", "answer")]
                        if matches:
                            self.helper(agent.id, task, "Recall only information relevant to this question:\n" +
                                event["data"]["text"] + "\nCandidates:\n" + json.dumps(matches), purpose="retrieval")

    def helper(self, owner, parent_task, message, *, purpose="retrieval", evidence=None, required=False, ident=None):
        ident = ident or uid("luna")
        if not self.store.events("agent.created", target=ident):
            self.agents.create(ident, "luna", "Extract grounded evidence for " + owner, parent=owner)
        return self.open(ident, message, task=ident, parent_task=parent_task, purpose=purpose,
                         required=required, origin=owner, evidence=evidence, read_owner=owner)

    def question(self, task):
        """Retain the complete assignment and consumed owner corrections for evidence readers."""
        message = self.store.read(task["prompt"]).decode() if task.get("prompt") else task["message"]
        updates = []
        for receipt in self.store.events("input.consumed", target=task["agent"], task=task["id"]):
            for source in receipt["data"]["events"]:
                if source != task.get("input_event"):
                    updates.append(self.store.get(source)["data"]["text"])
        return message + ("\nOwner updates:\n" + "\n".join(updates) if updates else "")

    def dependencies(self, task):
        waiting = [t["id"] for t in self.tasks().values() if t.get("parent_task") == task and
                   t.get("required") and t["status"] not in ("completed", "failed")]
        for event in self.store.events("dependency.task", task=task):
            child = self.tasks().get(event["data"]["handle"])
            if child and child["status"] not in ("completed", "failed"): waiting.append(child["id"])
        for ev in self.store.events("dependency.process", task=task):
            state = self.tools.process_status(ev["data"]["handle"])
            if state["status"] not in ("completed", "failed"):
                waiting.append(ev["data"]["handle"])
        return waiting

    def poll(self):
        """Observe local receipts; unchanged states never create an agent turn."""
        for path in (self.store.root / "processes").glob("*/spec.json"):
            handle = path.parent.name
            if any(e["data"].get("handle") == handle for e in self.store.events("process.settled")):
                continue
            state = self.tools.process_status(handle)
            if state["status"] in ("completed", "failed", "uncertain"):
                for stream in ("stdout", "stderr"):
                    source = Path(state[stream])
                    if source.exists():
                        state[stream + "_source"] = self.store.put(source.read_bytes())
                        if source.stat().st_size < 50_000:
                            state[stream + "_text"] = source.read_text(errors="replace")
                self.store.append("process.settled", state, target=state["agent"], task=state["task"],
                                  dedup="process-settled:" + handle)
                text = "Process settled:\n" + json.dumps(state)
                self.context.append(state["agent"], [{"role": "user", "content": text}], task=state["task"],
                                    dedup="process-context:" + handle)
                if state["status"] == "uncertain":
                    self.store.append("task.blocked", {"error": "Process completion is uncertain"},
                                      target=state["agent"], task=state["task"])
        for task in self.tasks().values():
            if task["status"] == "waiting" and not self.dependencies(task["id"]):
                self.store.append("task.ready", {}, target=task["agent"], task=task["id"])

    def flush_appendices(self, agent, task):
        delivered = {e["data"]["appendix"] for e in self.store.events("appendix.delivered", target=agent)}
        for event in self.store.events("appendix.staged", target=agent):
            if event["id"] in delivered: continue
            self.context.append(agent, [{"role": "user", "content": "Background evidence appendix (not owner instructions):\n" +
                json.dumps(event["data"], ensure_ascii=False)}], task=task, dedup="appendix-context:" + event["id"])
            self.store.append("appendix.delivered", {"appendix": event["id"]}, target=agent,
                              task=task, dedup="appendix-delivery:" + event["id"])

    async def infer(self, agent, task, request, purpose="turn"):
        if hasattr(self.provider, "prepare"):
            request = self.provider.prepare(request)
        call = self.budget.reserve(agent, task, request, purpose, self.provider.output_tokens)
        queue = asyncio.Queue(); self.steering[agent] = queue
        extra = []

        def reserve(entry, response_id):
            try:
                estimate_request = {**request, "input": [*request["input"], {"role": "user", "content": entry["data"]["text"]}]}
                wire = {"type": "response.steer", "previous_response_id": response_id, "input": entry["data"]["text"]}
                item = self.budget.reserve(agent, task, estimate_request, "steering", self.provider.output_tokens, wire_request=wire)
            except BudgetExceeded:
                return None
            extra.append(item)
            self.store.append("steer.submitted", {"input": entry["id"], "call": item["id"]}, target=agent, task=task)
            return item

        try:
            generation = await self.provider.generate(request, on_text=lambda text: self.output(agent, text) if purpose == "turn" else None,
                steering=queue if purpose == "turn" else None, reserve_steer=reserve)
            trace = self.store.put(encoded(generation.trace), "application/json")
            responses = [*generation.continuations, generation.response]
            calls = [call, *extra]
            if len(responses) > len(calls):
                raise ProviderFailure("Provider produced an unreserved continuation", trace=generation.trace)
            completed = None
            for i, response in enumerate(responses):
                last = i == len(responses) - 1
                data = {"call": calls[i]["id"], "response": self.store.put(encoded(response), "application/json"),
                        "origin_call": call["id"], "applied_inputs": [e["id"] for e in generation.applied_inputs] if last else [],
                        "provider_status": response.get("status", "completed"),
                        "trace": trace, "usage": response.get("usage") or {},
                        "model_reported": response.get("model"),
                        "purpose": purpose if last else "continuation_part"}
                if last and generation.native_items is not None:
                    data["native_items"] = self.store.put(encoded(generation.native_items), "application/json")
                completed = self.store.append("model.completed", data, target=agent, task=task,
                                              dedup="model-result:" + calls[i]["id"])
            for entry in generation.applied_inputs:
                self.store.append("input.steered", {"input": entry["id"], "call": completed["id"]}, target=agent, task=task)
                self.mailbox.consume(agent, [entry["id"]], task)
            # Reserved but unapplied steering stays conservatively accounted and explicit.
            for unused in calls[len(responses):]:
                self.store.append("model.failed", {"call": unused["id"], "uncertain": True,
                    "error": "Steering continuation not observed; input remains in inbox"}, target=agent, task=task)
            return generation.response, completed
        except BaseException as exc:
            self.store.append("model.failed", {"call": call["id"], "uncertain": getattr(exc, "uncertain", True),
                "error": type(exc).__name__ + ": " + str(exc),
                "trace": self.store.put(encoded(getattr(exc, "trace", [])), "application/json")}, target=agent, task=task)
            raise
        finally:
            self.steering.pop(agent, None)

    async def compact(self, agent, task):
        """Owning-model working compaction; no Luna permanent-memory authoring."""
        abandoned = {e["data"]["call"] for e in self.store.events("model.abandoned", target=agent)}
        for failure in self.store.events("model.failed", target=agent):
            started = self.store.get(failure["data"]["call"])
            if started["data"]["purpose"] == "compact" and failure["data"].get("uncertain") and started["id"] not in abandoned:
                raise RuntimeError("Reconcile the prior uncertain compaction before purchasing another")
        role = self.agents.get(agent).role
        request = self.context.request(agent, definitions(role))
        original_items = request["input"]
        through = max((e["seq"] for e in self.store.events(target=agent)), default=0)
        request = {**request, "tools": [], "input": request["input"] + [{"role": "user", "content":
            f"Compact this working context to well under {PROFILES[role]['target']} tokens. Return only a chronological working memo. Preserve motivation, decisions, corrections, uncertainty, source IDs and ALL unresolved obligations. This is your approval of the exact memo; originals remain retrievable."}]}
        response, approval = await self.infer(agent, task, request, "compact")
        return self.context.compact(agent, public_text(response), approval=approval["id"],
                                    through=through, source_items=original_items)

    async def dispatch(self, task, call, approval):
        agent, task_id = task["agent"], task["id"]
        name = call["name"]
        try: args = arguments(self.agents.get(agent).role, call)
        except (ValueError, PermissionError, TypeError) as exc:
            self.tools.finish(agent, task_id, call["call_id"], {"error": str(exc), "status": "not_executed"})
            return
        if any(e["data"]["call_id"] == call["call_id"] for e in self.store.events("tool.completed", target=agent)):
            return
        try:
            if name in ("delegate", "fork_agent", "retrieve", "wait", "attention", "selection_feedback"):
                if name not in {x["name"] for x in definitions(self.agents.get(agent).role)}:
                    raise PermissionError("Coordination tool unavailable to role")
                self.store.append("tool.intent", {"call_id": call["call_id"], "name": name, "arguments": args,
                    "approval": approval}, actor=agent, target=agent, task=task_id,
                    dedup="tool:" + agent + ":" + call["call_id"])
                suffix = hashlib.sha256((agent + call["call_id"]).encode()).hexdigest()[:24]
                if name == "fork_agent":
                    branch = args["agent"]
                    if self.store.events("agent.created", target=branch):
                        if (self.agents.get(branch).seed or {}).get("origin") != approval:
                            raise ValueError("Fork identity belongs to another checkpoint")
                    else:
                        completed = self.store.get(approval)
                        started = self.store.get(completed["data"].get("origin_call", completed["data"]["call"]))
                        request = json.loads(self.store.read(started["data"]["request"]))
                        items = list(request["input"])
                        for source in completed["data"].get("applied_inputs", []):
                            items.append({"role": "user", "content": self.store.get(source)["data"]["text"]})
                        self.context.fork(agent, branch, args["responsibility"], source_items=items, origin=approval)
                    handle = self.open(branch, args["message"], task="fork_" + suffix, parent_task=task_id,
                                       required=True, origin=agent)
                    value = {"handle": handle, "agent": branch, "status": "accepted", "history": "shared immutable checkpoint"}
                elif name == "delegate":
                    worker = args["agent"]
                    if not self.store.events("agent.created", target=worker):
                        self.agents.create(worker, "sol", args["responsibility"], parent=agent)
                    elif self.agents.get(worker).role != "sol" or self.agents.get(worker).responsibility != args["responsibility"]:
                        raise ValueError("Reuse a Sol with the same explicit responsibility, or create another identity")
                    if worker == agent: raise ValueError("Cannot delegate to self")
                    handle = self.open(worker, args["message"], task="assignment_" + suffix, parent_task=task_id,
                        required=True, origin=agent, read_owner=agent)
                    value = {"handle": handle, "agent": worker, "status": "accepted", "parent_waits": True}
                elif name == "retrieve":
                    handle = self.helper(agent, task_id, "Retrieve evidence for: " + args["query"], ident="luna_" + suffix)
                    value = {"handle": handle, "status": "accepted", "delivery": "appendix"}
                elif name == "wait":
                    handles = args["handles"]
                    if not handles: raise ValueError("Wait requires real handles")
                    for handle in handles:
                        if handle.startswith("process_"):
                            self.tools.process_status(handle, agent)
                            self.store.append("dependency.process", {"handle": handle}, target=agent, task=task_id,
                                              dedup="dependency:" + task_id + ":" + handle)
                        else:
                            child = self.tasks().get(handle)
                            if not child or child.get("parent_task") != task_id:
                                raise ValueError("Unknown or unrelated wait handle")
                            self.store.append("dependency.task", {"handle": handle}, target=agent, task=task_id,
                                              dedup="dependency:" + task_id + ":" + handle)
                    value = {"status": "waiting", "handles": handles,
                             "note": "Runtime will resume only after changes or urgent owner input."}
                elif name == "attention":
                    evidence = self.store.get(task["evidence"])
                    owner = evidence["target"]
                    value = {**args, "evidence": evidence["id"], "sources": evidence["data"].get("sources", []),
                             "source_path": evidence["data"].get("path")}
                    self.store.append("appendix.staged", value, actor=agent, target=owner, task=evidence["task"],
                                      dedup="attention:" + task_id)
                    if args["decision"] == "wake":
                        parent = self.tasks().get(evidence["task"])
                        if parent and parent["status"] in ("ready", "waiting"):
                            self.store.append("task.ready", {"urgent": True}, target=owner, task=parent["id"])
                        else:
                            self.open(owner, "A controller judged this late evidence material: " + args["appendix"],
                                      task="followup_" + suffix, origin="terra", evidence=evidence["id"])
                else:
                    for source in args["sources"]: self.store.get(source)
                    value = self.store.append("selection.approved", args, actor=agent, target=agent, task=task_id,
                                              dedup="selection:" + suffix)
                self.tools.finish(agent, task_id, call["call_id"], value)
            else:
                value = self.tools.execute(agent, task_id, call, approval)
                if name == "run_process":
                    self.store.append("dependency.process", {"handle": value["handle"]}, target=agent, task=task_id,
                                      dedup="dependency:" + task_id + ":" + value["handle"])
                if isinstance(value, dict) and value.get("curation_required"):
                    source = value["source"]
                    full = self.store.read(source).decode()
                    handle = "curator_" + hashlib.sha256((agent + call["call_id"]).encode()).hexdigest()[:24]
                    recent = [e for e in self.store.events("model.completed", target=agent, task=task_id) if e["data"].get("purpose") == "turn"][-3:]
                    summaries = [{"source": e["id"], "text": public_text(json.loads(self.store.read(e["data"]["response"])))} for e in recent]
                    message = ("Extract only evidence useful to this task: " + self.question(task) +
                        "\nRecent public progress:\n" + json.dumps(summaries) +
                        "\nReturn JSON with status (complete, partial, unable), summary, and quotes (exact strings from the source). "
                        "Do not publish memory. Full original is retained at " + value["path"] + "\nSOURCE:\n" + full)
                    self.helper(agent, task_id, message, purpose="curation", evidence=value, required=True, ident=handle)
        except (ValueError, PermissionError, KeyError, OSError) as exc:
            self.tools.finish(agent, task_id, call["call_id"], {"error": type(exc).__name__ + ": " + str(exc)})

    def complete(self, task, response, approval):
        agent, task_id = task["agent"], task["id"]
        text = public_text(response)
        if not text.strip():
            raise ValueError("No final response or tool calls; task remains unfinished")
        if task["purpose"] in ("retrieval", "curation"):
            parent = self.tasks()[task["parent_task"]]; owner = parent["agent"]
            data = {"summary": text, "sources": [approval], "path": str(self.store.root / "events"), "status": "complete"}
            if task["purpose"] == "curation":
                data.update(source=task["evidence"]["source"]["sha256"], path=task["evidence"]["path"])
                try:
                    report = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
                    original = self.store.read(task["evidence"]["source"]).decode()
                    if report["status"] not in ("complete", "partial", "unable") or not isinstance(report["summary"], str):
                        raise ValueError("Invalid curation report")
                    if estimate(report) > 6000:
                        raise ValueError("Curation report exceeds the compact evidence target")
                    if any(not isinstance(q, str) or q not in original for q in report.get("quotes", [])):
                        raise ValueError("Curation quote does not match original")
                    data.update(report)
                except (ValueError, KeyError, TypeError) as exc:
                    data.update(status="unable", summary="Curation could not be validated: " + str(exc), quotes=[])
                self.store.append("curation.result", data, actor=agent, target=owner, task=parent["id"],
                                  dedup="curation:" + task_id)
            ev = self.store.append("evidence.ready", data, actor=agent, target=owner, task=parent["id"],
                                   dedup="evidence:" + task_id)
            if task["required"] or task["purpose"] == "curation":
                self.store.append("appendix.staged", data, actor=agent, target=owner, task=parent["id"],
                                  dedup="required-evidence:" + task_id)
            else:
                preferences = [e["data"] for e in self.store.events("selection.approved", target=owner)]
                if not self.store.events("task.opened", task="attention_" + task_id):
                    self.open("terra", "Assess this evidence for " + owner + ". Parent status: " + parent["status"] +
                        ". Stage ordinary supplements; wake only for material corrections or unresolved necessary work.\n" +
                        json.dumps({"question": self.question(parent), "evidence": data, "approved_preferences": preferences}),
                        task="attention_" + task_id, purpose="attention", origin="system", evidence=ev["id"], read_owner=owner,
                        parent_task=parent["id"])
        elif task["purpose"] == "attention":
            if not any(e["dedup"] == "attention:" + task_id for e in self.store.events("appendix.staged")):
                evidence = self.store.get(task["evidence"])
                self.store.append("appendix.staged", {"appendix": text, "reason": "Controller returned prose; staged conservatively",
                    "evidence": evidence["id"]}, target=evidence["target"], task=evidence["task"], dedup="attention:" + task_id)
        elif task.get("parent_task"):
            parent = self.tasks()[task["parent_task"]]
            self.context.append(parent["agent"], [{"role": "user", "content":
                f"Persistent agent {agent} completed assignment {task_id}. Report:\n{text}\nSource: {approval}"}],
                task=parent["id"], dedup="assignment-report:" + task_id)
        else:
            self.store.append("answer", {"text": text, "call": approval, "origin": task["origin"]},
                              actor=agent, target=agent, task=task_id, dedup="answer:" + task_id)
        # Publication/handoff receipts precede terminal lifecycle state. A crash
        # between them replays this idempotent settlement, never new inference.
        self.store.append("task.completed", {"text": text, "call": approval}, target=agent, task=task_id,
                          dedup="completion:" + task_id)

    async def advance(self, task):
        agent, task_id = task["agent"], task["id"]
        try:
            role = self.agents.get(agent).role
            self.context.snapshot(agent, definitions(role))
            if task["purpose"] == "maintenance":
                await self.compact(agent, task_id)
                self.store.append("task.completed", {"operation": "idle working compaction"}, target=agent, task=task_id)
                return
            if not self.store.events("task.initialized", task=task_id):
                if task["input_event"]:
                    self.context.deliver(agent, [self.store.get(task["input_event"])])
                else:
                    message = self.store.read(task["prompt"]).decode() if task.get("prompt") else task["message"]
                    self.context.append(agent, [{"role": "user", "content": message}], task=task_id,
                                        dedup="task-context:" + task_id)
                self.store.append("task.initialized", {}, target=agent, task=task_id, dedup="initialized:" + task_id)
            pending = self.mailbox.pending(agent)
            if pending:
                self.context.deliver(agent, pending)
                self.mailbox.consume(agent, [e["id"] for e in pending], task_id)
                self.store.append("task.ready", {"urgent": False}, target=agent, task=task_id)
            previous = [e for e in self.store.events("model.completed", target=agent, task=task_id)
                        if e["data"].get("purpose") == "turn"]
            if previous and not pending and not self.dependencies(task_id):
                last = previous[-1]
                response = json.loads(self.store.read(last["data"]["response"]))
                later_history = self.store.events("history.added", target=agent, after=last["seq"])
                if not later_history and not any(i.get("type") == "function_call" for i in response.get("output", [])):
                    self.complete(task, response, last["id"])
                    return
            self.flush_appendices(agent, task_id)
            # Recovery dispatches only completed native call items, before fresh inference.
            calls = self.context.pending_calls(agent)
            if calls:
                completed = [e for e in self.store.events("model.completed", target=agent)
                             if e["data"].get("purpose") == "turn"]
                approval = completed[-1]["id"]
                for call in calls:
                    await self.dispatch(task, call, approval)
                if self.dependencies(task_id):
                    self.store.append("task.waiting", {}, target=agent, task=task_id)
                return
            request = self.context.request(agent, definitions(role))
            snapshot_id = self.context.snapshot(agent)["id"]
            deferred = any(e["data"]["snapshot"] == snapshot_id for e in self.store.events("compaction.deferred", target=agent))
            if self.context.tokens(agent, request) > PROFILES[role]["compact_at"] and not deferred:
                try:
                    await self.compact(agent, task_id)
                except (Exception,) as exc:
                    self.store.append("compaction.deferred", {"snapshot": snapshot_id,
                        "error": type(exc).__name__ + ": " + str(exc)}, target=agent, task=task_id)
                pending = self.mailbox.pending(agent)
                if pending:
                    self.context.deliver(agent, pending)
                    self.mailbox.consume(agent, [e["id"] for e in pending], task_id)
                request = self.context.request(agent, definitions(role))
            response, completed = await self.infer(agent, task_id, request)
            if self.tasks()[task_id]["status"] == "paused":
                return  # Keep the completed response; dispatch its tools only after resume.
            calls = self.context.pending_calls(agent)
            # HTTP cannot inject during generation. Before any not-yet-started
            # effect, ingest late owner input and explicitly settle stale calls.
            self.mailbox.ingest()
            if any(e["data"].get("urgent") for e in self.mailbox.pending(agent)):
                for call in calls:
                    self.tools.finish(agent, task_id, call["call_id"], {
                        "status": "not_executed", "reason": "Urgent owner update arrived before dispatch; reconsider this action."})
                return
            for call in calls:
                await self.dispatch(task, call, completed["id"])
            if role == "terra" and any(c["name"] == "attention" for c in calls):
                self.store.append("task.completed", {"call": completed["id"], "via": "attention decision"},
                                  target=agent, task=task_id, dedup="completion:" + task_id)
                return
            if self.dependencies(task_id):
                self.store.append("task.waiting", {}, target=agent, task=task_id)
            elif not calls:
                # A real owner input arriving during generation takes precedence over delivery.
                if self.mailbox.pending(agent):
                    self.store.append("answer.deferred", {"call": completed["id"]}, target=agent, task=task_id)
                else:
                    self.complete(task, response, completed["id"])
        except (Exception, asyncio.CancelledError) as exc:
            error = type(exc).__name__ + ": " + str(exc)
            if task["purpose"] in ("retrieval", "curation"):
                parent = self.tasks().get(task["parent_task"])
                if parent:
                    data = {"status": "unable", "summary": "Background evidence failed: " + error,
                            "sources": [task["opened"]]}
                    if task["purpose"] == "curation":
                        data.update(source=task["evidence"]["source"]["sha256"], path=task["evidence"]["path"])
                        self.store.append("curation.result", data, actor=agent, target=parent["agent"], task=parent["id"])
                    self.store.append("appendix.staged", data, actor=agent, target=parent["agent"], task=parent["id"])
                self.store.append("task.failed", {"error": error}, target=agent, task=task_id)
            elif task["purpose"] == "maintenance":
                self.store.append("compaction.deferred", {"snapshot": self.context.snapshot(agent)["id"], "error": error}, target=agent, task=task_id)
                self.store.append("task.failed", {"error": error}, target=agent, task=task_id)
            else:
                self.store.append("task.blocked", {"error": error}, target=agent, task=task_id)
            if isinstance(exc, asyncio.CancelledError): raise

    def idle_maintenance(self):
        """Create one owning compaction only after 20 quiet minutes and >100k tail tokens."""
        if self.provider is None or self.active or any(t["status"] not in ("completed", "failed") for t in self.tasks().values()):
            return
        inputs = self.store.events("input.received")
        if not inputs or time.time() - dt.datetime.fromisoformat(inputs[-1]["at"]).timestamp() < self.idle_seconds:
            return
        for agent in self.agents.all():
            if not agent.writer or self.mailbox.pending(agent.id): continue
            snapshots = self.store.events("context.snapshot", target=agent.id)
            if not snapshots: continue
            if any(e["data"]["snapshot"] == snapshots[-1]["id"] for e in self.store.events("compaction.deferred", target=agent.id)): continue
            if self.context.tail_tokens(agent.id) > 100_000 and not self.context.pending_calls(agent.id):
                self.open(agent.id, "Automatic working compaction after idle threshold", purpose="maintenance", origin="timer")
                break

    def recover(self):
        finished = {e["data"]["call"] for kind in ("model.completed", "model.failed", "model.abandoned")
                    for e in self.store.events(kind)}
        for event in self.store.events("model.started"):
            if event["id"] not in finished:
                self.store.append("model.failed", {"call": event["id"], "uncertain": True,
                    "error": "Runtime stopped before an inference receipt; explicit reconciliation required"},
                    target=event["target"], task=event["task"])
                self.store.append("task.blocked", {"error": "Uncertain inference " + event["id"]},
                                  target=event["target"], task=event["task"])

    async def run(self, *, watch=False, seconds=600):
        lock = (self.store.root / ".runtime.lock").open("a+")
        try:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError("A runtime already owns this workspace")
            self.recover(); began = time.monotonic()
            while not self.stopping and time.monotonic() - began < seconds:
                self.ingest(); self.poll()
                for agent, job in list(self.active.items()):
                    maintenance = next((t for t in self.tasks().values() if t["agent"] == agent and
                                       t["purpose"] == "maintenance" and t["status"] == "ready"), None)
                    if maintenance and self.mailbox.pending(agent): job.cancel()
                if time.monotonic() >= self._archive_at:
                    from .storage import Archive
                    Archive(self.store).due()
                    self._archive_at = time.monotonic() + 3600
                for agent, job in list(self.active.items()):
                    if job.done():
                        with contextlib.suppress(asyncio.CancelledError): await job
                        del self.active[agent]
                self.idle_maintenance()
                tasks = self.tasks()
                for task in tasks.values():
                    if len(self.active) >= 4: break
                    if task["status"] != "ready" or task["agent"] in self.active: continue
                    if self.dependencies(task["id"]) and not task.get("urgent"): continue
                    self.active[task["agent"]] = asyncio.create_task(self.advance(task))
                atomic(self.store.root / "status.json", encoded({"at": time.time(), "pid": __import__('os').getpid(),
                    "active_agents": list(self.active), **self.status()}))
                unfinished = any(t["status"] in ("ready", "waiting") for t in self.tasks().values())
                if not watch and not self.active and not unfinished: break
                # Blocked dependencies do not keep an otherwise idle --once run spinning.
                if not watch and not self.active and all(t["status"] != "ready" for t in self.tasks().values()):
                    processes = any(not p.with_name("done.json").exists() for p in (self.store.root / "processes").glob("*/spec.json"))
                    if not processes: break
                await asyncio.sleep(.2)
        finally:
            for job in self.active.values(): job.cancel()
            if self.active: await asyncio.gather(*self.active.values(), return_exceptions=True)
            lock.close()
        return self.status()

    def status(self):
        tasks = [{k: v for k, v in t.items() if k not in ("message", "evidence", "text")} |
                 {"objective": t["message"][:240]} for t in self.tasks().values()]
        return {"tasks": tasks, "budget": self.budget.status(),
                "agents": [{"id": a.id, "role": a.role, "responsibility": a.responsibility} for a in self.agents.all()],
                "pending_inputs": {a.id: len(self.mailbox.pending(a.id)) for a in self.agents.all()},
                "staged_appendices": len(self.store.events("appendix.staged")) - len(self.store.events("appendix.delivered"))}
