"""Verified conversation rewind. Original branches and external delivery receipts survive."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from . import reset
from .config import Config
from .context import Context
from .store import Store, atomic, encoded, now


def specification(config, item, request_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", request_id):
        raise ValueError("Use a stable simple rewind request ID")
    with reset.reader(config) as store:
        row = store.one(
            "SELECT i.*,o.kind origin FROM items i JOIN item_origins o ON o.item_id=i.id WHERE i.id=?",
            (item,),
        )
        if not row or row["session_id"] != "astra" or row["origin"] != "owner":
            raise ValueError("Rewind requires an original Astra owner-message boundary")
        task = store.one("SELECT * FROM tasks WHERE id=?", (row["task_id"],))
        if (
            task["parent_id"]
            or store.one(
                "SELECT min(source_item) n FROM owner_turns WHERE task_id=?", (task["id"],)
            )["n"]
            != item
        ):
            raise ValueError("Only the first owner message of a root task can be rerolled")
        cutoff = task["created_at"]
        if store.one("SELECT 1 FROM tasks WHERE created_at<? AND updated_at>=?", (cutoff, cutoff)):
            raise ValueError(
                "An earlier task changed after this boundary; historical reconstruction requires review"
            )
        if store.one(
            "SELECT 1 FROM model_calls WHERE started_at<? AND (completed_at>=? OR status IN ('prepared','running','uncertain'))",
            (cutoff, cutoff),
        ):
            raise ValueError("An inference crosses the rewind boundary")
        call = store.one(
            "SELECT id FROM model_calls WHERE task_id=? AND purpose='turn' ORDER BY rowid LIMIT 1",
            (task["id"],),
        )
        if not call:
            raise ValueError("No retained original request membership at this boundary")
        members = store.rows(
            "SELECT i.id,i.envelope_hash FROM model_call_items m JOIN items i ON i.id=m.item_id WHERE m.call_id=? AND i.id<? ORDER BY m.position",
            (call["id"], item),
        )
        return {
            "request_id": request_id,
            "item": item,
            "task": task["id"],
            "cutoff": cutoff,
            "prompt": row["text"],
            "prompt_hash": hashlib.sha256(row["text"].encode()).hexdigest(),
            "source_envelope": row["envelope_hash"],
            "channel": task["channel"],
            "address": task["address"],
            "prefix": members,
            "first_call": call["id"],
        }


def closure(db, tables):
    """Prune references to retired rows; keep external idempotency ledgers intact."""
    retained = {"deliveries", "inbound_messages", "owner_requests"}
    while True:
        changes = 0
        for table in tables:
            groups = {}
            for fk in db.execute('PRAGMA foreign_key_list("' + table + '")'):
                groups.setdefault(fk[0], []).append(fk)
            for group in groups.values():
                parent = group[0][2]
                columns = [g[3] for g in group]
                where = " AND ".join('child."' + g[3] + '" IS NOT NULL' for g in group)
                match = " AND ".join('parent."' + g[4] + '"=child."' + g[3] + '"' for g in group)
                missing = (
                    where
                    + ' AND NOT EXISTS(SELECT 1 FROM "'
                    + parent
                    + '" parent WHERE '
                    + match
                    + ")"
                )
                if table in retained:
                    if len(columns) != 1 or columns[0] not in ("task_id", "item_id"):
                        if db.execute(
                            'SELECT 1 FROM "' + table + '" child WHERE ' + missing + " LIMIT 1"
                        ).fetchone():
                            raise ValueError("Cannot preserve an external receipt: " + table)
                        continue
                    sql = (
                        'UPDATE "'
                        + table
                        + '" AS child SET "'
                        + columns[0]
                        + '"=NULL WHERE '
                        + missing
                    )
                else:
                    sql = 'DELETE FROM "' + table + '" AS child WHERE ' + missing
                changes += db.execute(sql).rowcount
        if not changes:
            break


def verify_database(store, spec):
    rows = Context(store, None).selected("astra", None)
    actual = [{"id": r["id"], "envelope_hash": r["envelope_hash"]} for r in rows]
    if actual != spec["prefix"]:
        raise RuntimeError("Rewound history differs from the original pre-question input")
    if store.one("SELECT 1 FROM items WHERE id>=?", (spec["item"],)):
        raise RuntimeError("Later conversation leaked into the rewound session")
    if store.one("SELECT 1 FROM tasks WHERE created_at>=?", (spec["cutoff"],)):
        raise RuntimeError("Later tasks leaked into the rewound session")
    if (
        store.rows("PRAGMA foreign_key_check")
        or store.one("PRAGMA integrity_check")["integrity_check"] != "ok"
    ):
        raise RuntimeError("Rewind database integrity failed")


def prepare_database(config, state, directory):
    spec = state["rewind"]
    cutoff = spec["cutoff"]
    path = directory / "database-fresh.sqlite3"
    path.unlink(missing_ok=True)
    with (
        contextlib.closing(sqlite3.connect(directory / "database-before.sqlite3")) as old,
        contextlib.closing(sqlite3.connect(path)) as db,
    ):
        old.backup(db)
        db.executescript(Path(__file__).with_name("schema.sql").read_text())
        db.execute("PRAGMA foreign_keys=OFF")
        tables = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND sql NOT LIKE 'CREATE VIRTUAL%' AND name NOT LIKE 'search_items_%' AND name NOT LIKE 'archive_search_%' AND name NOT LIKE 'memory_search_%'"
            )
        ]
        preserve = {
            "artifacts",
            "artifact_access",
            "archive_files",
            "archive_chunks",
            "deliveries",
            "delivery_payloads",
            "delivery_ordering",
            "delivery_retries",
            "inbound_messages",
            "owner_requests",
            "discord_cursors",
            "channel_state",
            "schema_versions",
        }
        with db:
            for table in tables:
                if table in preserve:
                    continue
                columns = {r[1] for r in db.execute('PRAGMA table_info("' + table + '")')}
                stamp = (
                    "created_at"
                    if "created_at" in columns
                    else "started_at"
                    if "started_at" in columns
                    else None
                )
                if stamp:
                    db.execute('DELETE FROM "' + table + '" WHERE "' + stamp + '">=?', (cutoff,))
            db.execute("DELETE FROM items WHERE id>=?", (spec["item"],))
            db.execute(
                "DELETE FROM memory_nodes WHERE first_item>=? OR kind='day'", (spec["item"],)
            )
            closure(db, tables)
            # These links intentionally have no FK in the runtime schema.
            db.execute(
                "DELETE FROM operation_turns WHERE operation_id NOT IN (SELECT id FROM operations)"
            )
            db.execute(
                "UPDATE memory_fragments SET batch_id=NULL WHERE batch_id NOT IN (SELECT id FROM memory_batches)"
            )
            db.execute(
                "DELETE FROM memory_fact_revisions WHERE batch_id IS NOT NULL AND batch_id NOT IN (SELECT id FROM memory_batches)"
            )
            db.execute("DELETE FROM memories")
            db.execute(
                "INSERT INTO memories SELECT r.topic,r.revision,r.summary,r.detail_hash,r.created_at FROM memory_revisions r WHERE r.revision=(SELECT max(x.revision) FROM memory_revisions x WHERE x.topic=r.topic)"
            )
            existing_facts = {
                (r[0], r[1], r[2], r[3]): r[4]
                for r in db.execute(
                    "SELECT agent_id,kind,key,revision,updated_at FROM memory_facts"
                )
            }
            db.execute("DELETE FROM memory_facts")
            for row in db.execute(
                "SELECT agent_id,kind,key,revision,record_json,created_at FROM memory_fact_revisions r WHERE revision=(SELECT max(x.revision) FROM memory_fact_revisions x WHERE x.agent_id=r.agent_id AND x.kind=r.kind AND x.key=r.key)"
            ).fetchall():
                agent, kind, key, revision, record, stamp = row
                fact = json.loads(record)
                stamp = existing_facts.get((agent, kind, key, revision), stamp)
                db.execute(
                    "INSERT INTO memory_facts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        agent,
                        kind,
                        key,
                        fact["scope"],
                        fact["text"],
                        fact["status"],
                        fact["confidence"],
                        revision,
                        json.dumps(fact["sources"]),
                        fact["quote"],
                        stamp,
                    ),
                )
            db.execute("DELETE FROM memory_heads")
            db.execute(
                "INSERT INTO memory_heads SELECT agent_id,max(version)+1 FROM memory_batches WHERE status='completed' GROUP BY agent_id"
            )
            # Rebuild mutable event previews from immutable originals, then only
            # the accepted pre-boundary descriptions. No model invents a past.
            descriptions = {}
            for (batch,) in db.execute(
                "SELECT result_hash FROM memory_batches WHERE status='completed' ORDER BY created_at"
            ):
                with reset.reader(config, directory / "database-before.sqlite3") as original:
                    response = json.loads(original.bytes(batch, touch=False, cache=False))
                for event in response.get("events", []):
                    descriptions.setdefault(event["id"], []).append(event["description"])
            for ident, item, text, kind, digest in db.execute(
                "SELECT n.id,i.id,i.text,i.kind,i.envelope_hash FROM memory_nodes n JOIN items i ON i.id=n.first_item WHERE n.kind='event'"
            ).fetchall():
                if kind == "function_call":
                    with reset.reader(config, directory / "database-before.sqlite3") as original:
                        text = json.loads(original.bytes(digest, touch=False, cache=False)).get(
                            "arguments", ""
                        )
                preview = (
                    text
                    if len(text) <= 1600
                    else text[:800] + "\n[Navigation excerpt; expand exact source]\n" + text[-800:]
                )
                body = preview + "".join(
                    "\nDerived fragment description: " + x for x in descriptions.get(ident, [])
                )
                db.execute(
                    "UPDATE memory_nodes SET body=?,last_item=? WHERE id=?", (body, item, ident)
                )
            for agent, day in db.execute(
                "SELECT DISTINCT agent_id,day FROM memory_nodes WHERE kind='episode'"
            ).fetchall():
                episodes = db.execute(
                    "SELECT id,title,body,first_item,last_item FROM memory_nodes WHERE agent_id=? AND day=? AND kind='episode' ORDER BY first_item",
                    (agent, day),
                ).fetchall()
                ident = f"day:{agent}:{day}"
                db.execute(
                    "INSERT INTO memory_nodes VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        ident,
                        agent,
                        "day",
                        None,
                        day,
                        day + " activity timeline",
                        "\n\n".join(x[1] + ": " + x[2] for x in episodes),
                        min(x[3] for x in episodes),
                        max(x[4] for x in episodes),
                        cutoff,
                    ),
                )
                db.executemany(
                    "INSERT INTO memory_edges VALUES(?,?)", [(ident, x[0]) for x in episodes]
                )
            db.execute("INSERT INTO search_items(search_items) VALUES('rebuild')")
            db.execute("DELETE FROM memory_search")
            db.execute(
                "INSERT INTO memory_search SELECT id,agent_id,title||char(10)||body FROM memory_nodes"
            )
            db.execute(
                "UPDATE activity_feed SET after_call_rowid=(SELECT coalesce(max(rowid),0) FROM model_calls)"
            )
        db.execute("PRAGMA foreign_keys=ON")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Rewind left orphan references")
    with (
        reset.reader(config, path) as store,
        reset.reader(config, directory / "database-before.sqlite3") as original,
    ):
        verify_database(store, spec)
        for table in (
            "artifacts",
            "archive_files",
            "archive_chunks",
            "discord_cursors",
            "delivery_payloads",
            "delivery_ordering",
        ):
            if store.rows("SELECT * FROM " + table) != original.rows("SELECT * FROM " + table):
                raise RuntimeError("Rewind changed preserved evidence: " + table)
        for table in ("deliveries", "inbound_messages", "owner_requests"):

            def immutable(rows):
                return [
                    {k: v for k, v in r.items() if k not in ("task_id", "item_id")} for r in rows
                ]

            if immutable(store.rows("SELECT * FROM " + table)) != immutable(
                original.rows("SELECT * FROM " + table)
            ):
                raise RuntimeError("Rewind changed external receipts: " + table)
        state["rewind_verification"] = {
            "exact_prefix": True,
            "foreign_keys": True,
            "external_receipts_preserved": True,
            "original_artifacts_preserved": True,
            "prior_memory_facts": store.rows("SELECT * FROM memory_facts"),
        }
    path.chmod(0o600)
    with path.open("rb") as stream:
        import os

        os.fsync(stream.fileno())
    return reset.digest(path)


def apply(config, item, request_id):
    # A completed receipt wins even if item IDs have since been reused.
    for path in sorted((config.workspace / "resets").glob("*.json")):
        prior = json.loads(path.read_text())
        if prior.get("rewind", {}).get("request_id") == request_id:
            if prior["rewind"]["item"] != item:
                raise ValueError("Rewind request ID reused")
            return prior
    pending = config.workspace / "reset-pending.json"
    spec = (
        json.loads(pending.read_text()).get("rewind")
        if pending.exists()
        else specification(config, item, request_id)
    )
    if not spec or spec["item"] != item or spec["request_id"] != request_id:
        raise ValueError("A different reset is pending")
    result = reset.apply(config, allow_terminal_records=True, rewind=spec)
    return json.loads((config.workspace / "resets" / (result["id"] + ".json")).read_text())


def submit(config, state):
    if state["phase"] != "completed" or not state.get("rewind"):
        raise ValueError("Complete the verified rewind first")
    spec = state["rewind"]
    key = "rewind-" + spec["request_id"]
    store = Store(config)
    try:
        with store.transaction():
            prior = store.one("SELECT * FROM owner_requests WHERE id=?", (key,))
            if prior:
                if store.get(prior["source_hash"]) != spec:
                    raise ValueError("Replay identity changed")
                return store.get(prior["result_hash"])
            verify_database(store, spec)
            # Retain exact prompt bytes. External effects are explicitly separate
            # from the restored conversation, with the full old branch accessible.
            task = store.new_task(
                spec["prompt"], channel=spec["channel"], address=spec["address"], key=key
            )
            notice = {
                "owner_authorized_reroll": True,
                "original_owner_item": spec["item"],
                "backup": state["backup"]["path"],
                "retained_database": str(
                    Path(state["backup"]["path"]).parent / "database-before.sqlite3"
                ),
                "instruction": "The conversation was rewound to this question. Later messages/results are retained in the backup; existing files and external effects were not undone. Inspect before overwriting or repeating an external effect. This is not an instruction to redo later follow-up questions.",
            }
            store.item(
                "astra",
                {"role": "user", "content": json.dumps(notice)},
                task,
                origin="runtime_notice",
            )
            result = {
                "task": task,
                "reset_id": state["id"],
                "request_id": key,
                "accepted_at": now(),
                "backup": state["backup"],
            }
            store.db.execute(
                "INSERT INTO owner_requests VALUES(?,?,?,?,?,?,?,?)",
                (
                    key,
                    store.put(spec),
                    "message",
                    "completed",
                    task,
                    store.put(result),
                    "verified-rewind:" + state["id"],
                    now(),
                ),
            )
            store.event("owner.replay", notice, task, "astra")
        for row in store.rows("SELECT topic FROM memories"):
            Context(store, None).export_memory(row["topic"])
        return result
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "submit"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--item", type=int, required=True)
    parser.add_argument("--request-id", required=True)
    args = parser.parse_args()
    config = Config(args.root)
    if args.action == "plan":
        result = specification(config, args.item, args.request_id)
    else:
        result = apply(config, args.item, args.request_id)
        if args.action == "submit":
            result = submit(config, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
