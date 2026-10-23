from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
from pathlib import Path

import jsonschema

from .context import Context
from .delivery import Delivery, Discord
from .models import Models
from .execution import ProcessPending, UncertainEffect
from .privacy import clean
from .store import atomic, encoded, now, uid
from .tools import Tools
from .workspaces import Workspaces
from .gateway import Gateway
from .service import process_identity
from . import __version__, turns
from .inbox import drain as drain_inbox
from .results import Results
from .recall import Recall
from .evidence import Evidence, EvidencePending
from . import progress


TERMINAL = ("completed", "blocked", "cancelled")
REQUEST_FIELDS = {
    "plan": {"text"},
    "delegate": {
        "role",
        "purpose",
        "objective",
        "mode",
        "network",
        "cwd",
        "repository",
        "environment_names",
        "required",
    },
    "launch_worker": {"worker_kind", "question", "objective", "cwd", "required", "sources"},
    "wait": {"children", "processes", "owner_messages", "after_seconds", "seconds"},
    "task_status": {"task"},
    "steer": {"task", "text"},
    "pause": {"task"},
    "resume": {"task"},
    "memory": {"topic", "expected_revision", "summary", "detail", "rationale", "sources"},
    "memory_update": {"topic", "expected_revision", "summary", "detail", "rationale", "sources"},
    "memory_record": {"batch", "proposal"},
    "compact": set(),
    "submit": set(),
    "check_submission": {"task", "command"},
    "review_submission": {"release", "expected_diff_sha256", "notes"},
    "integrate_submission": {"release"},
    "rollback_submission": {"release"},
    "outcome": {"status", "reason", "evidence_refs"},
}


