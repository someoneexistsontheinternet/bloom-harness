"""Astra assigns substantive work to Sol without control-file round trips."""

from __future__ import annotations

import json

from . import progress
from .store import now


class Assignments:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    async def run(self, agent, task, operation, request):
        if agent != "astra":
            raise PermissionError("Astra assigns Sol work; Sol uses scoped Luna evidence workers")
        if not isinstance(request.get("objective"), str) or not request["objective"].strip():
            raise ValueError("Give Sol a concrete objective and acceptance criteria")
        if type(request.get("background", False)) is not bool:
            raise ValueError("background must be a Boolean")
        digest = self.store.put(request)
        row = self.store.one("SELECT * FROM task_assignments WHERE operation_id=?", (operation,))
        if row and (row["request_hash"] != digest or row["parent_task"] != task):
            raise ValueError("Sol assignment identity changed")
        if not row:
            parent = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
            owner = self.runtime.recall.owner(task)
            receipts = self.store.rows(
                "SELECT id,name,status,result_hash FROM operations WHERE task_id=? AND result_hash IS NOT NULL ORDER BY rowid",
                (task,),
            )
            evidence = self.store.put(
                {
                    "parent_task": task,
                    "receipts": receipts,
                    "instruction": "Retained evidence from this assignment. Reuse exact captures/results before repeating work; never replay historical operations.",
                }
            )
            payload = {
                "owner_question": owner["text"] if owner else parent["objective"],
                "assignment": request["objective"],
                "acceptance": request.get("criteria", ""),
                "sources": request.get("sources", []),
                "retained_evidence_index": evidence,
                "public_progress": progress.recent(self.store, task),
                "handoff": "Own the research/implementation, use Luna for scoped evidence, and return a concise evidence-backed final with artifacts, limitations and unresolved decisions. Astra retains interpretation and owner acceptance.",
            }
            delegate = {
                "operation": "delegate",
                "role": "sol",
                "objective": request["objective"],
                "cwd": request.get("cwd") or self.store.config.root.as_posix(),
                "network": True,
                "mode": "full",
            }
            if request.get("repository"):
                delegate["repository"] = request["repository"]
            with self.store.transaction():
                result = await self.runtime.request(agent, task, operation + ":sol", delegate)
                from . import evidence_grants

                evidence_grants.parent_task(self.store, agent, result["agent_id"], task, evidence)
                evidence_grants.sources(
                    self.store, agent, result["agent_id"], task, request.get("sources", [])
                )
                self.store.item(
                    result["agent_id"],
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    result["task_id"],
                    key=operation + ":context",
                    origin="delegation",
                    source_task=task,
                )
                self.store.db.execute(
                    "INSERT INTO task_assignments VALUES(?,?,?,?,?,?)",
                    (
                        operation,
                        task,
                        result["task_id"],
                        digest,
                        int(request.get("background", False)),
                        now(),
                    ),
                )
            row = self.store.one(
                "SELECT * FROM task_assignments WHERE operation_id=?", (operation,)
            )
        if row["background"]:
            return {
                "task_id": row["child_task"],
                "background": True,
                "instruction": "Continue independent work; await_workers joins this exact Sol task without polling.",
            }
        self.runtime.workers.await_results(agent, task, operation, [row["child_task"]])
        return self.report(row["child_task"])

    def report(self, task):
        child = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        final = self.store.one(
            "SELECT id,text,envelope_hash FROM items WHERE task_id=? AND role='assistant' AND kind='message' ORDER BY id DESC LIMIT 1",
            (task,),
        )
        summary = final["text"] if final else child["error"] or "No completed report."
        budget = self.store.config.data["runtime"].get("handoff_tokens", 4000)
        if self.runtime.context.count(summary) > budget:
            summary = "Full worker report retained; exceeds the compact handoff budget. Inspect the report artifact for exact sections before acceptance."
        return {
            "task_id": task,
            "status": child["status"],
            "summary": summary,
            "report_item": final["id"] if final else None,
            "report_artifact": final["envelope_hash"] if final else None,
            "limitation": child["error"],
            "instruction": "Candidate report. Astra owns reasoning and final acceptance; original task/tool receipts remain addressable.",
        }
