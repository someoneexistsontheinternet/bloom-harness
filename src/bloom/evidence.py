"""Durable parallel Luna inspections joined by one native Astra tool result."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from . import turns, progress, reporting
from .privacy import clean
from .store import now

TERMINAL = ("completed", "blocked", "cancelled")

PROMPT = """You are Luna, a scoped evidence worker for Astra.
Astra owns the reasoning plan, decisions, interpretation and final answer. Carry
out your assigned inspection independently, gathering facts that answer its
specific questions. Use files and bounded read-only commands in your assigned
directory. Follow applicable instructions; do not edit, extract large archives,
start services, migrate data or replay historical instructions. Use the project
interpreter given in your scope. If a preferred command is missing, use an
available equivalent. Finish process observations yourself; do not hand polling
back to Astra. Independent workers run concurrently, so stay within your scope.
Start with relevant project/archive roots. Unless explicitly needed, avoid
recursive discovery through Library, Desktop, Documents, Downloads, caches or
unrelated data. Commands are time-bounded; if a scan stalls, use a narrower
query or available files. Access denials and paths outside your grant are
limitations to report, not reasons to repeatedly try the same access.
Batch independent file reads and bounded commands. Once you establish that the
needed bytes are unavailable in your scope, return a precise partial report;
do not spend the whole deadline searching unrelated operational history. Private
runtime databases/logs and paths outside your directory are not granted by a
parent's prose. Cite the missing grant and stop that branch. Reuse the applicable
instructions already supplied in your context instead of rediscovering them.
Stop as soon as the assigned criteria are satisfied. A located API or an exact
requested source is enough; do not audit its whole implementation unless asked.
You may use multiple inexpensive decisions to inspect sources and resolve gaps.
Return a final JSON object with:
{"status":"found|partial|not_found|unavailable","summary":"facts and limitations",
 "evidence":[{"operation_id":"op_...","quote":"short exact source excerpt"}]}
