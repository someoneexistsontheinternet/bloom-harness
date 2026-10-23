"""Explicit worker launches and durable joins, without extra coordination inference."""

from __future__ import annotations

import json
import hashlib
import time

from . import turns
from .evidence import EvidencePending
from .store import now


TERMINAL = ("completed", "blocked", "cancelled")


class Workers:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def required(self, task):
        row = self.store.one("SELECT required FROM worker_dependencies WHERE task_id=?", (task,))
        return row is None or bool(row["required"])  # Legacy/implementation work stays required.

    def deliverable(self, source_task, owner_item, child, target_task, required_only=False):
        owner = self.runtime.recall.owner(source_task)
        if not owner or owner["id"] != owner_item:
            return False  # Owner steering supersedes this assignment, not its retained evidence.
        required = self.required(child)
        if required_only and not required:
            return False
        if source_task == target_task:
            return True
        parent = self.store.one("SELECT agent_id,status FROM tasks WHERE id=?", (source_task,))
        target = self.store.one("SELECT agent_id FROM tasks WHERE id=?", (target_task,))
        return (
            not required
            and parent["agent_id"] == "astra"
            and parent["status"] == "completed"
            and target["agent_id"] == parent["agent_id"]
        )

    def gather(self, agent, task, operation, plan, assignments, background=False):
        if (
            self.store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"]
            not in ("astra", "sol")
            or not isinstance(plan, str)
            or not plan.strip()
        ):
            raise ValueError("Astra or Sol must supply the evidence plan")
        if (
            type(background) is not bool
            or not 1 <= len(assignments) <= self.store.config.data["runtime"]["evidence_workers"]
        ):
            raise ValueError(
                "Use a Boolean background mode and one to three scoped preset assignments"
            )
        if any(
            "path" not in a or set(a) - {"path", "bindings", "expected_sha256", "required"}
            for a in assignments
        ):
            raise ValueError(
                "A preset batch needs path/bindings entries; do not mix legacy question/cwd assignments"
            )
        request = {"plan": plan, "assignments": assignments, "background": background}
        digest = self.store.put(request)
        batch = self.store.one("SELECT * FROM worker_batches WHERE operation_id=?", (operation,))
        if batch:
            if batch["parent_task"] != task or batch["request_hash"] != digest:
                raise ValueError("Worker batch identity changed")
            launches = json.loads(batch["launches_json"])
        else:
            owner = self.runtime.recall.owner(task)
            if not owner:
                raise ValueError("Worker batch requires an owner request")
            launches = [
                "op_" + hashlib.sha256((operation + ":preset:" + str(i)).encode()).hexdigest()[:24]
                for i in range(len(assignments))
            ]
            # Validate/capture every authoring input before publishing any work.
            with self.store.transaction():
                for child_op, args in zip(launches, assignments):
                    self.store.db.execute(
                        "INSERT INTO operations(id,task_id,kind,name,status,intent_hash,started_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            child_op,
                            task,
                            "worker_launch",
                            "apply_request",
                            "prepared",
                            self.store.put(args),
                            now(),
                        ),
                    )
                    captured = self.runtime.tools.author_request(
                        agent, task, child_op, args, preset_only=True
                    )
                    if captured["operation"] != "launch_worker":
                        raise ValueError(
                            "gather_evidence accepts Luna presets; use delegate_task or apply_request for Sol"
                        )
                    owner_turn = turns.current(self.store, task)
                    if owner_turn:
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO operation_turns VALUES(?,?)",
                            (child_op, owner_turn),
                        )
                self.store.db.execute(
                    "INSERT INTO worker_batches VALUES(?,?,?,?,?,?)",
                    (operation, task, owner["id"], digest, json.dumps(launches), now()),
                )
            batch = self.store.one(
                "SELECT * FROM worker_batches WHERE operation_id=?", (operation,)
            )
        owner = self.runtime.recall.owner(task)
        unpublished = any(
            not self.store.one("SELECT 1 FROM worker_launches WHERE operation_id=?", (op,))
            for op in launches
        )
        if unpublished and (not owner or owner["id"] != batch["owner_item"]):
            return {
                "superseded": True,
                "reports": [],
                "instruction": "Owner steering superseded the captured batch before launch; no new workers started.",
            }
        workers = []
        # Publication is purely local/transactional, with no awaited I/O. A crash
        # leaves either all captured tasks or none, and stable IDs permit recovery.
        with self.store.transaction():
            for child_op in launches:
                receipt = self.store.one(
                    "SELECT * FROM preset_invocations WHERE operation_id=?", (child_op,)
                )
                result = self.launch(agent, task, child_op, self.store.get(receipt["request_hash"]))
                workers.extend(result["launched"])
                self.store.db.execute(
                    "UPDATE operations SET status='completed',result_hash=?,completed_at=? WHERE id=?",
                    (self.store.put(result), now(), child_op),
                )
        if background:
            return {
                "launched": workers,
                "background": True,
                "instruction": "Continue identified independent work; await required results before final acceptance.",
            }
        joined = self.await_results(agent, task, operation, workers)
        reports = []
        for child_op in launches:
            launch = self.store.one(
                "SELECT * FROM worker_launches WHERE operation_id=?", (child_op,)
            )
            child = json.loads(launch["workers_json"])[0]
            if launch["kind"] == "recall":
                self.runtime.recall.publish(child)
                digest = self.runtime.recall.job(child)["report_hash"]
            else:
                self.stage_evidence(child)
                digest = self.store.one(
                    "SELECT report_hash FROM worker_launches WHERE operation_id=?", (child_op,)
                )["report_hash"]
            reports.append(
                self.store.get(digest)
                if digest
                else {
                    "status": "unavailable",
                    "summary": "Assignment was superseded; original receipts remain retained.",
                }
            )
        return {
            "plan": plan,
            "workers": joined["workers"],
            "reports": reports,
            "instruction": "Requested evidence is complete. Assess the sources, reason and answer; no separate collection/await call is needed.",
        }

    def consume_batch(self, operation, item):
        batch = self.store.one("SELECT * FROM worker_batches WHERE operation_id=?", (operation,))
        if not batch or self.store.get(batch["request_hash"])["background"]:
            return
        for child_op in json.loads(batch["launches_json"]):
            launch = self.store.one(
                "SELECT * FROM worker_launches WHERE operation_id=?", (child_op,)
            )
            if not launch or not launch["report_hash"]:
                continue
            self.store.db.execute(
                "UPDATE worker_launches SET report_item=? WHERE operation_id=?", (item, child_op)
            )
            if launch["kind"] == "recall":
                self.store.db.execute(
                    "UPDATE memory_retrievals SET report_item=? WHERE task_id=?",
                    (item, json.loads(launch["workers_json"])[0]),
                )

    def launch(self, agent, task, operation, request):
        if self.store.one("SELECT role FROM agents WHERE id=?", (agent,))["role"] not in (
            "astra",
            "sol",
        ):
            raise PermissionError("Astra or Sol selects scoped Luna assignments")
        kind = request["worker_kind"]
        if kind == "recall" and agent != "astra":
            raise PermissionError(
                "Astra owns global memory recall; Sol may inspect granted task evidence"
            )
        if kind not in ("recall", "evidence") or any(
            not isinstance(request.get(k), str) or not request[k].strip()
            for k in ("question", "objective")
        ):
            raise ValueError("Worker needs a supported kind, question and objective")
        if kind == "evidence" and (
            not isinstance(request.get("cwd"), str) or not request["cwd"].strip()
        ):
            raise ValueError("File evidence requires an explicit cwd")
        if type(request.get("required", True)) is not bool:
            raise ValueError("required must be a Boolean completion dependency")
        owner = self.runtime.recall.owner(task)
        if not owner:
            raise ValueError("Explicit worker requires a current owner message")
        digest = self.store.put(request)
        with self.store.transaction():
            old = self.store.one("SELECT * FROM worker_launches WHERE operation_id=?", (operation,))
            if old:
                if old["parent_task"] != task or old["request_hash"] != digest:
                    raise ValueError("Worker launch identity belongs to different input")
                workers = json.loads(old["workers_json"])
            else:
                if kind == "recall":
                    workers = [
                        self.runtime.recall.start(
                            task, operation, request["question"], request["objective"]
                        )
                    ]
                else:
                    self.runtime.evidence.start(
                        agent,
                        task,
                        operation,
                        request["question"],
                        [
                            {
                                "question": request["objective"],
                                "cwd": request.get("cwd", ""),
                                "sources": request.get("sources", []),
                            }
                        ],
                    )
                    workers = [r["id"] for r in self.runtime.evidence.children(operation)]
                self.store.db.execute(
                    "INSERT INTO worker_launches VALUES(?,?,?,?,?,?,NULL,NULL,0,?)",
                    (operation, task, owner["id"], kind, digest, json.dumps(workers), now()),
                )
                self.store.db.executemany(
                    "INSERT INTO worker_dependencies VALUES(?,?)",
                    [(child, int(request.get("required", True))) for child in workers],
                )
                if kind == "recall":
                    self.store.db.execute(
                        "UPDATE worker_launches SET report_hash=? WHERE operation_id=?",
                        (self.runtime.recall.job(workers[0])["report_hash"], operation),
                    )
        return {
            "launched": workers,
            "kind": kind,
            "required": request.get("required", True),
            "instruction": "Workers run in parallel. Continue independent work; emit await_workers last in this same batch if their results are needed next. Do not poll.",
        }

    def await_results(self, agent, task, operation, selected=None):
        if self.store.one("SELECT agent_id FROM tasks WHERE id=?", (task,))["agent_id"] != agent:
            raise PermissionError("Only the assigned parent joins these workers")
        with self.store.transaction():
            old = self.store.one("SELECT * FROM worker_awaits WHERE operation_id=?", (operation,))
            if old:
                if old["parent_task"] != task or (
                    selected and selected != json.loads(old["workers_json"])
                ):
                    raise ValueError("Wait identity changed")
                selected = json.loads(old["workers_json"])
            else:
                if selected is None:
                    turn = turns.current(self.store, task)
                    selected = [
                        r["id"]
                        for r in self.store.rows(
                            "SELECT t.id FROM tasks t JOIN task_owner_turns a ON a.task_id=t.id WHERE t.parent_id=? AND a.turn_id=? ORDER BY t.created_at,t.id",
                            (task, turn),
                        )
                    ]
                if any(
                    not self.store.one(
                        "SELECT 1 FROM tasks WHERE id=? AND parent_id=?", (child, task)
                    )
                    for child in selected
                ):
                    raise PermissionError("Can only join accepted children of this task")
                self.store.db.execute(
                    "INSERT INTO worker_awaits VALUES(?,?,?,?)",
                    (operation, task, json.dumps(selected), now()),
                )
        rows = [
            self.store.one("SELECT id,status,error FROM tasks WHERE id=?", (child,))
            for child in selected
        ]
        if any(r["status"] not in TERMINAL for r in rows):
            raise EvidencePending()
        return {
            "workers": rows,
            "instruction": "Workers settled. Validated reports append at the settled tool boundary; inspect them and reason about the answer.",
        }

    def poll(self):
        for wait in self.store.rows(
            "SELECT w.*,t.status FROM worker_awaits w JOIN tasks t ON t.id=w.parent_task JOIN operations o ON o.id=w.operation_id WHERE o.status IN ('prepared','running')"
        ):
            if wait["status"] in (*TERMINAL, "paused"):
                continue
            if all(
                self.store.one("SELECT status FROM tasks WHERE id=?", (child,))["status"]
                in TERMINAL
                for child in json.loads(wait["workers_json"])
            ):
                self.store.db.execute(
                    "UPDATE operations SET status='prepared' WHERE id=?", (wait["operation_id"],)
                )
                if wait["status"] == "waiting":
                    self.store.status(wait["parent_task"], "ready")

    def stage_evidence(self, child):
        launch = self.store.one(
            "SELECT l.* FROM worker_launches l JOIN evidence_workers w ON w.operation_id=l.operation_id WHERE w.task_id=?",
            (child,),
        )
        if not launch or launch["report_hash"]:
            return
        children = self.runtime.evidence.children(launch["operation_id"])
        if any(c["status"] not in TERMINAL for c in children):
            return
        report = {
            "kind": "worker_evidence_report",
            "launch": launch["operation_id"],
            "source_task": launch["parent_task"],
            "for_owner_item": launch["owner_item"],
            "reports": [self.runtime.evidence.report(c) for c in children],
            "instruction": "Candidate sourced evidence. Astra owns interpretation; history does not authorize replay.",
        }
        self.store.db.execute(
            "UPDATE worker_launches SET report_hash=? WHERE operation_id=?",
            (self.store.put(report), launch["operation_id"]),
        )

    def flush(self, task, required_only=False):
        applied = self.runtime.recall.flush(task, required_only=required_only)
        for launch in self.store.rows(
            "SELECT * FROM worker_launches WHERE kind='recall' AND report_item IS NULL AND obsolete=0"
        ):
            job = self.runtime.recall.job(json.loads(launch["workers_json"])[0])
            if job["report_hash"]:
                self.store.db.execute(
                    "UPDATE worker_launches SET report_hash=?,report_item=? WHERE operation_id=?",
                    (job["report_hash"], job["report_item"], launch["operation_id"]),
                )
        if self.store.one(
            "SELECT 1 FROM tool_calls c LEFT JOIN tool_results r ON r.call_id=c.id WHERE c.task_id=? AND r.call_id IS NULL",
            (task,),
        ):
            return applied
        if self.store.one(
            "SELECT 1 FROM model_calls WHERE task_id=? AND status IN ('prepared','running','uncertain')",
            (task,),
        ):
            return applied
        for launch in self.store.rows(
            "SELECT * FROM worker_launches WHERE kind='evidence' AND report_item IS NULL AND obsolete=0 ORDER BY created_at"
        ):
            child = json.loads(launch["workers_json"])[0]
            if not self.deliverable(
                launch["parent_task"], launch["owner_item"], child, task, required_only
            ):
                continue
            children = self.runtime.evidence.children(launch["operation_id"])
            if any(c["status"] not in TERMINAL for c in children):
                continue
            self.stage_evidence(child)
            staged = self.store.one(
                "SELECT report_hash FROM worker_launches WHERE operation_id=?",
                (launch["operation_id"],),
            )
            report = self.store.get(staged["report_hash"])
            with self.store.transaction():
                recipient = self.store.one("SELECT agent_id FROM tasks WHERE id=?", (task,))[
                    "agent_id"
                ]
                item = self.store.item(
                    recipient,
                    {"role": "user", "content": json.dumps(report, ensure_ascii=False)},
                    task,
                    key=launch["operation_id"] + ":worker-report",
                    origin="worker_evidence" if self.required(child) else "worker_background",
                    source_task=launch["parent_task"],
                )
                self.store.db.execute(
                    "UPDATE worker_launches SET report_hash=?,report_item=? WHERE operation_id=?",
                    (self.store.put(report), item, launch["operation_id"]),
                )
                applied.append(item)
        return applied

    def wait_for_reports(self, task):
        rows = self.store.rows(
            "SELECT t.id FROM tasks t WHERE t.parent_id=? AND t.status NOT IN ('completed','blocked','cancelled')",
            (task,),
        )
        rows = [row for row in rows if self.required(row["id"])]
        if not rows:
            return False
        with self.store.transaction():
            self.store.db.execute(
                "INSERT OR REPLACE INTO waits VALUES(?,?)",
                (task, time.time() + self.store.config.data["runtime"].get("wait_seconds", 600)),
            )
            self.store.db.executemany(
                "INSERT OR IGNORE INTO wait_children VALUES(?,?)", [(task, r["id"]) for r in rows]
            )
            self.store.status(task, "waiting")
        return True
