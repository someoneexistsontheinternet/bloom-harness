"""Explicit immutable evidence grants from an assignment's parent to its worker."""

from __future__ import annotations

from pathlib import Path

from .store import now


def allowed(store, agent, *, digest=None, path=None):
    if digest:
        return store.one(
            "SELECT * FROM evidence_grants WHERE agent_id=? AND artifact_hash=?", (agent, digest)
        )
    if path:
        return store.one(
            "SELECT * FROM evidence_grants WHERE agent_id=? AND path=?",
            (agent, str(Path(path).resolve())),
        )
    return None


def give(store, parent, child, task, digest, path="", content_sha256=None):
    path = str(Path(path).resolve()) if path else ""
    store.db.execute(
        "INSERT OR IGNORE INTO evidence_grants VALUES(?,?,?,?,?,?,?)",
        (child, digest, path, content_sha256, parent, task, now()),
    )


def parent_task(store, parent, child, task, index):
    give(store, parent, child, task, index)
    for row in store.rows(
        "SELECT result_hash FROM operations WHERE task_id=? AND result_hash IS NOT NULL", (task,)
    ):
        give(store, parent, child, task, row["result_hash"])
    for row in store.rows(
        "SELECT f.hash,f.path FROM web_files f JOIN web_sources s ON s.id=f.source_id JOIN web_runs r ON r.operation_id=s.operation_id WHERE r.task_id=?",
        (task,),
    ):
        give(store, parent, child, task, row["hash"], row["path"], row["hash"])
    for row in store.rows(
        "SELECT path,source_hash,text_hash,metadata_hash FROM tool_result_files WHERE task_id=?",
        (task,),
    ):
        give(store, parent, child, task, row["source_hash"], row["path"], row["text_hash"])
        give(store, parent, child, task, row["metadata_hash"])


def sources(store, parent, child, task, references):
    cwd = Path(store.one("SELECT cwd FROM agents WHERE id=?", (parent,))["cwd"])
    for reference in references or []:
        if not isinstance(reference, str):
            raise ValueError("Evidence sources must be exact paths or artifact hashes")
        digest = reference.removeprefix("artifact:")
        prior = allowed(store, parent, digest=digest)
        if prior:
            give(store, parent, child, task, digest)
            continue
        path = (cwd / Path(reference).expanduser()).resolve()
        prior = allowed(store, parent, path=path)
        if prior:
            give(store, parent, child, task, prior["artifact_hash"], path, prior["content_sha256"])
            continue
        web = store.one(
            "SELECT f.*,r.agent_id FROM web_files f JOIN web_sources s ON s.id=f.source_id JOIN web_runs r ON r.operation_id=s.operation_id WHERE f.path=?",
            (str(path),),
        )
        if web and web["agent_id"] == parent:
            give(store, parent, child, task, web["hash"], path, web["hash"])
        saved = store.one(
            "SELECT * FROM tool_result_files WHERE path=? AND agent_id=?", (str(path), parent)
        )
        if saved:
            give(store, parent, child, task, saved["source_hash"], path, saved["text_hash"])
        own = store.one(
            "SELECT 1 FROM operations o JOIN tasks t ON t.id=o.task_id WHERE t.agent_id=? AND o.result_hash=?",
            (parent, digest),
        )
        if own:
            give(store, parent, child, task, digest)
        # Ordinary project paths still use the worker's cwd/path enforcement.
        # Prose or an unknown path never grants private/sibling runtime access.
