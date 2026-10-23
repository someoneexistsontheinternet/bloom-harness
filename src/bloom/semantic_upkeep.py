"""Idle semantic upkeep authored and explicitly approved by the real Astra model."""

from __future__ import annotations

import asyncio
import json

from .diagnostics import label
from .memory import MemoryInterrupted, PROMPT
from .store import now


INSTRUCTIONS = """You are Astra reviewing your retained history at a deliberate memory boundary.
Author the episode/event timeline and only genuinely durable state/persona changes
that you approve. Preserve motivation, dates, attempts, observations, decisions,
owner corrections, unresolved work and exact sources. Ordinary research findings
belong in the episode, not a permanent active fact. Existing accepted facts are
not new evidence; unchanged facts need no update. Never invent a quotation or
replace an explicit owner preference with an inference. This is not new owner
work and does not authorize external effects. Return JSON only:
{"approve":true,"batch":"the supplied batch ID","proposal":{...}}.
Approval applies to exactly this proposal. If you cannot approve it, return
{"approve":false,"batch":"the supplied batch ID","reason":"specific obstacle"}.
No Luna or Sol is authorized to approve this publication.
"""


def publish(memory, batch, call, decision):
    store = memory.store
    receipt = store.one("SELECT * FROM model_calls WHERE id=?", (call,))
    if (
        not receipt
        or receipt["agent_id"] != "astra"
        or receipt["model_requested"] != store.config.profile("astra")["model"]
        or receipt["purpose"] != "memory_upkeep"
        or receipt["status"] != "completed"
    ):
        raise PermissionError("Semantic upkeep requires a completed real Astra approval")
    response = store.get(receipt["response_hash"])
    text = "\n".join(
        p["text"]
        for x in response.get("output", [])
        if x.get("type") == "message"
        for p in x.get("content", [])
        if "text" in p
    )
    if (
        json.loads(text) != decision
        or decision.get("approve") is not True
        or decision.get("batch") != batch["id"]
        or set(decision) != {"approve", "batch", "proposal"}
    ):
        raise PermissionError(
            "Astra must approve the exact batch and proposal returned by its native response"
        )
    body = store.get(receipt["request_hash"])
    source = json.loads(body["input"][0]["content"])
    request = store.get(batch["request_hash"])
    if (
        source.get("batch") != batch["id"]
        or source.get("source_hash") != batch["request_hash"]
        or source.get("request") != request
    ):
        raise PermissionError("Astra approval did not review this source batch")
    proposal = decision["proposal"]
    if batch["status"] == "completed":
        if store.get(batch["result_hash"]) != proposal:
            raise ValueError("Memory batch already published different contents")
        return
    memory.validate("astra", proposal, request)
    operation = "upkeep:" + call
    with store.transaction():
        payload = store.put(
            {"operation": "memory_record", "batch": batch["id"], "proposal": proposal}
        )
        store.db.execute(
            "INSERT OR IGNORE INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                operation,
                batch["task_id"],
                "maintenance",
                "memory_record",
                "prepared",
                payload,
                None,
                now(),
                None,
                None,
            ),
        )
        store.db.execute(
            "INSERT OR IGNORE INTO memory_approvals VALUES(?,?,?,?,?)",
            (operation, batch["task_id"], call, payload, now()),
        )
        memory.publish(batch, proposal, request, approval=operation)
        store.db.execute(
            "UPDATE operations SET status='completed',result_hash=?,completed_at=? WHERE id=?",
            (store.put(proposal), now(), operation),
        )
        store.db.execute(
            "UPDATE semantic_upkeep SET status='completed',error=NULL,updated_at=? WHERE batch_id=?",
            (now(), batch["id"]),
        )
        store.event(
            "memory.upkeep_approved",
            {"batch": batch["id"], "approval_call": call, "version": memory.version("astra")},
            agent="astra",
        )


