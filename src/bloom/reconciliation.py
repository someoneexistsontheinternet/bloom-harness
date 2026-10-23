"""Explicit abandonment of an unacknowledged, effect-free model purchase.

The original status/usage, transport stream and diagnostics are preserved. This
does not assert the provider did no work, retrieve a reply, or execute a tool.
"""

from .store import now


def abandon(store, call_id, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Reconciliation requires an operator reason")
    prior = store.one("SELECT * FROM model_reconciliations WHERE call_id=?", (call_id,))
    if prior:
        return prior
    row = store.one("SELECT * FROM model_calls WHERE id=?", (call_id,))
    if not row or row["status"] != "uncertain":
        raise ValueError("Select an uncertain model request")
    body = store.get(row["request_hash"])
    if any(
        t.get("type") not in ("function", "web_search", "web_search_preview")
        for t in body.get("tools", [])
    ):
        raise ValueError("Hosted mutation tools require external-effect reconciliation first")
    if store.one("SELECT 1 FROM items WHERE model_call_id=?", (call_id,)) or row["response_hash"]:
        raise ValueError("Request has accepted output; inspect it before any abandonment")
    browser = store.one("SELECT 1 FROM browser_sessions WHERE pending_call=?", (call_id,))
    if browser:
        raise ValueError("Browser submission requires visible reply reconciliation first")
    original = {
        "call": row,
        "stream": store.one("SELECT * FROM model_call_streams WHERE call_id=?", (call_id,)),
        "diagnostics": store.one(
            "SELECT * FROM model_call_diagnostics WHERE call_id=?", (call_id,)
        ),
    }
    with store.transaction():
        store.db.execute(
            "INSERT INTO model_reconciliations VALUES(?,?,?,?,?)",
            (call_id, store.put(original), "abandoned_unacknowledged_purchase", reason, now()),
        )
        store.db.execute(
            "UPDATE model_calls SET status='failed',error=coalesce(error,'') || '; explicit abandonment of unacknowledged purchase; provider outcome/usage remain unknown where unreported; no local tools dispatched' WHERE id=?",
            (call_id,),
        )
        store.event(
            "model.reconciled",
            {
                "call": call_id,
                "decision": "abandoned_unacknowledged_purchase",
                "reason": reason,
                "original_hash": store.one(
                    "SELECT original_hash FROM model_reconciliations WHERE call_id=?", (call_id,)
                )["original_hash"],
            },
            row["task_id"],
            row["agent_id"],
        )
    return store.one("SELECT * FROM model_reconciliations WHERE call_id=?", (call_id,))
