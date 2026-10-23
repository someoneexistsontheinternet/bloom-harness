"""Durable Discord JSON records; raw evidence and derived views stay distinct."""

import base64
import gzip
import hashlib
import json

from .privacy import clean
from . import turns


def attachments(store, name, value):
    data = (json.dumps(clean(value), ensure_ascii=False, indent=2) + "\n").encode()
    if len(data) <= 7_000_000:
        parts = [(name + ".json", data, "application/json")]
    else:
        compressed = gzip.compress(data, compresslevel=6, mtime=0)
        if len(compressed) <= 7_000_000:
            parts = [(name + ".json.gz", compressed, "application/gzip")]
        else:
            size = 1_000_000
            count = (len(compressed) + size - 1) // size
            parts = [
                (
                    f"{name}.{i + 1:04d}-of-{count:04d}.json",
                    json.dumps(
                        {
                            "encoding": "base64 chunk of gzip JSON",
                            "original_sha256": hashlib.sha256(data).hexdigest(),
                            "gzip_sha256": hashlib.sha256(compressed).hexdigest(),
                            "part": i + 1,
                            "parts": count,
                            "data": base64.b64encode(
                                compressed[i * size : (i + 1) * size]
                            ).decode(),
                        }
                    ).encode(),
                    "application/json",
                )
                for i in range(count)
            ]
    return [
        {
            "filename": filename,
            "hash": store.put(raw, media),
            "bytes": len(raw),
            "content_type": media,
        }
        for filename, raw, media in parts
    ]


def queue_records(delivery):
    store = delivery.store
    feed = store.one("SELECT * FROM activity_feed WHERE singleton=1")
    if not feed or not delivery.discord.settings["enabled"]:
        return
    # Record each stage exactly once; do not backfill pre-activation conversations.
    calls = store.rows(
        "SELECT c.*,t.id AS delivery_task FROM model_calls c "
        "LEFT JOIN working_compactions w ON w.call_id=c.id "
        "LEFT JOIN semantic_upkeep u ON c.id IN ('upkeep_' || u.batch_id,'upkeep_' || u.batch_id || '_correction') "
        "LEFT JOIN memory_batches b ON b.id=u.batch_id "
        "JOIN tasks t ON t.id=coalesce(c.task_id,w.task_id,b.task_id) "
        "WHERE c.rowid>? AND t.channel='discord' AND c.status IN ('completed','failed','uncertain') "
        "AND (c.task_id IS NOT NULL OR c.rowid>coalesce((SELECT after_call_rowid FROM activity_maintenance_feed WHERE singleton=1),9223372036854775807)) "
        "AND NOT EXISTS (SELECT 1 FROM activity_records a WHERE a.call_id=c.id AND a.stage='results') ORDER BY c.rowid LIMIT 128",
        (feed["after_call_rowid"],),
    )
    queued = 0
    for call in calls:
        if queued >= 8:
            break
        if (
            call["purpose"] == "turn"
            and call["status"] == "completed"
            and not store.one("SELECT 1 FROM items WHERE model_call_id=? LIMIT 1", (call["id"],))
        ):
            continue  # Inference can finish before the runtime ingests native items.
        tools = store.rows(
            "SELECT c.*,o.status,o.result_hash,o.error,r.item_id AS result_item FROM tool_calls c JOIN items i ON i.id=c.item_id LEFT JOIN operations o ON o.id=c.operation_id LEFT JOIN tool_results r ON r.call_id=c.id WHERE i.model_call_id=? ORDER BY c.rowid",
            (call["id"],),
        )
        settled = all(
            t["result_item"] is not None or t["status"] in ("uncertain", "cancelled") for t in tools
        )
        stage = "results" if settled else "requested"
        if store.one(
            "SELECT 1 FROM activity_records WHERE call_id=? AND stage=?", (call["id"], stage)
        ):
            continue
        response = store.get(call["response_hash"]) if call["response_hash"] else {}
        value = {
            "format_version": 1,
            "stage": stage,
            "owner_turn_id": turns.call_turn(store, call["id"]),
            "model_call": {
                k: call[k]
                for k in (
                    "id",
                    "task_id",
                    "agent_id",
                    "purpose",
                    "model_requested",
                    "model_reported",
                    "status",
                    "input_tokens",
                    "cached_tokens",
                    "output_tokens",
                    "started_at",
                    "completed_at",
                    "error",
                )
            },
            "native_output": [
                x for x in response.get("output", []) if x.get("type") != "reasoning"
            ],
            "tools": [],
        }
        for tool in tools:
            item = (
                store.one("SELECT envelope_hash FROM items WHERE id=?", (tool["result_item"],))
                if tool["result_item"]
                else None
            )
            value["tools"].append(
                {
                    "id": tool["id"],
                    "operation_id": tool["operation_id"],
                    "name": tool["name"],
                    "arguments": store.get(tool["arguments_hash"]),
                    "status": tool["status"] or "pending",
                    "error": tool["error"],
                    "original_result": store.get(tool["result_hash"])
                    if tool["result_hash"]
                    else None,
                    "model_facing_result": store.get(item["envelope_hash"]) if item else None,
                }
            )
        files = attachments(store, call["id"] + "-" + stage, value)
        with store.transaction():
            for part, file in enumerate(files):
                delivery.queue_payload(
                    call["delivery_task"],
                    f"{call['id']}:{stage}:{part}",
                    {
                        "content": f"{call['agent_id']} · {stage} · {call['purpose']}\n`{call['id']}` · JSON {part + 1}/{len(files)}"
                    },
                    [file],
                    address=feed["channel_id"],
                )
            store.db.execute("INSERT INTO activity_records VALUES(?,?)", (call["id"], stage))
        queued += 1