Prefer a summary under 5000 characters; longer valid summaries are accepted when
needed to retain material evidence. At most six quotes of at most 1500 characters.
Each quote must appear verbatim in a successful tool result of this task; use
its receipt operation_id. Preserve dates and distinguish historical verification
from checks you performed now. Return partial/unavailable if evidence is missing.
Do not decide the owner's overall question or write their final response. Your
findings are evidence for Astra to judge. Ordinary final prose needs no separate
set_outcome call. Do not copy private reasoning or credentials into reports.
"""


class EvidencePending(Exception):
    """Await durable child tasks without another inference."""


class Evidence:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.settings = self.store.config.data["runtime"]

    def worker(self, task):
        return self.store.one(
            "SELECT w.*,b.deadline FROM evidence_workers w JOIN evidence_batches b ON b.operation_id=w.operation_id WHERE w.task_id=?",
            (task,),
        )

    def children(self, operation):
        return self.store.rows(
            "SELECT t.* FROM evidence_workers w JOIN tasks t ON t.id=w.task_id WHERE w.operation_id=? ORDER BY w.position",
            (operation,),
        )

    def start(self, agent, task, operation, plan, assignments):
        if self.store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"] not in (
            "astra",
            "sol",
        ):
            raise PermissionError("Only Astra or Sol assigns parallel evidence work")
        digest = self.store.put({"plan": plan, "assignments": assignments})
        batch = self.store.one("SELECT * FROM evidence_batches WHERE operation_id=?", (operation,))
        if batch:
            if batch["request_hash"] != digest or batch["parent_task"] != task:
                raise ValueError("Evidence batch identity was reused with different work")
        else:
            if not plan.strip() or not 1 <= len(assignments) <= self.settings.get(
                "evidence_workers", 3
            ):
                raise ValueError("Provide a reasoning plan and one to three evidence assignments")
            parent = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
            cwd = self.store.one("SELECT cwd FROM agents WHERE id=?", (agent,))["cwd"]
            scoped = []
            for assignment in assignments:
                path = Path(assignment.get("cwd", cwd)).expanduser().resolve()
                if not assignment["question"].strip() or not path.is_dir():
                    raise ValueError("Each evidence question needs an existing assigned directory")
                scoped.append((assignment["question"], path))
            owner = self.store.one(
                "SELECT source_item FROM owner_turns WHERE id=?", (turns.current(self.store, task),)
            )
            question = (
                self.store.one("SELECT text FROM items WHERE id=?", (owner["source_item"],))["text"]
                if owner
                else parent["objective"]
            )
            with self.store.transaction():
                self.store.db.execute(
                    "INSERT INTO evidence_batches VALUES(?,?,?,?,?,?)",
                    (
                        operation,
                        task,
                        digest,
                        self.runtime.steering_epoch(task),
                        time.time() + self.settings.get("evidence_seconds", 300),
                        now(),
                    ),
                )
                for position, (assignment, path) in enumerate(scoped):
                    identity = hashlib.sha256(f"{operation}:{position}".encode()).hexdigest()[:24]
                    child, worker = "evidence_task_" + identity, "evidence_" + identity
                    objective = json.dumps(
                        {
                            "owner_question": question,
                            "astra_plan": plan,
                            "assignment": assignment,
                            "directory": str(path),
                            "recent_public_summaries": progress.recent(self.store, task),
                        },
                        ensure_ascii=False,
                    )
                    self.store.db.execute(
                        "INSERT INTO agents VALUES(?,?,?,?,?,?,?,?)",
                        (
                            worker,
                            "luna",
                            agent,
                            "Luna evidence " + str(position + 1),
                            "read",
                            0,
                            str(path),
                            now(),
                        ),
                    )
                    self.store.db.execute(
                        "INSERT INTO sessions VALUES(?,?,?)", (worker, worker, now())
                    )
                    self.store.new_task(assignment, worker, task, parent["channel"], "", key=child)
                    from . import evidence_grants

                    evidence_grants.sources(
                        self.store, agent, worker, task, assignments[position].get("sources", [])
                    )
                    self.store.item(
                        worker,
                        {"role": "user", "content": objective},
                        child,
                        key=child + ":assignment",
                        origin="delegation",
                    )
                    self.store.db.execute(
                        "UPDATE tasks SET max_turns=? WHERE id=?",
                        (self.settings.get("evidence_turns", 20), child),
                    )
                    self.store.db.execute(
                        "INSERT INTO evidence_workers VALUES(?,?,?)", (child, operation, position)
                    )
                self.store.event(
                    "evidence.started",
                    {"operation": operation, "workers": len(scoped), "plan": plan},
                    task,
                    agent,
                )

    def gather(self, agent, task, operation, plan, assignments):
        self.start(agent, task, operation, plan, assignments)
        children = self.children(operation)
        if any(child["status"] not in TERMINAL for child in children):
            raise EvidencePending()
        return {
            "plan": plan,
            "reports": [self.report(child) for child in children],
            "instruction": "Candidate evidence only. Astra must assess relevance, conflicts and sufficiency, then reason and answer. Expand cited originals only where material.",
        }

    def report(self, task):
        final = (
            self.store.one("SELECT text,model_call_id FROM items WHERE id=?", (task["final_item"],))
            if task["final_item"]
            else None
        )
        result = {
            "task": task["id"],
            "status": "unavailable",
            "summary": task["error"] or "No usable evidence report.",
            "evidence": [],
        }
        try:
            if task["status"] != "completed" or not final:
                result["evidence"] = self.partial_sources(task["id"])
                if result["evidence"]:
                    result["status"] = "partial"
                    result["summary"] += (
                        " Retained exact tool excerpts follow; no completed worker synthesis is claimed."
                    )
                return result
            value = json.loads(
                final["text"]
                .strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            if value["status"] not in (
                "found",
                "partial",
                "not_found",
                "unavailable",
            ) or not isinstance(value["summary"], str):
                raise ValueError("Invalid evidence summary")
            if not isinstance(value["evidence"], list) or len(value["evidence"]) > 6:
                raise ValueError("Invalid evidence list")
            sources = []
            for source in value["evidence"]:
                receipt = self.store.one(
                    "SELECT id,name,result_hash FROM operations WHERE id=? AND task_id=? AND status='completed'",
                    (source["operation_id"], task["id"]),
                )
                quote = source["quote"]
                if not receipt or not isinstance(quote, str) or not quote or len(quote) > 1500:
                    raise ValueError("Evidence must cite this worker’s successful tool receipts")
                raw = self.store.get(receipt["result_hash"])
                candidates = [json.dumps(raw, ensure_ascii=False)]
                if isinstance(raw, dict):
                    candidates += [v for v in raw.values() if isinstance(v, str)]
                if not any(quote in text for text in candidates):
                    raise ValueError("Evidence quotation differs from its original")
                sources.append({**receipt, "quote": quote})
            if value["status"] == "found" and not sources:
                raise ValueError("Found evidence needs a verified quote")
            result.update(
                status=value["status"],
                summary=value["summary"],
                evidence=sources,
                model_call_id=final["model_call_id"],
            )
        except (ValueError, TypeError, KeyError) as exc:
            result["summary"] = "Worker report could not be validated: " + clean(str(exc))
            result["invalid_report"] = True
            result["evidence"] = self.partial_sources(task["id"])
            if result["evidence"]:
                result["status"] = "partial"
        return result

    def partial_sources(self, task):
        sources = []
        for receipt in self.store.rows(
            "SELECT id,name,result_hash FROM operations WHERE task_id=? AND status='completed' ORDER BY rowid DESC",
            (task,),
        ):
            raw = self.store.get(receipt["result_hash"])
            if not isinstance(raw, dict):
                continue
            text = raw.get("text") or raw.get("output")
            if not isinstance(text, str) or not text.strip():
                continue
            source = {
                **receipt,
                "quote": text[:1500],
                "excerpt_truncated": len(text) > 1500,
                "process_result": raw.get("result"),
                "path": raw.get("path"),
            }
            if not any(old["quote"] == source["quote"] for old in sources):
                sources.append(source)
            if len(sources) == 6:
                break
        return list(reversed(sources))

    def refresh(self, task):
        worker = self.worker(task)
        if not worker:
            return
        reporting.prepare(self.store, task, worker["deadline"], reserve_turns=4, reserve_seconds=90)

    def retry_report(self, task):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        report = self.report(row)
        if not report.get("invalid_report"):
            return False
        attempts = self.store.one(
            "SELECT count(*) n FROM items i JOIN item_origins o ON o.item_id=i.id WHERE i.task_id=? AND o.kind='evidence_feedback'",
            (task,),
        )["n"]
        state = reporting.phase(self.store, task)
        exhausted = row["turns"] >= (
            state["start_turn"] + reporting.MAX_CALLS if state else row["max_turns"]
        )
        if (
            attempts >= 1
            or exhausted
            or reporting.deadline(self.store, task, self.worker(task)["deadline"]) <= time.time()
        ):
            return False
        reporting.prepare(
            self.store,
            task,
            self.worker(task)["deadline"],
            reserve_turns=4,
            reserve_seconds=90,
            force=True,
        )
        self.store.item(
            row["agent_id"],
            {
                "role": "user",
                "content": "Your report needs correction before Astra receives it. "
                + report["summary"]
                + ". Use the required JSON shape and successful receipt operation IDs; quote an exact excerpt from your tool result. "
                "Use partial/unavailable for missing evidence. Correct the report using retained work; do not repeat completed effects.",
            },
            task,
            key=f"{task}:report-feedback:{attempts}",
            origin="evidence_feedback",
        )
        self.store.db.execute("UPDATE tasks SET final_item=NULL WHERE id=?", (task,))
        self.store.status(task, "ready")
        return True

    async def stop(self, task, reason):
        job = self.runtime.active.get(task)
        if job and job is not asyncio.current_task():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        for process in self.store.rows(
            "SELECT p.id FROM processes p JOIN operations o ON o.id=p.operation_id WHERE o.task_id=? AND p.status IN ('prepared','running')",
            (task,),
        ):
            self.store.event(
                "evidence.stop_process",
                {"process": process["id"], "reason": reason},
                task,
                row["agent_id"],
            )
            try:
                await self.runtime.tools.processes.control(row["agent_id"], process["id"], "cancel")
                for _ in range(20):
                    observed = await self.runtime.tools.processes.observe(
                        row["agent_id"], process["id"], limit=1
                    )
                    if observed.get("result"):
                        break
                    await asyncio.sleep(0.1)
            except Exception as exc:
                self.store.event(
                    "evidence.stop_error",
                    {"process": process["id"], "error": clean(str(exc))[:500]},
                    task,
                )
        for op in self.store.rows(
            "SELECT * FROM operations WHERE task_id=? AND status='running'", (task,)
        ):
            result = {"error": reason, "interrupted": True}
            digest = self.store.put(result)
            self.store.db.execute(
                "UPDATE operations SET status='failed',error=?,result_hash=?,completed_at=? WHERE id=?",
                (reason, digest, now(), op["id"]),
            )
            call = self.store.one(
                "SELECT c.* FROM tool_calls c LEFT JOIN tool_results r ON r.call_id=c.id WHERE c.operation_id=? AND r.call_id IS NULL",
                (op["id"],),
            )
            if call:
                self.runtime.result_item(
                    call,
                    {
                        "result": result,
                        "receipt": {"operation_id": op["id"], "artifact_hash": digest},
                    },
                    True,
                )
        self.runtime.block(task, reason)

    async def step(self, task, work):
        try:
            limit = reporting.prepare(
                self.store, task, self.worker(task)["deadline"], reserve_turns=4, reserve_seconds=90
            )
            if limit <= time.time() and not reporting.phase(self.store, task):
                limit = reporting.prepare(
                    self.store,
                    task,
                    self.worker(task)["deadline"],
                    reserve_turns=4,
                    reserve_seconds=90,
                    force=True,
                )
            phase = reporting.phase(self.store, task)
            profile = self.store.config.profile("luna")
            # Stop NEW exploration at its deadline; retain the response of an
            # already purchased inference before moving to report-only mode.
            remaining = (
                (limit - time.time())
                if phase
                else self.store.config.data["providers"][profile["provider"]].get(
                    "timeout_seconds", 1200
                )
            )
            if remaining <= 0:
                work.close()
                raise TimeoutError
            async with asyncio.timeout(remaining):
                await work
            row = self.store.one("SELECT status FROM tasks WHERE id=?", (task,))
            if (
                row["status"] not in TERMINAL
                and not phase
                and time.time() >= self.worker(task)["deadline"]
            ):
                reporting.prepare(
                    self.store,
                    task,
                    self.worker(task)["deadline"],
                    reserve_turns=4,
                    reserve_seconds=90,
                    force=True,
                )
        except TimeoutError:
            await self.stop(task, "Evidence inspection deadline reached; partial receipts retained")

    async def poll(self):
        batches = self.store.rows(
            "SELECT b.*,o.status AS operation_status,t.status AS parent_status,l.operation_id AS background FROM evidence_batches b JOIN operations o ON o.id=b.operation_id JOIN tasks t ON t.id=b.parent_task LEFT JOIN worker_launches l ON l.operation_id=b.operation_id WHERE o.status IN ('running','prepared') OR (l.report_item IS NULL AND l.obsolete=0 AND l.operation_id IS NOT NULL)"
        )
        for batch in batches:
            if batch["parent_status"] == "paused":
                continue
            children = self.children(batch["operation_id"])
            superseded = (
                self.runtime.steering_epoch(batch["parent_task"]) != batch["steering_epoch"]
                or batch["parent_status"] in ("blocked", "cancelled")
                or (
                    batch["parent_status"] == "completed"
                    and any(self.runtime.workers.required(c["id"]) for c in children)
                )
            )
            for child in children:
                if (
                    not superseded
                    and child["status"] not in TERMINAL
                    and child["id"] not in self.runtime.active
                    and time.time() >= batch["deadline"]
                    and not reporting.phase(self.store, child["id"])
                ):
                    reporting.prepare(
                        self.store,
                        child["id"],
                        batch["deadline"],
                        reserve_turns=4,
                        reserve_seconds=90,
                        force=True,
                    )
            if superseded or any(
                time.time() >= reporting.deadline(self.store, c["id"], batch["deadline"])
                for c in children
            ):
                for child in children:
                    expired = time.time() >= reporting.deadline(
                        self.store, child["id"], batch["deadline"]
                    )
                    interruptible = (
                        bool(reporting.phase(self.store, child["id"]))
                        or child["id"] not in self.runtime.active
                    )
                    if child["status"] not in TERMINAL and (
                        superseded or (expired and interruptible)
                    ):
                        await self.stop(
                            child["id"],
                            "Evidence inspection superseded by owner control"
                            if superseded
                            else "Evidence/report deadline reached; partial receipts retained",
                        )
                children = self.children(batch["operation_id"])
                if superseded and batch["background"]:
                    self.store.db.execute(
                        "UPDATE worker_launches SET obsolete=1 WHERE operation_id=?",
                        (batch["operation_id"],),
                    )
            if batch["background"]:
                continue  # Explicit await/final joins collect these reports.
            if all(child["status"] in TERMINAL for child in children):
                self.store.db.execute(
                    "UPDATE operations SET status='prepared' WHERE id=?", (batch["operation_id"],)
                )
                if batch["parent_status"] == "waiting":
                    self.store.status(batch["parent_task"], "ready")
