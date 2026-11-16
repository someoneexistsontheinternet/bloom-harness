"""Relational inspection views derived entirely from committed filesystem events."""

import json


class Index:
    """Project common identities/lifecycle/usage into tables, leaving originals in files."""

    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS projection_cursor(singleton INTEGER PRIMARY KEY, seq INTEGER NOT NULL);
            INSERT OR IGNORE INTO projection_cursor VALUES(1,0);
            CREATE TABLE IF NOT EXISTS agent_index(
              id TEXT PRIMARY KEY, role TEXT NOT NULL, parent TEXT, responsibility TEXT NOT NULL, source_event TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_index(
              id TEXT PRIMARY KEY, agent TEXT NOT NULL, parent_task TEXT, purpose TEXT NOT NULL,
              status TEXT NOT NULL, required INTEGER NOT NULL, origin TEXT NOT NULL,
              opened_event TEXT NOT NULL, opened_seq INTEGER NOT NULL, updated_seq INTEGER NOT NULL,
              metadata TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS task_ready ON task_index(status,agent);
            CREATE TABLE IF NOT EXISTS call_index(
              id TEXT PRIMARY KEY, agent TEXT NOT NULL, task TEXT NOT NULL, purpose TEXT NOT NULL,
              model_requested TEXT NOT NULL, model_reported TEXT, status TEXT NOT NULL,
              request_sha TEXT NOT NULL, response_sha TEXT, input_tokens INTEGER,
              cached_tokens INTEGER, output_tokens INTEGER, budget TEXT NOT NULL, source_event TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS calls_task ON call_index(task,agent);
            CREATE TABLE IF NOT EXISTS memory_index(
              owner TEXT NOT NULL, layer TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL,
              source_sha TEXT NOT NULL, approval TEXT, source_event TEXT NOT NULL,
              PRIMARY KEY(owner,layer,key,revision));
        """)

    def clear(self):
        for table in ("agent_index", "task_index", "call_index", "memory_index"):
            self.db.execute("DELETE FROM " + table)
        self.db.execute("UPDATE projection_cursor SET seq=0")

    def apply(self, event):
        """Update an index only after its corresponding event file has committed."""
        data, kind = event["data"], event["kind"]
        if kind == "agent.created":
            self.db.execute("INSERT INTO agent_index VALUES(?,?,?,?,?)", (
                data["id"], data["role"], data["parent"], data["responsibility"], event["id"]))
        elif kind == "agent.responsibility":
            self.db.execute("UPDATE agent_index SET responsibility=?,source_event=? WHERE id=?",
                            (data["text"], event["id"], event["target"]))
        elif kind == "task.opened":
            self.db.execute("INSERT INTO task_index VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                event["task"], event["target"], data["parent_task"], data["purpose"], "ready",
                data["required"], data["origin"], event["id"], event["seq"], event["seq"], json.dumps(data)))
        elif kind in ("task.ready", "task.waiting", "task.completed", "task.failed", "task.blocked", "task.paused"):
            row = self.db.execute("SELECT metadata FROM task_index WHERE id=?", (event["task"],)).fetchone()
            if row:
                metadata = {**json.loads(row[0]), **data}
                self.db.execute("UPDATE task_index SET status=?,updated_seq=?,metadata=? WHERE id=?",
                                (kind.split(".")[1], event["seq"], json.dumps(metadata), event["task"]))
        elif kind == "model.started":
            self.db.execute("INSERT INTO call_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                event["id"], event["target"], event["task"], data["purpose"], data["model"], None,
                "started", data["request"]["sha256"], None, None, None, None, data["budget"], event["id"]))
        elif kind == "model.completed":
            usage = data.get("usage") or {}
            self.db.execute("UPDATE call_index SET status='completed',model_reported=?,response_sha=?,input_tokens=?,cached_tokens=?,output_tokens=?,source_event=? WHERE id=?",
                (data.get("model_reported"), data["response"]["sha256"], usage.get("input_tokens"),
                 usage.get("input_tokens_details", {}).get("cached_tokens"), usage.get("output_tokens"), event["id"], data.get("call")))
        elif kind in ("model.failed", "model.abandoned"):
            self.db.execute("UPDATE call_index SET status=?,source_event=? WHERE id=?",
                            (kind.split(".")[1], event["id"], data["call"]))
        elif kind == "memory.accepted":
            self.db.execute("INSERT INTO memory_index VALUES(?,?,?,?,?,?,?)", (event["target"], data["layer"],
                data["key"], data["revision"], data["source"]["sha256"], data["approval"], event["id"]))
        self.db.execute("UPDATE projection_cursor SET seq=?", (event["seq"],))

    def tasks(self):
        return {r["id"]: {**json.loads(r["metadata"]), "id": r["id"], "agent": r["agent"],
            "status": r["status"], "opened": r["opened_event"], "seq": r["opened_seq"]}
            for r in self.db.execute("SELECT * FROM task_index ORDER BY opened_seq")}