class Runtime:
    def __init__(self, store, models=None, delivery=None):
        self.store = store
        self.config = store.config
        self.models = models or Models(store)
        self.context = Context(store, self.models)
        self.results = Results(store, self.models, self.context.count)
        self.delivery = delivery or Delivery(store)
        self.tools = Tools(store, self.request, self.models)
        self.workspaces = Workspaces(store)
        self.active = {}
        self.stopping = False
        self.memory_job = None
        self.memory_attempt_at = 0
        self.recall = Recall(self)
        self.evidence = Evidence(self)
        from .workers import Workers

        self.workers = Workers(self)
        from .assignments import Assignments

        self.assignments = Assignments(self)

    def recover(self):
        self.recall.recover()
        # A completed response is reusable; an unacknowledged external effect is not.
        self.store.db.execute(
            "UPDATE model_calls SET status='uncertain',error='Runtime interrupted before durable completion' WHERE status='running'"
        )
        self.store.db.execute(
            "UPDATE model_calls SET status='failed',error='Retrieval request prepared but not dispatched before restart' WHERE status='prepared' AND task_id IN (SELECT task_id FROM memory_retrievals)"
        )
        self.store.db.execute("UPDATE deliveries SET status='uncertain' WHERE status='sending'")
        self.store.db.execute(
            "UPDATE channel_routes SET status='uncertain' WHERE status='creating'"
        )
        for op in self.store.rows("SELECT * FROM operations WHERE status='running'"):
            if self.store.one("SELECT 1 FROM task_assignments WHERE operation_id=?", (op["id"],)):
                self.store.db.execute(
                    "UPDATE operations SET status='prepared' WHERE id=?", (op["id"],)
                )
                continue
            if self.store.one("SELECT 1 FROM worker_batches WHERE operation_id=?", (op["id"],)):
                self.store.db.execute(
                    "UPDATE operations SET status='prepared' WHERE id=?", (op["id"],)
                )
                continue
            if self.store.one("SELECT 1 FROM worker_awaits WHERE operation_id=?", (op["id"],)):
                self.store.db.execute(
                    "UPDATE operations SET status='prepared' WHERE id=?", (op["id"],)
                )
                continue
            if self.store.one("SELECT 1 FROM process_awaits WHERE operation_id=?", (op["id"],)):
                self.store.db.execute(
                    "UPDATE operations SET status='prepared' WHERE id=?", (op["id"],)
                )
                continue
            if op["name"] == "gather_evidence" and self.store.one(
                "SELECT 1 FROM evidence_batches WHERE operation_id=?", (op["id"],)
            ):
                self.store.db.execute(
                    "UPDATE operations SET status='prepared' WHERE id=?", (op["id"],)
                )
                continue
            control = self.store.one(
                "SELECT c.* FROM controls c JOIN operation_controls l ON l.control_id=c.id WHERE l.operation_id=?",
                (op["id"],),
            )
            if control and control["status"] in ("completed", "failed"):
                if control["command"] == "memory_update" and control["status"] == "completed":
                    self.memory_mirror(
                        self.store.get(control["result_hash"])["topic"], op["task_id"]
                    )
                self.store.db.execute(
                    "UPDATE operations SET status=?,result_hash=?,completed_at=? WHERE id=?",
                    (control["status"], control["result_hash"], now(), op["id"]),
                )
                continue
            if op["name"] in ("memory_get", "memory_review") or (
                control
                and control["command"] in ("memory_update", "memory_record")
                and control["status"] == "running"
            ):
                self.store.db.execute(
                    "UPDATE operations SET status='prepared' WHERE id=?", (op["id"],)
                )
                continue  # Typed write and receipt commit in one transaction; reads are repeatable.
            if op["name"] == "apply_request" and self.store.one(
                "SELECT 1 FROM preset_invocations WHERE operation_id=?", (op["id"],)
            ):
                self.store.db.execute(
                    "UPDATE operations SET status='prepared' WHERE id=?", (op["id"],)
                )
                continue  # Captured local launch intent; worker/control publication is atomic.
            if op["name"] == "start_process":
                proc = self.store.one("SELECT id FROM processes WHERE operation_id=?", (op["id"],))
                if proc:
                    # Resume observation of the persisted handle, including its
                    # output. Processes.start never launches twice for this ID.
                    self.store.db.execute(
                        "UPDATE operations SET status='prepared' WHERE id=?", (op["id"],)
                    )
                    continue
            if op["name"] == "observe_process":
                self.store.db.execute(
                    "UPDATE operations SET status='prepared' WHERE id=?", (op["id"],)
                )
                continue
            self.store.db.execute(
                "UPDATE operations SET status='uncertain',error='Interrupted effect requires reconciliation' WHERE id=?",
                (op["id"],),
            )
        self.store.db.execute("UPDATE tasks SET status='ready' WHERE status='running'")
        for row in self.store.rows(
            "SELECT DISTINCT task_id FROM model_calls WHERE status='uncertain' AND task_id IS NOT NULL UNION SELECT DISTINCT task_id FROM operations WHERE status='uncertain' AND task_id IS NOT NULL"
        ):
            self.block(
                row["task_id"],
                "Uncertain effect or inference; inspect exact receipt before explicit reconciliation",
            )

    def notify_parent(self, task):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        if row["status"] not in TERMINAL:
            return
        fingerprint = hashlib.sha256(
            encoded({k: row[k] for k in ("status", "error", "final_item", "turns")})
        ).hexdigest()
        previous = self.store.one(
            "SELECT fingerprint FROM task_notifications WHERE task_id=?", (task,)
        )
        if previous and previous["fingerprint"] == fingerprint:
            return
        item = None
        if self.recall.job(task):
            item = self.recall.publish(task)
        elif self.evidence.worker(task):
            self.workers.stage_evidence(task)  # Durable inbox only; never wakes the parent.
        elif row["parent_id"]:
            parent = self.store.one("SELECT * FROM tasks WHERE id=?", (row["parent_id"],))
            assignment = self.store.one(
                "SELECT background FROM task_assignments WHERE child_task=?", (task,)
            )
            if not assignment or assignment["background"]:
                report = self.assignments.report(task)
                item = self.store.item(
                    parent["agent_id"],
                    {
                        "role": "user",
                        "content": "Child report (candidate claims; inspect supporting evidence):\n"
                        + json.dumps(report, ensure_ascii=False),
                    },
                    parent["id"],
                    key=task + ":handoff:" + fingerprint,
                    origin="child_report" if self.workers.required(task) else "worker_background",
                    source_task=task,
                    source_agent=row["agent_id"],
                )
        elif row["status"] == "blocked" and row["final_item"] is None:
            alert = self.store.one(
                "SELECT id,status FROM deliveries WHERE task_id=? AND kind LIKE 'attention:%' ORDER BY rowid LIMIT 1",
                (task,),
            )
            if alert:
                # One unresolved task already has an attention handle. Retain
                # it even if the cause changes or acknowledgement is uncertain;
                # a new nonce would duplicate the incident notification.
                self.store.event(
                    "task.attention_retained",
                    {
                        "delivery": alert["id"],
                        "delivery_status": alert["status"],
                        "latest_error": row["error"],
                    },
                    task,
                    row["agent_id"],
                )
            else:
                self.delivery.queue(
                    None,
                    task,
                    "Attention needed: "
                    + (row["error"] or "Task blocked")
                    + "\nTask: "
                    + task
                    + ". Retained work is available for inspection.",
                    "attention:" + fingerprint,
                )
        self.store.db.execute(
            "INSERT OR REPLACE INTO task_notifications VALUES(?,?,?)", (task, fingerprint, item)
        )

    def block(self, task, reason):
        paused = (
            self.store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "paused"
        )
        self.store.status(task, "paused" if paused else "blocked", reason)
        self.settle_pending(task)
        self.notify_parent(task)

    def result_item(self, call, value, failed):
        task = self.store.one("SELECT * FROM tasks WHERE id=?", (call["task_id"],))
        source = self.store.one("SELECT call_id FROM items WHERE id=?", (call["item_id"],))[
            "call_id"
        ]
        item = self.store.item(
            task["agent_id"],
            {
                "type": "function_call_output",
                "call_id": source,
                "output": json.dumps(value, ensure_ascii=False),
            },
            task["id"],
            key=call["operation_id"] + ":result",
        )
        self.store.db.execute(
            "INSERT OR IGNORE INTO tool_results VALUES(?,?,?)", (call["id"], item, int(failed))
        )
        if not failed:
            self.workers.consume_batch(call["operation_id"], item)

    def settle_pending(self, task):
        """Close the model protocol without pretending that an uncertain effect failed."""
        calls = self.store.rows(
            "SELECT c.* FROM tool_calls c LEFT JOIN tool_results r ON r.call_id=c.id WHERE c.task_id=? AND r.call_id IS NULL",
            (task,),
        )
        for call in calls:
            operation = self.store.one(
                "SELECT * FROM operations WHERE id=?", (call["operation_id"],)
            )
            if (
                operation
                and operation["name"] == "gather_evidence"
                and (
                    self.store.one(
                        "SELECT 1 FROM evidence_batches WHERE operation_id=?", (operation["id"],)
                    )
                    or self.store.one(
                        "SELECT 1 FROM worker_batches WHERE operation_id=?", (operation["id"],)
                    )
                )
            ):
                continue  # Accepted workers survive steering; collect their receipts.
            if operation and self.store.one(
                "SELECT 1 FROM task_assignments WHERE operation_id=?", (operation["id"],)
            ):
                continue  # Already accepted Sol work is collected, never duplicated.
            if operation and operation["status"] in ("running", "completed", "failed"):
                continue  # In-flight or acknowledged work is settled by its normal result path.
            if operation and operation["status"] == "uncertain":
                value = {
                    "status": "uncertain",
                    "operation_id": operation["id"],
                    "error": operation["error"],
                    "instruction": "Inspect retained evidence; do not blindly repeat this effect.",
                }
            else:
                value = {
                    "status": "cancelled",
                    "not_executed": True,
                    "operation_id": call["operation_id"],
                    "reason": "Task stopped before dispatch; make a fresh decision after steering/resume.",
                }
                if operation:
                    self.store.db.execute(
                        "UPDATE operations SET status='cancelled',result_hash=? WHERE id=?",
                        (self.store.put(value), operation["id"]),
                    )
                else:
                    self.store.db.execute(
                        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,result_hash,started_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            call["operation_id"],
                            task,
                            "tool",
                            call["name"],
                            "cancelled",
                            call["arguments_hash"],
                            self.store.put(value),
                            now(),
                            now(),
                        ),
                    )
            self.result_item(call, value, True)

    def check_tree(self, agent, task):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        if not row:
            raise ValueError("Unknown task")
        owner = row["agent_id"]
        while owner and owner != agent:
            r = self.store.one("SELECT parent_id FROM agents WHERE id=?", (owner,))
            owner = r["parent_id"] if r else None
        if owner != agent:
            raise PermissionError("Task is outside your agent tree")
        return row

    def pause(self, task):
        rows = self.store.rows(
            "WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT t.id FROM tasks t JOIN tree p ON t.parent_id=p.id) SELECT id FROM tree",
            (task,),
        )
        for row in rows:
            status = self.store.one("SELECT status FROM tasks WHERE id=?", (row["id"],))["status"]
            if status not in (*TERMINAL, "paused"):
                self.store.db.execute(
                    "INSERT OR REPLACE INTO task_pauses VALUES(?,?,?,?)",
                    (row["id"], task, status, now()),
                )
                self.store.status(row["id"], "paused")
                self.settle_pending(row["id"])
        self.store.event(
            "tree.paused",
            {
                "retained_processes": "Existing external processes remain observable; cancel by handle."
            },
            task,
        )

    def resume(self, task):
        rows = self.store.rows(
            "WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT t.id FROM tasks t JOIN tree p ON t.parent_id=p.id) SELECT t.* FROM tasks t JOIN tree ON tree.id=t.id LEFT JOIN task_pauses p ON p.task_id=t.id WHERE (t.id=? AND t.status IN ('paused','blocked')) OR (p.pause_root=? AND t.status='paused')",
            (task, task, task),
        )
        for row in rows:
            uncertain = self.store.one(
                "SELECT id FROM operations WHERE task_id=? AND status='uncertain' LIMIT 1",
                (row["id"],),
            )
            # Owner resume permits a fresh inference, whose tools were never dispatched.
            # The unacknowledged attempt and unknown usage remain explicitly recorded.
            self.store.event("owner.resume", {"unacknowledged_inference_retained": True}, row["id"])
            self.store.db.execute("DELETE FROM task_outcomes WHERE task_id=?", (row["id"],))
            self.store.db.execute("DELETE FROM waits WHERE task_id=?", (row["id"],))
            self.store.db.execute("DELETE FROM task_pauses WHERE task_id=?", (row["id"],))
            from .reconciliation import abandon

            for call in self.store.rows(
                "SELECT id FROM model_calls WHERE status='uncertain' AND (task_id=? OR id IN (SELECT call_id FROM working_compactions WHERE task_id=?))",
                (row["id"], row["id"]),
            ):
                abandon(
                    self.store,
                    call["id"],
                    "Owner explicitly resumed the same retained task; preserve prior purchase and unknown usage",
                )
            self.store.status(
                row["id"],
                "blocked" if uncertain else "ready",
                "Explicit reconciliation required" if uncertain else None,
            )

    def steer(self, task, text, owner=True):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        if row["status"] in TERMINAL:
            return self.store.new_task(
                text, row["agent_id"], row["parent_id"], row["channel"], row["address"]
            )
        self.store.item(
            row["agent_id"],
            {"role": "user", "content": "Owner/parent steering:\n" + text},
            task,
            origin="owner_steering" if owner else "steering",
        )
        self.store.event("task.steered", {"text": text}, task, row["agent_id"])
        self.release_process_waits(task)
        if row["status"] == "waiting":
            self.store.status(task, "ready")
            self.store.db.execute("DELETE FROM waits WHERE task_id=?", (task,))
        # Running inference receives this item at the next decision boundary; deadline watchdog cancels it at 90s.
        return task

    def release_process_waits(self, task):
        """Owner steering can break a wait; a polling timer cannot."""
        for wait in self.store.rows(
            "SELECT w.*,p.status AS process_status FROM process_awaits w JOIN operations o ON o.id=w.operation_id LEFT JOIN processes p ON p.id=w.process_id WHERE o.task_id=?",
            (task,),
        ):
            result = {
                "waiting_interrupted_by_owner": True,
                "process_id": wait["process_id"],
                "process_status": wait["process_status"],
                "completion_observed": False,
                "instruction": "Owner steering interrupted the wait. Reuse this handle; do not relaunch accepted work.",
            }
            if not wait["process_id"]:
                result.update(
                    not_executed=True,
                    instruction="Owner steering interrupted the capacity wait before launch.",
                )
            self.store.db.execute(
                "UPDATE operations SET status=?,result_hash=?,completed_at=? WHERE id=?",
                (
                    "completed" if wait["process_id"] else "failed",
                    self.store.put(result),
                    now(),
                    wait["operation_id"],
                ),
            )
            self.store.db.execute(
                "DELETE FROM process_awaits WHERE operation_id=?", (wait["operation_id"],)
            )

    def wake_processes(self):
        """Resume native result collection, never inference, when work settles."""
        waits = self.store.rows(
            "SELECT w.*,o.status AS operation_status,o.task_id,t.status AS task_status,p.status AS process_status FROM process_awaits w JOIN operations o ON o.id=w.operation_id JOIN tasks t ON t.id=o.task_id LEFT JOIN processes p ON p.id=w.process_id"
        )
        occupied = self.store.one(
            "SELECT count(*) n FROM processes WHERE status IN ('prepared','running')"
        )["n"]
        for wait in waits:
            if wait["operation_status"] in ("completed", "failed", "uncertain", "cancelled"):
                self.store.db.execute(
                    "DELETE FROM process_awaits WHERE operation_id=?", (wait["operation_id"],)
                )
                continue
            if wait["task_status"] == "paused" or wait["task_status"] in TERMINAL:
                continue
            settled = (
                wait["process_status"] in ("completed", "failed", "uncertain")
                if wait["process_id"]
                else occupied < self.config.data["runtime"]["max_local_processes"]
            )
            if settled:
                self.store.db.execute(
                    "UPDATE operations SET status='prepared' WHERE id=?", (wait["operation_id"],)
                )
                if wait["task_status"] == "waiting":
                    self.store.status(wait["task_id"], "ready")

    async def interruptible(self, awaitable, task):
        latest = self.steering_epoch(task)
        work = asyncio.create_task(awaitable)
        interrupted_at = None
        try:
            while not work.done():
                await asyncio.wait({work}, timeout=0.2)
                status = self.store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"]
                steering = self.steering_epoch(task) > latest
                if status in ("paused", "cancelled") or steering:
                    interrupted_at = interrupted_at or time.monotonic()
                    if (
                        time.monotonic() - interrupted_at
                        >= self.config.data["runtime"]["mention_seconds"]
                    ):
                        work.cancel()
                        await asyncio.gather(work, return_exceptions=True)
                        raise InterruptedError(
                            "Owner steering/pause interrupted the pending call before new tool dispatch"
                        )
            return await work
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    def steering_epoch(self, task):
        return (
            self.store.one(
                "SELECT max(id) AS n FROM events WHERE task_id=? AND kind='task.steered'", (task,)
            )["n"]
            or 0
        )

    def memory_mirror(self, topic, task):
        try:
            self.context.export_memory(topic)
        except OSError as exc:
            # The accepted revision/receipt already committed. A derived-file
            # failure must not masquerade as a rejected or repeatable write.
            self.store.event(
                "memory.mirror_failed", {"topic": topic, "error": clean(str(exc))}, task, "astra"
            )

    def approve_memory(self, agent, task, operation, request):
        if agent != "astra":
            raise PermissionError(
                "Every semantic memory write requires Astra approval; Sol/Luna may only supply evidence"
            )
        call = self.store.one(
            "SELECT m.* FROM tool_calls c JOIN items i ON i.id=c.item_id JOIN model_calls m ON m.id=i.model_call_id WHERE c.operation_id=? AND c.task_id=?",
            (operation, task),
        )
        if (
            not call
            or call["agent_id"] != "astra"
            or call["purpose"] != "turn"
            or call["status"] != "completed"
            or call["model_requested"] != self.config.profile("astra")["model"]
        ):
            raise PermissionError(
                "Memory approval must be an actual completed Astra turn, not a helper or ownerless backend write"
            )
        digest = self.store.put(request)
        prior = self.store.one(
            "SELECT payload_hash FROM memory_approvals WHERE operation_id=?", (operation,)
        )
        if prior and prior["payload_hash"] != digest:
            raise ValueError("Memory approval identity changed")
        self.store.db.execute(
            "INSERT OR IGNORE INTO memory_approvals VALUES(?,?,?,?,?)",
            (operation, task, call["id"], digest, now()),
        )

    async def request(self, agent, task, operation_id, request):
        kind = "memory_update" if request.get("operation") == "memory" else request.get("operation")
        if kind == "delegate_task":
            if set(request) - {
                "operation",
                "objective",
                "criteria",
                "cwd",
                "sources",
                "repository",
                "background",
            }:
                raise ValueError("Unsupported Sol assignment fields")
            return await self.assignments.run(
                agent, task, operation_id, {k: v for k, v in request.items() if k != "operation"}
            )
        if kind == "memory_review":
            if agent != "astra" or set(request) - {"operation", "source_ids"}:
                raise PermissionError("Astra reviews and approves semantic memory")
            return self.context.history.prepare(agent, task, request.get("source_ids"))
        if kind == "memory_get":
            if agent != "astra" or set(request) != {"operation", "topic"}:
                raise ValueError("Accepted memory_get requires Astra and a topic")
            result = self.context.memory_get(request["topic"])
            owner = self.recall.owner(task)
            return {**result, "owner_source": "item:" + str(owner["id"]) if owner else None}
        if kind == "gather_evidence":
            if set(request) - {"operation", "plan", "assignments", "background"}:
                raise ValueError("Unsupported fields in evidence request")
            if any("path" in a for a in request["assignments"]):
                return self.workers.gather(
                    agent,
                    task,
                    operation_id,
                    request["plan"],
                    request["assignments"],
                    request.get("background", False),
                )
            if request.get("background"):
                raise ValueError("Background batches require explicit preset assignments")
            return self.evidence.gather(
                agent, task, operation_id, request["plan"], request["assignments"]
            )
        if kind == "await_workers":
            if set(request) - {"operation", "tasks"}:
                raise ValueError("Unsupported worker wait fields")
            return self.workers.await_results(agent, task, operation_id, request.get("tasks"))
        if kind not in REQUEST_FIELDS:
            raise ValueError("Unknown domain operation: " + str(kind))
        unknown = set(request) - REQUEST_FIELDS[kind] - {"operation", "request_id"}
        if unknown:
            raise ValueError("Unsupported fields for " + kind + ": " + ", ".join(sorted(unknown)))
        digest = self.store.put(clean(request))
        request_id = request.get("request_id", operation_id)
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a nonempty stable identifier")
        control = hashlib.sha256((agent + ":" + task + ":" + request_id).encode()).hexdigest()
        previous = self.store.one("SELECT * FROM controls WHERE id=?", (control,))
        if previous:
            if previous["request_hash"] != digest:
                raise ValueError("Request identity was reused with different content")
            if self.store.one("SELECT 1 FROM operations WHERE id=?", (operation_id,)):
                self.store.db.execute(
                    "INSERT OR REPLACE INTO operation_controls VALUES(?,?)", (operation_id, control)
                )
            if previous["status"] == "completed":
                if kind == "memory_update":
                    self.memory_mirror(self.store.get(previous["result_hash"])["topic"], task)
                return self.store.get(previous["result_hash"])
            if (
                kind not in ("launch_worker", "memory_update", "memory_record")
                or previous["status"] != "running"
            ):
                raise RuntimeError("This domain request has an unresolved prior receipt")
        else:
            self.store.db.execute(
                "INSERT INTO controls VALUES(?,?,?,?,?,?)",
                (control, kind or "", digest, "running", None, now()),
            )
        if self.store.one("SELECT 1 FROM operations WHERE id=?", (operation_id,)):
            self.store.db.execute(
                "INSERT OR REPLACE INTO operation_controls VALUES(?,?)", (operation_id, control)
            )
        try:
            if kind == "plan":
                self.store.db.execute(
                    "UPDATE tasks SET plan=?,updated_at=? WHERE id=?",
                    (request["text"], now(), task),
                )
                result = {"plan_recorded": True}
            elif kind == "launch_worker":
                with self.store.transaction():
                    result = self.workers.launch(agent, task, operation_id, request)
                    self.store.db.execute(
                        "UPDATE controls SET status='completed',result_hash=? WHERE id=?",
                        (self.store.put(result), control),
                    )
                return result
            elif kind == "delegate":
                parent = self.store.one("SELECT * FROM agents WHERE id=?", (agent,))
                role = request["role"]
                if (
                    parent["role"] == "luna"
                    or role not in ("sol", "luna")
                    or (parent["role"] == "sol" and role != "luna")
                ):
                    raise PermissionError("Invalid delegation role for this parent")
                if (
                    parent["role"] == "astra"
                    and role == "luna"
                    and request.get("purpose") not in ("attempt", "evidence", "retrieval")
                ):
                    raise ValueError(
                        "Direct Astra→Luna requires purpose=attempt, evidence or retrieval"
                    )
                if role == "luna" and (
                    request.get("mode", "read") != "read" or request.get("repository")
                ):
                    raise PermissionError(
                        "Luna reads evidence and may write disposable helper scripts in process scratch; substantive implementation/worktrees belong to Astra or Sol"
                    )
                mode = request.get("mode", "full") if role == "sol" else "read"
                if mode not in ("read", "workspace", "full"):
                    raise ValueError("Invalid worker mode")
                if "network" in request and type(request["network"]) is not bool:
                    raise ValueError("network must be a TOML Boolean")
                environment_names = request.get("environment_names", [])
                if not isinstance(environment_names, list) or any(
                    not isinstance(n, str) or not n.replace("_", "").isalnum()
                    for n in environment_names
                ):
                    raise ValueError(
                        "environment_names must be a list of variable names, not values"
                    )
                if (
                    not isinstance(request.get("objective"), str)
                    or not request["objective"].strip()
                ):
                    raise ValueError("Delegate objective must be nonempty text")
                child, child_task = "agent_" + control[:20], "task_" + control[:20]
                cwd = str(Path(request.get("cwd", parent["cwd"])).expanduser().resolve())
                if not Path(cwd).is_dir():
                    raise ValueError("Worker workspace must be an existing directory")
                if request.get("repository"):
                    from .workspaces import git

                    git(Path(request["repository"]).resolve(), "rev-parse", "HEAD")
                self.store.db.execute(
                    "INSERT INTO agents VALUES(?,?,?,?,?,?,?,?)",
                    (
                        child,
                        role,
                        agent,
                        role.title() + "-" + control[:6],
                        mode,
                        int(request.get("network", role == "sol")),
                        cwd,
                        now(),
                    ),
                )
                self.store.db.execute("INSERT INTO sessions VALUES(?,?,?)", (child, child, now()))
                self.store.new_task(request["objective"], child, task, key=child_task)
                if type(request.get("required", True)) is not bool:
                    raise ValueError("required must be a Boolean completion dependency")
                self.store.db.execute(
                    "INSERT INTO worker_dependencies VALUES(?,?)",
                    (child_task, int(request.get("required", True))),
                )
                self.store.status(
                    child_task, "paused", "Delegation setup has not yet committed its receipt"
                )
                for name in environment_names:
                    self.store.db.execute(
                        "INSERT INTO agent_environment VALUES(?,?)", (child, name)
                    )
                parent_task = self.store.one("SELECT channel FROM tasks WHERE id=?", (task,))
                if parent_task["channel"] == "discord":
                    self.store.db.execute(
                        "UPDATE tasks SET channel='discord',address='' WHERE id=?", (child_task,)
                    )
                    self.store.db.execute(
                        "INSERT INTO channel_routes VALUES(?,?,?,?,?,?,?)",
                        (
                            child_task,
                            task,
                            self.config.data["discord"]["channel_id"],
                            role.title() + " · " + request["objective"][:70],
                            "prepared",
                            None,
                            None,
                        ),
                    )
                if request.get("repository"):
                    workspace = self.workspaces.create(child_task, request["repository"])
                    self.store.db.execute(
                        "UPDATE agents SET cwd=? WHERE id=?", (workspace["path"], child)
                    )
                result = {"agent_id": child, "task_id": child_task, "role": role}
                self.store.status(child_task, "ready")
            elif kind == "wait":
                children = request.get("children", [])
                processes = request.get("processes", [])
                if not isinstance(children, list) or not isinstance(processes, list):
                    raise ValueError(
                        "children and processes must be lists; empty lists are allowed"
                    )
                for child in children:
                    self.check_tree(agent, child)
                    if child == task:
                        raise ValueError("Cannot wait on yourself")
                for process in processes:
                    self.tools.processes.authorize(agent, process)
                seconds = float(request.get("seconds", self.config.data["runtime"]["wait_seconds"]))
                if (
                    not math.isfinite(seconds)
                    or not 0 < seconds <= self.config.data["runtime"]["max_wait_seconds"]
                ):
                    raise ValueError("Wait guard exceeds its configured positive maximum")
                messages = request.get("owner_messages", 0)
                after = float(request.get("after_seconds", 0))
                if (
                    type(messages) is not int
                    or messages < 0
                    or not math.isfinite(after)
                    or after < 0
                ):
                    raise ValueError(
                        "owner_messages must be a nonnegative integer; after_seconds must be finite and nonnegative"
                    )
                epoch = self.store.one("SELECT max(id) AS n FROM events")["n"] or 0
                with self.store.transaction():
                    self.store.db.execute("DELETE FROM waits WHERE task_id=?", (task,))
                    self.store.db.execute(
                        "INSERT INTO waits VALUES(?,?)", (task, time.time() + seconds)
                    )
                    self.store.db.executemany(
                        "INSERT INTO wait_children VALUES(?,?)", [(task, c) for c in set(children)]
                    )
                    self.store.db.executemany(
                        "INSERT INTO wait_processes VALUES(?,?)",
                        [(task, p) for p in set(processes)],
                    )
                    self.store.db.execute(
                        "INSERT INTO wait_conditions VALUES(?,?,?,?)",
                        (task, epoch, messages, time.time() + after if after else 0),
                    )
                    self.store.status(task, "waiting")
                result = {
                    "waiting_for_all": {
                        "children": children,
                        "processes": processes,
                        "owner_messages": messages,
                        "after_seconds": after,
                    },
                    "guard_seconds": seconds,
                }
            elif kind in ("task_status", "steer", "pause", "resume"):
                selected = request.get("task", task)
                row = self.check_tree(agent, selected)
                if kind == "task_status":
                    result = {
                        "task": row,
                        "recent_items": self.store.rows(
                            "SELECT id,role,kind,text FROM items WHERE task_id=? ORDER BY id DESC LIMIT 10",
                            (selected,),
                        ),
                    }
                elif kind == "steer":
                    result = {"task": self.steer(selected, request["text"], owner=False)}
                elif kind == "pause":
                    self.pause(selected)
                    result = {"paused": selected}
                else:
                    self.resume(selected)
                    result = {"resumed": selected}
            elif kind == "memory_update":
                if agent != "astra":
                    raise PermissionError(
                        "Astra owns accepted memory; workers propose evidence to their parent"
                    )
                if (
                    type(request.get("expected_revision")) is not int
                    or request["expected_revision"] < 0
                ):
                    raise ValueError("expected_revision must be a nonnegative integer")
                with self.store.transaction():
                    self.approve_memory(agent, task, operation_id, request)
                    sources = self.context.memory_sources(task, operation_id, request["sources"])
                    receipt = self.context.memory(
                        request["topic"],
                        request["expected_revision"],
                        request["summary"],
                        request.get("detail", ""),
                        request["rationale"],
                        sources,
                        export=False,
                    )
                    result = {
                        **self.context.memory_get(request["topic"]),
                        **receipt,
                        "snapshot_effect": "Saved memory enters the frozen snapshot at the next compaction; this receipt only appends to current history.",
                    }
                    self.store.db.execute(
                        "UPDATE controls SET status='completed',result_hash=? WHERE id=?",
                        (self.store.put(result), control),
                    )
                self.memory_mirror(request["topic"], task)
                return result
            elif kind == "memory_record":
                with self.store.transaction():
                    self.approve_memory(agent, task, operation_id, request)
                    result = self.context.history.record(
                        task, operation_id, request["batch"], request["proposal"]
                    )
                    self.store.db.execute(
                        "UPDATE controls SET status='completed',result_hash=? WHERE id=?",
                        (self.store.put(result), control),
                    )
                return result
            elif kind == "compact":
                result = (
                    self.context.history.request_compaction(agent, task)
                    if self.context.history.enabled(agent)
                    else {"context_id": await self.context.compact(agent, task)}
                )
            elif kind == "outcome":
                status = request["status"]
                if status not in ("completed", "blocked", "cancelled"):
                    raise ValueError("Invalid task outcome")
                self.store.db.execute(
                    "INSERT OR REPLACE INTO task_outcomes VALUES(?,?,?,?)",
                    (
                        task,
                        status,
                        request.get("reason", ""),
                        self.store.put(request.get("evidence_refs", [])),
                    ),
                )
                result = {"outcome_recorded": status, "final_message_still_required": True}
            elif kind == "submit":
                result = self.workspaces.submit(task)
            elif kind in (
                "check_submission",
                "review_submission",
                "integrate_submission",
                "rollback_submission",
            ):
                if self.store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"] == "luna":
                    raise PermissionError(
                        "Luna submits candidates; Astra or Sol owns acceptance/integration"
                    )
                if kind == "check_submission":
                    selected = request.get("task", task)
                    self.check_tree(agent, selected)
                    result = await self.workspaces.validate(selected, request["command"])
                else:
                    release = request["release"]
                    owner = self.store.one(
                        "SELECT w.task_id FROM worktrees w JOIN release_worktrees r ON r.worktree_id=w.id WHERE r.release_id=?",
                        (release,),
                    )
                    if not owner:
                        raise ValueError("Unknown release ownership")
                    self.check_tree(agent, owner["task_id"])
                    if kind == "review_submission":
                        result = self.workspaces.review(
                            release, agent, request["expected_diff_sha256"], request["notes"]
                        )
                    elif kind == "integrate_submission":
                        result = self.workspaces.integrate(release)
                    else:
                        result = self.workspaces.rollback(release)
            else:
                raise ValueError("Unknown domain operation: " + str(kind))
            self.store.db.execute(
                "UPDATE controls SET status='completed',result_hash=? WHERE id=?",
                (self.store.put(result), control),
            )
            return result
        except Exception as exc:
            status = "uncertain" if isinstance(exc, UncertainEffect) else "failed"
            self.store.db.execute(
                "UPDATE controls SET status=?,result_hash=? WHERE id=?",
                (status, self.store.put({"error": clean(str(exc))}), control),
            )
            if (
                kind == "delegate"
                and "child_task" in locals()
                and self.store.one("SELECT 1 FROM tasks WHERE id=?", (child_task,))
            ):
                self.block(child_task, "Delegation setup failed: " + clean(str(exc)))
            raise

    def ingest_response(self, task, call, response):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        internal_retrieval = (
            self.recall.job(task) is not None or self.evidence.worker(task) is not None
        )
        has_tools = any(x.get("type") == "function_call" for x in response.get("output", []))
        with self.store.transaction():
            for position, envelope in enumerate(response.get("output", [])):
                item = self.store.item(
                    row["agent_id"], envelope, task, call, call + ":" + str(position)
                )
                if envelope.get("type") == "function_call":
                    ident = call + ":" + envelope["call_id"]
                    operation = "op_" + hashlib.sha256(ident.encode()).hexdigest()[:24]
                    error = None
                    try:
                        arguments = json.loads(envelope["arguments"])
                        if not isinstance(arguments, dict):
                            raise ValueError("Tool arguments must be a JSON object")
                        offered, known = progress.offered_schema(self.store, call, envelope["name"])
                        if known and offered is None:
                            raise ValueError(
                                "Tool was not offered in this model request; no operation executed"
                            )
                        if "progress" in arguments:
                            progress.validate(arguments["progress"])
                        elif offered and "progress" in offered.get("parameters", {}).get(
                            "required", []
                        ):
                            raise ValueError(
                                "Missing required public progress (observed, next, why); tools were not executed"
                            )
                        if "progress" in arguments or "background" in arguments:
                            detail = self.store.put(
                                {"call": call, "contract": "public-progress-v1"}
                            )
                            if not self.store.one(
                                "SELECT 1 FROM events WHERE kind='model.tool_contract' AND detail_hash=?",
                                (detail,),
                            ):
                                self.store.event(
                                    "model.tool_contract",
                                    self.store.get(detail),
                                    task,
                                    row["agent_id"],
                                )
                    except (ValueError, TypeError, jsonschema.ValidationError) as exc:
                        arguments = envelope["arguments"]
                        error = {
                            "error": "Invalid tool arguments: " + str(exc),
                            "not_executed": True,
                        }
                    digest = self.store.put(arguments)
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO tool_calls VALUES(?,?,?,?,?,?)",
                        (ident, item, task, envelope["name"], digest, operation),
                    )
                    owner_turn = turns.call_turn(self.store, call)
                    if owner_turn:
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO operation_turns VALUES(?,?)",
                            (operation, owner_turn),
                        )
                    if error:
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO operations(id,task_id,kind,name,status,intent_hash,result_hash,started_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (
                                operation,
                                task,
                                "tool",
                                envelope["name"],
                                "failed",
                                digest,
                                self.store.put(error),
                                now(),
                                now(),
                            ),
                        )
                        self.result_item(
                            self.store.one("SELECT * FROM tool_calls WHERE id=?", (ident,)),
                            error,
                            True,
                        )
                if envelope.get("type") == "message" and not has_tools:
                    text = "\n".join(p["text"] for p in envelope.get("content", []) if "text" in p)
                    if (
                        text
                        and not internal_retrieval
                        and (envelope.get("phase") == "commentary" or has_tools)
                    ):
                        self.delivery.queue(item, task, text)
            if has_tools:
                output = response["output"]
                indexes = [i for i, x in enumerate(output) if x.get("type") == "function_call"]
                prose = [(i, x) for i, x in enumerate(output) if x.get("type") == "message"]
                compliant = (
                    len(prose) == 1
                    and prose[0][1].get("phase") == "final_answer"
                    and prose[0][0] > max(indexes)
                )
                detail = {"call": call, "requested_shape": compliant}
                if not self.store.one(
                    "SELECT 1 FROM events WHERE kind='model.phase_policy' AND detail_hash=?",
                    (self.store.put(detail),),
                ):
                    self.store.event("model.phase_policy", detail, task, row["agent_id"])
                text = "Tools: " + ", ".join("`" + output[i]["name"] + "`" for i in indexes)
                summaries = [
                    "\n".join(p["text"] for p in x.get("content", []) if "text" in p)
                    for _, x in prose
                ]
                updates = []
                for i in indexes:
                    try:
                        update = progress.render(json.loads(output[i]["arguments"])["progress"])
                        if update not in updates:
                            updates.append(update)
                    except (KeyError, TypeError, ValueError, jsonschema.ValidationError):
                        continue
                if updates:
                    summaries = updates
                elif not any(summaries):
                    summaries = ["Public progress summary unavailable in this response."]
                if any(summaries):
                    text += "\n\n" + "\n\n".join(summaries)
                # This is a display composition; original envelopes/phases/order
                # above remain unchanged and are what the next model call sees.
                first = self.store.one(
                    "SELECT id FROM items WHERE source_key=?", (call + ":" + str(indexes[0]),)
                )
                if not internal_retrieval:
                    self.delivery.queue(first["id"], task, text)

    async def execute(self, task, call):
        attribution = self.store.one(
            "SELECT turn_id FROM operation_turns WHERE operation_id=?", (call["operation_id"],)
        )
        with turns.bind(attribution["turn_id"] if attribution else None):
            await self._execute(task, call)

    async def _execute(self, task, call):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        operation = call["operation_id"]
        old = self.store.one("SELECT * FROM operations WHERE id=?", (operation,))
        if (
            old
            and old["status"] == "running"
            and self.store.one("SELECT 1 FROM process_awaits WHERE operation_id=?", (operation,))
        ):
            self.store.status(task, "waiting")
            return
        if (
            old
            and old["status"] in ("uncertain", "running")
            and not (
                old["status"] == "running"
                and (
                    call["name"] == "gather_evidence"
                    or self.store.one(
                        "SELECT 1 FROM worker_awaits WHERE operation_id=?", (operation,)
                    )
                )
            )
        ):
            self.block(task, "Unresolved operation: " + operation)
            return
        if old and old["status"] in ("completed", "failed"):
            raw = self.store.get(old["result_hash"])
            failed = old["status"] == "failed"
        else:
            if row["status"] in ("paused", "cancelled", "blocked"):
                return
            self.store.db.execute(
                "INSERT OR IGNORE INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
                (operation, task, "tool", call["name"], "prepared", call["arguments_hash"], now()),
            )
            self.store.db.execute("UPDATE operations SET status='running' WHERE id=?", (operation,))
            operation_epoch = self.steering_epoch(task)
            try:
                raw = clean(
                    await self.interruptible(
                        self.tools.run(
                            row["agent_id"],
                            task,
                            operation,
                            call["name"],
                            self.store.get(call["arguments_hash"]),
                        ),
                        task,
                    )
                )
                failed = False
            except EvidencePending:
                self.store.status(task, "waiting")
                return
            except ProcessPending as pending:
                self.store.db.execute(
                    "INSERT INTO process_awaits VALUES(?,?,?) ON CONFLICT(operation_id) DO UPDATE SET process_id=excluded.process_id",
                    (operation, pending.process_id, now()),
                )
                state = self.store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"]
                if self.steering_epoch(task) != operation_epoch:
                    self.release_process_waits(task)
                    if state not in ("paused", "cancelled"):
                        self.store.status(task, "ready")
                elif state not in ("paused", "cancelled"):
                    self.store.status(task, "waiting")
                return
            except UncertainEffect as exc:
                self.store.db.execute(
                    "UPDATE operations SET status='uncertain',error=? WHERE id=?",
                    (str(exc), operation),
                )
                self.block(task, str(exc))
                return
            except InterruptedError as exc:
                launched = (
                    self.store.one("SELECT id FROM processes WHERE operation_id=?", (operation,))
                    if call["name"] == "start_process"
                    else None
                )
                accepted = self.store.one(
                    "SELECT c.result_hash FROM controls c JOIN operation_controls o ON o.control_id=c.id WHERE o.operation_id=? AND c.command IN ('memory_update','memory_record') AND c.status='completed'",
                    (operation,),
                )
                if launched:
                    raw = await self.tools.processes.observe(row["agent_id"], launched["id"])
                    raw["wait_interrupted"] = str(exc)
                    failed = False  # Launch acknowledged, job completion stays explicit.
                elif accepted:
                    raw, failed = self.store.get(accepted["result_hash"]), False
                elif call["name"] not in (
                    "read_file",
                    "view_image",
                    "read_artifact",
                    "observe_process",
                    "web_fetch",
                    "web_search",
                    "web_open",
                    "memory_search",
                    "memory_read",
                    "memory_get",
                    "memory_review",
                    "memory_update",
                    "memory_record",
                ):
                    self.store.db.execute(
                        "UPDATE operations SET status='uncertain',error=? WHERE id=?",
                        (str(exc), operation),
                    )
                    if (
                        self.store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"]
                        != "paused"
                    ):
                        self.store.status(task, "blocked", str(exc))
                    self.settle_pending(task)
                    return
                else:
                    raw, failed = {"interrupted": str(exc), "no_external_mutation": True}, True
            except asyncio.CancelledError:
                resumable = call["name"] == "observe_process" or (
                    call["name"] == "start_process"
                    and self.store.one("SELECT 1 FROM processes WHERE operation_id=?", (operation,))
                )
                resumable = (
                    resumable
                    or self.store.one(
                        "SELECT 1 FROM worker_awaits WHERE operation_id=?", (operation,)
                    )
                    or self.store.one(
                        "SELECT 1 FROM preset_invocations WHERE operation_id=?", (operation,)
                    )
                )
                resumable = resumable or self.store.one(
                    "SELECT 1 FROM worker_batches WHERE operation_id=?", (operation,)
                )
                resumable = resumable or self.store.one(
                    "SELECT 1 FROM task_assignments WHERE operation_id=?", (operation,)
                )
                resumable = resumable or call["name"] in (
                    "memory_get",
                    "memory_review",
                    "memory_update",
                    "memory_record",
                )
                self.store.db.execute(
                    "UPDATE operations SET status=?,error='Interrupted while performing tool effect' WHERE id=?",
                    ("prepared" if resumable else "uncertain", operation),
                )
                raise
            except Exception as exc:
                raw, failed = {"error": clean(str(exc)), "type": type(exc).__name__}, True
            self.store.db.execute(
                "UPDATE operations SET status=?,result_hash=?,completed_at=? WHERE id=?",
                ("failed" if failed else "completed", self.store.put(raw), now(), operation),
            )
        self.store.db.execute("DELETE FROM process_awaits WHERE operation_id=?", (operation,))
        digest = self.store.put(raw)
        try:
            view = await self.results.present(row["agent_id"], task, call, raw, digest)
        except Exception as exc:
            # Failure to derive a view must never dump the original web body or
            # base64 into working context. The acknowledged original remains.
            view = {
                "artifact": digest,
                "curation_error": clean(str(exc)),
                "instruction": "Original retained; inspect a scoped exact range using the source receipt.",
            }
        if isinstance(raw, dict) and ("_image" in raw or "_images" in raw):
            from .images import content
            from .research_views import ResearchViews

            images = ResearchViews(self.results).images(row["agent_id"], call, raw)
            output = content(
                self.store,
                {**view, **images, "receipt": {"operation_id": operation, "artifact_hash": digest}},
            )
        else:
            output = json.dumps(
                {"result": view, "receipt": {"operation_id": operation, "artifact_hash": digest}},
                ensure_ascii=False,
            )
        source = self.store.one("SELECT call_id FROM items WHERE id=?", (call["item_id"],))[
            "call_id"
        ]
        with self.store.transaction():
            item = self.store.item(
                row["agent_id"],
                {"type": "function_call_output", "call_id": source, "output": output},
                task,
                key=operation + ":result",
            )
            self.store.db.execute(
                "INSERT OR IGNORE INTO tool_results VALUES(?,?,?)", (call["id"], item, int(failed))
            )
            if not failed:
                self.workers.consume_batch(operation, item)

    def new_inputs(self, task, call, steering_only=False):
        # Output IDs are too late: owner input can arrive while inference runs.
        # The actual request membership is the decision's input boundary.
        boundary = self.store.one(
            "SELECT max(item_id) AS n FROM model_call_items WHERE call_id=?", (call,)
        )["n"]
        if boundary is None:
            raise RuntimeError(
                "Saved turn has no input membership; inspect its request before recovery"
            )
        return self.store.rows(
            "SELECT i.id FROM items i LEFT JOIN item_origins o ON o.item_id=i.id WHERE i.task_id=? AND i.role='user' AND i.id>? AND (?=0 OR o.kind IN ('steering','owner_steering')) AND coalesce(o.kind,'') NOT IN ('compaction','worker_background')",
            (task, boundary, int(steering_only)),
        )

    def defer_final(self, task, call):
        row = self.store.one("SELECT agent_id FROM tasks WHERE id=?", (task,))
        self.store.item(
            row["agent_id"],
            {
                "role": "user",
                "content": "Runtime notice: new input arrived after the previous request was prepared. Its final answer was retained but withheld from delivery. Reconsider it with the new input before completing this task.",
            },
            task,
            key=call + ":new-input",
            origin="runtime_notice",
        )
        self.store.status(task, "ready")

    async def step(self, task):
        with turns.bind(turns.current(self.store, task)):
            work = self._step(task)
            if self.recall.job(task):
                await self.recall.step(task, work)
            elif self.evidence.worker(task):
                await self.evidence.step(task, work)
            else:
                await work

    async def _step(self, task):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        agent = row["agent_id"]
        self.store.status(task, "running")
        # Ingest saved complete calls before buying a new inference.
        completed = self.store.rows(
            "SELECT * FROM model_calls WHERE task_id=? AND purpose='turn' AND status='completed' ORDER BY rowid",
            (task,),
        )
        for call in completed:
            self.ingest_response(task, call["id"], self.store.get(call["response_hash"]))
        if completed and self.new_inputs(task, completed[-1]["id"], steering_only=True):
            self.settle_pending(task)
        pending = self.store.rows(
            "SELECT c.* FROM tool_calls c LEFT JOIN tool_results r ON r.call_id=c.id WHERE c.task_id=? AND r.call_id IS NULL ORDER BY c.rowid",
            (task,),
        )
        if not pending:
            # If the last settled turn was final, finish it without repeating inference.
            if completed:
                last = self.store.get(completed[-1]["response_hash"])["output"]
                if not any(x.get("type") == "function_call" for x in last) and any(
                    x.get("phase") == "final_answer"
                    or (x.get("type") == "message" and not x.get("phase"))
                    for x in last
                ):
                    if not self.new_inputs(task, completed[-1]["id"]):
                        await self.finish(task)
                        return
                    self.defer_final(task, completed[-1]["id"])
            from . import reporting

            report_phase = reporting.phase(self.store, task)
            decision_limit = (
                report_phase["start_turn"] + reporting.MAX_CALLS
                if report_phase
                else row["max_turns"]
            )
            if row["turns"] >= decision_limit:
                if self.evidence.worker(task):
                    await self.evidence.stop(
                        task, "Configured turn ceiling reached; partial evidence retained"
                    )
                else:
                    self.block(task, "Configured turn ceiling reached; partial evidence retained")
                return
            steering_epoch = self.steering_epoch(task)
            try:
                if self.store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"] in (
                    "astra",
                    "sol",
                ):
                    self.workers.flush(task)
                else:
                    self.recall.refresh(task)
                    self.evidence.refresh(task)
                instructions, items = await self.interruptible(
                    self.context.compose(agent, task, self.tools.schemas(agent)), task
                )
                if self.recall.job(task) and self.context.count(
                    [instructions, items, self.tools.schemas(agent)]
                ) > self.config.data["memory"].get("retrieval_input_tokens", 256000):
                    raise RuntimeError(
                        "Memory retrieval input budget exceeded; original reads retained"
                    )
            except InterruptedError:
                if self.store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] not in (
                    "paused",
                    "cancelled",
                    "blocked",
                ):
                    self.store.status(task, "ready")
                return
            input_ids = list(self.context.members[agent])
            try:
                reporting_phase = self.store.one(
                    "SELECT 1 FROM worker_reporting WHERE task_id=?", (task,)
                )
                override = (
                    {"effort": self.config.data["runtime"].get("worker_report_effort", "medium")}
                    if reporting_phase
                    else None
                )
                call, response = await self.interruptible(
                    self.models.infer(
                        agent,
                        task,
                        instructions,
                        items,
                        self.tools.schemas(agent),
                        item_ids=input_ids,
                        profile_override=override,
                    ),
                    task,
                )
            except InterruptedError:
                if self.store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] not in (
                    "paused",
                    "cancelled",
                ):
                    self.store.status(task, "ready")
                return
            recorded = [
                r["item_id"]
                for r in self.store.rows(
                    "SELECT item_id FROM model_call_items WHERE call_id=? ORDER BY position",
                    (call,),
                )
            ]
            if recorded != input_ids:
                raise RuntimeError("Model adapter did not retain the actual request membership")
            self.ingest_response(task, call, response)
            self.store.db.execute("UPDATE tasks SET turns=turns+1 WHERE id=?", (task,))
            if self.steering_epoch(task) != steering_epoch:
                self.settle_pending(task)
                if self.store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] not in (
                    "paused",
                    "cancelled",
                ):
                    self.store.status(task, "ready")
                return
            if self.store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] in (
                "paused",
                "cancelled",
            ):
                self.settle_pending(task)
                return
            pending = self.store.rows(
                "SELECT c.* FROM tool_calls c JOIN items i ON i.id=c.item_id LEFT JOIN tool_results r ON r.call_id=c.id WHERE i.model_call_id=? AND r.call_id IS NULL ORDER BY c.rowid",
                (call,),
            )
            if not pending:
                output = response.get("output", [])
                if not any(x.get("type") == "function_call" for x in output) and any(
                    x.get("phase") == "final_answer"
                    or (x.get("type") == "message" and not x.get("phase"))
                    for x in output
                ):
                    self.workers.flush(task, required_only=True)
                    if self.new_inputs(task, call):
                        self.defer_final(task, call)
                    else:
                        await self.finish(task)
                else:
                    self.store.status(task, "ready")
                return
        # Parallelize only read operations; dependent mutations retain provider order.
        read_names = {
            "read_file",
            "read_artifact",
            "observe_process",
            "web_fetch",
            "memory_search",
            "memory_read",
            "memory_get",
        }
        batch = []
        dispatch_epoch = self.steering_epoch(task)
        for tool in pending:
            if self.steering_epoch(task) != dispatch_epoch:
                if batch:
                    await asyncio.gather(*(self.execute(task, c) for c in batch))
                self.settle_pending(task)
                if (
                    self.store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"]
                    == "running"
                ):
                    self.store.status(task, "ready")
                return
            if tool["name"] in read_names:
                batch.append(tool)
                if len(batch) < self.config.data["runtime"]["parallel_tools_per_agent"]:
                    continue
            if batch:
                await asyncio.gather(*(self.execute(task, c) for c in batch))
                batch = []
            if tool["name"] not in read_names:
                await self.execute(task, tool)
        if batch:
            await asyncio.gather(*(self.execute(task, c) for c in batch))
        if self.store.one("SELECT status FROM tasks WHERE id=?", (task,))["status"] == "running":
            self.store.status(task, "ready")
        await self.delivery.activity(task)

    async def finish(self, task):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        if row["status"] in ("paused", "cancelled"):
            return
        role = self.store.one("SELECT role FROM agents WHERE id=?", (row["agent_id"],))["role"]
        if role == "sol" and row["parent_id"]:
            final = self.store.one(
                "SELECT text,envelope_hash FROM items WHERE task_id=? AND role='assistant' AND kind='message' ORDER BY id DESC LIMIT 1",
                (task,),
            )
            if final and self.context.count(final["text"]) > self.config.data["runtime"].get(
                "handoff_tokens", 4000
            ):
                key = task + ":concise-handoff"
                if not self.store.one("SELECT 1 FROM items WHERE source_key=?", (key,)):
                    self.store.item(
                        row["agent_id"],
                        {
                            "role": "user",
                            "content": "Your complete report is retained as artifact "
                            + final["envelope_hash"]
                            + ". Return a concise final handoff within "
                            + str(self.config.data["runtime"].get("handoff_tokens", 4000))
                            + " tokens: findings, exact references/artifact paths, limitations and decisions for Astra. Do not repeat tools or source bodies; keep the detailed report retrievable.",
                        },
                        task,
                        key=key,
                        origin="runtime_notice",
                    )
                    self.store.status(task, "ready")
                    return
        if self.workers.flush(task, required_only=True):
            # A saved final can be recovered after restart while a report waits
            # in its durable inbox. Let the next decision consume that evidence.
            self.store.status(task, "ready")
            return
        children = self.store.rows("SELECT id,status FROM tasks WHERE parent_id=?", (task,))
        explicit_outcome = self.store.one("SELECT * FROM task_outcomes WHERE task_id=?", (task,))
        outcome = explicit_outcome or {"status": "completed", "reason": ""}
        if outcome["status"] == "completed" and self.workers.wait_for_reports(task):
            return
        if outcome["status"] == "completed" and any(
            c["status"] not in TERMINAL and self.workers.required(c["id"]) for c in children
        ):
            self.store.item(
                row["agent_id"],
                {
                    "role": "user",
                    "content": "Your child work is still active. Inspect its results and explicitly wait before final acceptance.",
                },
                task,
            )
            self.store.status(task, "ready")
            return
        processes = self.store.rows(
            "SELECT p.id,p.status FROM processes p JOIN operations o ON o.id=p.operation_id WHERE o.task_id=? AND p.status NOT IN ('completed','failed') ORDER BY p.id",
            (task,),
        )
        if processes and not explicit_outcome:
            # Starting a command acknowledges launch, not its result. In
            # particular, a fast command may finish just after start's snapshot.
            final_call = self.store.one(
                "SELECT model_call_id FROM items WHERE task_id=? AND role=? ORDER BY id DESC LIMIT 1",
                (task, "assistant"),
            )
            if not self.recall.job(task) and not self.evidence.worker(task):
                final = self.store.one(
                    "SELECT id,text FROM items WHERE task_id=? AND role='assistant' AND kind='message' ORDER BY id DESC LIMIT 1",
                    (task,),
                )
                if final and final["text"]:
                    self.delivery.queue(
                        final["id"],
                        task,
                        "Update · work is still in progress\n\n" + final["text"],
                        "progress",
                    )
            key = (
                task
                + ":unsettled-processes:"
                + str(final_call["model_call_id"] if final_call else row["turns"])
            )
            self.store.item(
                row["agent_id"],
                {
                    "role": "user",
                    "content": "Runtime notice: completion was deferred because these process handles have no observed terminal result: "
                    + json.dumps(processes)
                    + ". Use observe_process on the existing handles, inspect the output and finish the requested work; do not relaunch them. "
                    "Use an explicit wait if they are still running. If the requested outcome is an intentionally continuing service/job, "
                    "verify its startup, then call set_outcome(completed) with a reason explaining the handoff and report its monitoring/stop procedure. "
                    "A progress update alone does not complete the owner request.",
                },
                task,
                key=key,
                origin="runtime_notice",
            )
            self.store.status(task, "ready")
            return
        # Terminal state, owner closure and the card/prose outbox group commit
        # together. A crash cannot leave a terminal task with only half a group.
        with self.store.transaction():
            self.store.status(
                task,
                outcome["status"],
                outcome["reason"] or None if outcome["status"] != "completed" else None,
            )
            final = self.store.one(
                "SELECT id FROM items WHERE task_id=? AND role='assistant' AND kind='message' ORDER BY id DESC LIMIT 1",
                (task,),
            )
            self.store.db.execute(
                "UPDATE tasks SET final_item=? WHERE id=?", (final["id"] if final else None, task)
            )
            if self.evidence.worker(task) and self.evidence.retry_report(task):
                return
            if self.recall.job(task) or self.evidence.worker(task):
                self.notify_parent(task)
                return  # Internal evidence is appended to Astra; only activity JSON is public.
            if final:
                turns.close(self.store, task, final["id"])
            self.notify_parent(task)
            self.delivery.queue_final(task)

    def wake_waits(self):
        for wait in self.store.rows("SELECT * FROM waits"):
            children = self.store.rows(
                "SELECT t.id,t.status FROM tasks t JOIN wait_children w ON w.child_id=t.id WHERE w.task_id=?",
                (wait["task_id"],),
            )
            processes = self.store.rows(
                "SELECT p.id,p.status,p.exit_code FROM processes p JOIN wait_processes w ON w.process_id=p.id WHERE w.task_id=?",
                (wait["task_id"],),
            )
            conditions = self.store.one(
                "SELECT * FROM wait_conditions WHERE task_id=?", (wait["task_id"],)
            ) or {"owner_messages": 0, "not_before": 0, "after_event": 0}
            messages = self.store.one(
                "SELECT count(*) AS n FROM events WHERE id>? AND kind='owner.message'",
                (conditions["after_event"],),
            )["n"]
            declared = bool(
                children or processes or conditions["owner_messages"] or conditions["not_before"]
            )
            settled = (
                declared
                and all(c["status"] in TERMINAL for c in children)
                and all(p["status"] in ("completed", "failed", "uncertain") for p in processes)
                and messages >= conditions["owner_messages"]
                and time.time() >= conditions["not_before"]
            )
            if declared and not settled and time.time() >= wait["deadline"]:
                # A watchdog tick is not evidence that the dependency settled.
                # Keep observing locally instead of buying another model turn
                # to ask the worker/process for the same unfinished status.
                self.store.db.execute(
                    "UPDATE waits SET deadline=? WHERE task_id=?",
                    (time.time() + self.config.data["runtime"]["wait_seconds"], wait["task_id"]),
                )
                self.store.event(
                    "wait.guard_renewed",
                    {"children": children, "processes": processes, "owner_messages": messages},
                    wait["task_id"],
                )
                continue
            if settled or time.time() >= wait["deadline"]:
                task = self.store.one("SELECT * FROM tasks WHERE id=?", (wait["task_id"],))
                if task["status"] == "waiting":
                    self.store.item(
                        task["agent_id"],
                        {
                            "role": "user",
                            "content": "Wait settled: "
                            + json.dumps(
                                {
                                    "children": children,
                                    "processes": processes,
                                    "owner_messages": messages,
                                    "guard_fired": not settled,
                                }
                            ),
                        },
                        task["id"],
                    )
                    self.store.status(task["id"], "ready")
                self.store.db.execute("DELETE FROM waits WHERE task_id=?", (wait["task_id"],))

    async def inbox(self):
        drain_inbox(self)

    async def memory_maintenance(self):
        history = self.context.history
        task = self.store.one(
            "SELECT id FROM tasks WHERE agent_id='astra' ORDER BY created_at DESC LIMIT 1"
        )
        if not task:
            return {"skipped": "no_astra_task"}
        rows = self.context.selected("astra", task["id"])
        packet_ids = {
            r["item_id"]
            for r in self.store.rows("SELECT item_id FROM memory_packets WHERE agent_id='astra'")
        }
        tail = [r for r in rows if r["id"] not in packet_ids]
        tokens = self.context.count([self.store.get(r["envelope_hash"]) for r in tail])
        # Gate the entire idle pass, including paid consolidation. A frozen
        # memory packet must not make a just-compacted history eligible again.
        due = (
            self.store.one("SELECT * FROM semantic_upkeep_due WHERE agent_id='astra'")
            if self.config.data["memory"].get("semantic_upkeep_enabled", True)
            else None
        )
        if tokens <= self.config.data["memory"]["context_tokens"] and not due:
            return {"skipped": "below_idle_token_threshold", "tail_tokens": tokens}
        if history.idle_due():
            from .semantic_upkeep import run as semantic_upkeep

            upkeep = await semantic_upkeep(self, task["id"])
            if not history.idle_due():
                return {"skipped": "new_work_preempted_compaction", "upkeep": upkeep}
            if (
                upkeep["published"]
                and upkeep.get("pending_fragments")
                and not upkeep.get("errors")
                and not upkeep.get("declined")
            ):
                self.store.db.execute(
                    "INSERT INTO semantic_upkeep_due VALUES(?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET watermark=max(watermark,excluded.watermark),eligible_tokens=max(eligible_tokens,excluded.eligible_tokens)",
                    ("astra", history.watermark("astra"), tokens, now()),
                )
                return {"skipped": "semantic_backlog_in_progress", "upkeep": upkeep}
            if tokens <= self.config.data["memory"]["context_tokens"] and not upkeep["published"]:
                if upkeep.get("pending_fragments") == 0:
                    self.store.db.execute("DELETE FROM semantic_upkeep_due WHERE agent_id='astra'")
                return {"skipped": "semantic_upkeep_deferred", "upkeep": upkeep}
            context_id = await self.context.compact("astra", task["id"], rows)
            if not history.pending("astra", task["id"]):
                self.store.db.execute("DELETE FROM semantic_upkeep_due WHERE agent_id='astra'")
            return {
                "context_id": context_id,
                "permanent_memory_changed": bool(upkeep["published"]),
                "upkeep": upkeep,
            }
        return {"skipped": "new_work_preempted_compaction"}

    async def maintain_memory(self):
        """Called by the coordinator; owner work always preempts idle inference."""
        history = self.context.history
        if self.memory_job and self.memory_job.done():
            try:
                self.memory_job.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.store.event(
                    "memory.maintenance_failed", {"error": clean(str(exc))[:1000]}, agent="astra"
                )
                self.memory_attempt_at = time.time() + 1740  # Thirty-minute failure backoff.
            self.memory_job = None
        if self.memory_job and not history.idle_due():
            self.memory_job.cancel()
            await asyncio.gather(self.memory_job, return_exceptions=True)
            self.memory_job = None
        if (
            not self.memory_job
            and not self.active
            and time.time() - self.memory_attempt_at >= 60
            and history.idle_due()
        ):
            self.memory_attempt_at = time.time()
            # Keep indexing/archival-read failures inside the supervised idle
            # task; they must not take down the coordinator's main loop.
            self.memory_job = asyncio.create_task(self.memory_maintenance())

    def receive_discord(self, message):
        self.delivery.acknowledge(message)
        settings = self.config.data["discord"]
        if message.get("author", {}).get("id") != settings["owner_id"]:
            return
        addresses = {settings["channel_id"]} | {
            r["external_id"]
            for r in self.store.rows("SELECT external_id FROM channel_routes WHERE status='ready'")
        }
        if message.get("channel_id") not in addresses:
            return
        if self.store.one(
            "SELECT 1 FROM inbound_messages WHERE channel='discord' AND external_id=?",
            (message["id"],),
        ):
            return
        text = message.get("content", "")
        attachments = [
            {"name": a.get("filename"), "url": a["url"], "content_type": a.get("content_type")}
            for a in message.get("attachments", [])
        ]
        if attachments:
            text += "\nOwner attachments: " + json.dumps(attachments)
        if not text:
            return
        task = None
        with self.store.transaction():
            command = text.strip().lower()
            if command in ("pause", "resume"):
                states = (
                    ("ready", "running", "waiting") if command == "pause" else ("paused", "blocked")
                )
                placeholders = ",".join("?" for _ in states)
                for root in self.store.rows(
                    f"SELECT id FROM tasks WHERE agent_id='astra' AND status IN ({placeholders})",
                    states,
                ):
                    (self.pause if command == "pause" else self.resume)(root["id"])
            else:
                ongoing = self.store.one(
                    "SELECT * FROM tasks WHERE agent_id='astra' AND status IN ('running','ready','waiting') ORDER BY created_at LIMIT 1"
                )
                mentioned = any(
                    m.get("id") == self.delivery.discord.bot_id for m in message.get("mentions", [])
                )
                if ongoing and mentioned:
                    task = self.steer(ongoing["id"], text)
                else:
                    queued = self.store.one(
                        "SELECT * FROM tasks WHERE agent_id='astra' AND channel='discord' AND address=? AND status IN ('ready','running','waiting') ORDER BY created_at LIMIT 1",
                        (message["channel_id"],),
                    )
                    if queued:
                        task = queued["id"]
                        self.store.item(
                            "astra",
                            {"role": "user", "content": text},
                            task,
                            key="discord-message:" + message["id"],
                            origin="owner",
                        )
                        self.store.event(
                            "owner.message",
                            {"source": "discord", "message": message["id"]},
                            task,
                            "astra",
                        )
                    else:
                        task = self.store.new_task(
                            text,
                            channel="discord",
                            address=message["channel_id"],
                            key="discord_" + message["id"],
                        )
            self.store.db.execute(
                "INSERT INTO inbound_messages VALUES(?,?,?,?,?,?)",
                (
                    "discord",
                    message["id"],
                    message["channel_id"],
                    self.store.put(clean(message)),
                    task,
                    now(),
                ),
            )

    async def run(self, until_idle=False):
        self.recover()
        gateway = (
            Gateway(self.store, self.delivery.discord, self.receive_discord, catch_up=True)
            if self.config.data["discord"]["enabled"]
            else None
        )
        identity = process_identity(os.getpid())
        started_at = time.time()

        def heartbeat(status="running"):
            atomic(
                self.config.workspace / "runtime.json",
                encoded(
                    {
                        "pid": os.getpid(),
                        "start_identity": identity,
                        "release": os.environ.get("BLOOM_RELEASE_ID"),
                        "source": str(self.config.source),
                        "version": __version__,
                        "status": status,
                        "runtime_started_at": started_at,
                        "updated_at": time.time(),
                        "discord": "disabled" if gateway is None else gateway.status,
                        "active_tasks": list(self.active),
                    }
                ),
            )

        async def health_loop():
            while not self.stopping:
                heartbeat()
                await asyncio.sleep(5)

        async def delivery_loop():
            while not self.stopping:
                try:
                    await self.delivery.flush()
                    await self.delivery.typing()
                except Exception as exc:
                    self.store.event("delivery.loop_error", {"error": clean(str(exc))[:1000]})
                    await asyncio.sleep(2)
                await asyncio.sleep(0.2)

        async def discord_loop():
            await gateway.run()

        async def process_loop():
            while not self.stopping:
                for row in self.store.rows(
                    "SELECT id FROM processes WHERE status IN ('prepared','running')"
                ):
                    try:
                        await asyncio.wait_for(self.tools.processes._observe(row["id"]), 15)
                    except Exception as exc:
                        self.store.event(
                            "process.observation_error",
                            {"process": row["id"], "error": clean(str(exc))[:500]},
                        )
                await asyncio.sleep(2)

        async def call_view_loop():
            from .call_views import refresh

            while not self.stopping:
                job = asyncio.create_task(asyncio.to_thread(refresh, self.config))
                try:
                    await asyncio.shield(job)
                except asyncio.CancelledError:
                    await job  # Finish local publication before closing runtime.
                    raise
                except Exception as exc:
                    self.store.event("call_views.error", {"error": clean(str(exc))[:500]})
                await asyncio.sleep(60)

        background = [
            asyncio.create_task(delivery_loop()),
            asyncio.create_task(process_loop()),
            asyncio.create_task(health_loop()),
            asyncio.create_task(call_view_loop()),
        ]
        if self.config.data["discord"]["enabled"]:
            background.append(asyncio.create_task(discord_loop()))
        try:
            while not self.stopping:
                await self.inbox()
                self.recall.poll()
                await self.evidence.poll()
                self.workers.poll()
                self.wake_processes()
                self.wake_waits()
                await self.maintain_memory()
                for task, job in list(self.active.items()):
                    if job.done():
                        del self.active[task]
                        try:
                            job.result()
                        except asyncio.CancelledError:
                            pass
                        except Exception as exc:
                            self.block(task, clean(str(exc))[:2000])
                            self.store.event("task.error", {"error": clean(str(exc))[:2000]}, task)
                busy = {
                    self.store.one("SELECT agent_id FROM tasks WHERE id=?", (t,))["agent_id"]
                    for t in self.active
                }
                busy.update(
                    r["agent_id"]
                    for r in self.store.rows(
                        "SELECT DISTINCT agent_id FROM tasks WHERE status='waiting'"
                    )
                )
                ready = self.store.rows(
                    "SELECT * FROM tasks WHERE status='ready' ORDER BY CASE WHEN agent_id='astra' THEN 0 ELSE 1 END,created_at"
                )
                for task in ready:
                    if len(self.active) >= self.config.data["runtime"]["max_agents"]:
                        break
                    if task["agent_id"] not in busy and task["id"] not in self.active:
                        self.active[task["id"]] = asyncio.create_task(self.step(task["id"]))
                        busy.add(task["agent_id"])
                if (
                    until_idle
                    and not self.active
                    and not self.store.one(
                        "SELECT id FROM tasks WHERE status IN ('ready','running','waiting') LIMIT 1"
                    )
                ):
                    # Local result recording is immediate. Network sends retain their
                    # pending/uncertain receipts if the caller chooses to stop now.
                    await self.delivery.flush()
                    break
                await asyncio.sleep(0.1)
        finally:
            if self.memory_job:
                self.memory_job.cancel()
                await asyncio.gather(self.memory_job, return_exceptions=True)
            for job in self.active.values():
                job.cancel()
            await asyncio.gather(*self.active.values(), return_exceptions=True)
            for job in background:
                job.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            heartbeat("stopped")