async def run(runtime, task, *, eligible=None):
    memory = runtime.context.history
    store = runtime.store
    if not store.config.data["memory"].get("semantic_upkeep_enabled", True):
        return {"published": 0, "disabled": True}
    eligible = eligible or memory.idle_due
    published = 0
    errors = []
    for _ in range(store.config.data["memory"].get("max_batches_per_pass", 4)):
        if not eligible():
            raise MemoryInterrupted("New owner work preempted semantic upkeep")
        reviewed = memory.prepare("astra", task)
        if not reviewed.get("batch"):
            break
        batch = store.one("SELECT * FROM memory_batches WHERE id=?", (reviewed["batch"],))
        previous = store.one("SELECT * FROM semantic_upkeep WHERE batch_id=?", (batch["id"],))
        if previous and previous["status"] in ("failed", "declined", "uncertain"):
            errors.append(
                {"batch": batch["id"], "status": previous["status"], "error": previous["error"]}
            )
            break
        call = "upkeep_" + batch["id"]
        store.db.execute(
            "INSERT OR IGNORE INTO semantic_upkeep VALUES(?,?,?,?,?)",
            (batch["id"], call, "prepared", None, now()),
        )
        payload = {
            "batch": batch["id"],
            "source_hash": batch["request_hash"],
            "request": reviewed["request"],
        }
        items = [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        instructions = INSTRUCTIONS + "\nProposal contract:\n" + PROMPT
        profile = {
            **store.config.profile("astra"),
            "effort": store.config.data["memory"].get("maintenance_effort", "high"),
        }
        try:
            for attempt in range(2):
                ident = call + ("_correction" if attempt else "")
                if not eligible():
                    raise MemoryInterrupted("New owner work preempted semantic upkeep")
                store.db.execute(
                    "UPDATE semantic_upkeep SET call_id=?,status='running',updated_at=? WHERE batch_id=?",
                    (ident, now(), batch["id"]),
                )
                actual, response = await runtime.models.infer(
                    "astra",
                    None,
                    instructions,
                    items,
                    [],
                    purpose="memory_upkeep",
                    profile_override=profile,
                    call_id=ident,
                    retry_limit=0,
                )
                text = "\n".join(
                    p["text"]
                    for x in response.get("output", [])
                    if x.get("type") == "message"
                    for p in x.get("content", [])
                    if "text" in p
                )
                try:
                    decision = json.loads(text)
                    if decision.get("approve") is False and decision.get("batch") == batch["id"]:
                        reason = str(decision.get("reason", "Astra declined publication"))
                        store.db.execute(
                            "UPDATE semantic_upkeep SET status='declined',error=?,updated_at=? WHERE batch_id=?",
                            (reason, now(), batch["id"]),
                        )
                        return {"published": published, "declined": reason}
                    if not eligible():
                        raise MemoryInterrupted("Owner input arrived before publication")
                    publish(memory, batch, actual, decision)
                    published += 1
                    break
                except ValueError as invalid:
                    receipt = store.one("SELECT status FROM model_calls WHERE id=?", (actual,))
                    if attempt or not receipt or receipt["status"] != "completed":
                        raise
                    items = [
                        *items,
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "validation_error": str(invalid),
                                    "rejected_proposal": text,
                                    "instruction": "Correct the proposal using the same original fragments. Preserve existing facts, exact quotations and revisions. You may approve an episode with empty updates; never invent support.",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ]
        except (asyncio.CancelledError, MemoryInterrupted):
            store.db.execute(
                "UPDATE semantic_upkeep SET status='interrupted',updated_at=? WHERE batch_id=?",
                (now(), batch["id"]),
            )
            raise
        except Exception as exc:
            receipt = store.one("SELECT status FROM model_calls WHERE id=?", (ident,))
            status = "uncertain" if receipt and receipt["status"] == "uncertain" else "failed"
            store.db.execute(
                "UPDATE semantic_upkeep SET status=?,error=?,updated_at=? WHERE batch_id=?",
                (status, label(exc), now(), batch["id"]),
            )
            errors.append({"batch": batch["id"], "status": status, "error": label(exc)})
            break
    return {
        "published": published,
        "errors": errors,
        "pending_fragments": len(memory.pending("astra", task)),
    }
