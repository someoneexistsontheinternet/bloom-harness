"""A durable, bounded final-report phase for scoped read-only workers."""

from __future__ import annotations

import time

from .store import now


MAX_CALLS = 2  # One synthesis and at most one exact-source/format correction.


def phase(store, task):
    return store.one("SELECT * FROM worker_reporting WHERE task_id=?", (task,))


def deadline(store, task, exploration_deadline):
    state = phase(store, task)
    return state["deadline"] if state else exploration_deadline


def prepare(store, task, exploration_deadline, *, reserve_turns, reserve_seconds, force=False):
    state = phase(store, task)
    if state:
        return state["deadline"]
    row = store.one("SELECT * FROM tasks WHERE id=?", (task,))
    remaining = exploration_deadline - time.time()
    due = row["turns"] > 0 and (
        row["max_turns"] - row["turns"] <= reserve_turns or remaining < reserve_seconds
    )
    if not force and (remaining <= 0 or not due):
        return exploration_deadline
    with store.transaction():
        limit = time.time() + store.config.data["runtime"]["worker_report_seconds"]
        store.db.execute(
            "INSERT INTO worker_reporting VALUES(?,?,?,?)", (task, row["turns"], limit, now())
        )
        store.item(
            row["agent_id"],
            {
                "role": "user",
                "content": "Reporting phase: stop discovery and synthesize the available successful receipts now. "
                "State what answered the assignment, exact evidence, and any remaining limitation. "
                "Discovery tools are withheld; observation of already-running processes is still permitted. "
                "You have a separate fixed reporting deadline, one synthesis and at most one source/format correction. "
                "Return the required final JSON; no more scanning or repeated completed work.",
            },
            task,
            key=task + ":reporting",
            origin="worker_reporting",
        )
    return limit
