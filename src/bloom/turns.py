"""Causal owner-message attribution, separate from tasks and physical model calls."""

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib

from .store import now

_UNBOUND = object()
_turn = ContextVar("bloom_owner_turn", default=_UNBOUND)


def current(store, task):
    value = _turn.get()
    if value is not _UNBOUND:
        return value
    row = store.one(
        "SELECT t.turn_id FROM task_owner_turns t JOIN owner_turns o ON o.id=t.turn_id WHERE t.task_id=? AND o.replied_at IS NULL",
        (task,),
    )
    return row["turn_id"] if row else None


@contextmanager
def bind(turn):
    token = _turn.set(turn)
    try:
        yield
    finally:
        _turn.reset(token)


def owner_input(store, task, item):
    turn = "turn_" + hashlib.sha256(f"{task}:{item}".encode()).hexdigest()[:32]
    store.db.execute(
        "INSERT OR IGNORE INTO owner_turns(id,task_id,source_item,created_at) VALUES(?,?,?,?)",
        (turn, task, item, now()),
    )
    store.db.execute(
        "INSERT INTO task_owner_turns VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET turn_id=excluded.turn_id",
        (task, turn),
    )
    return turn


def inherit(store, task, parent):
    turn = current(store, parent)
    if turn:
        store.db.execute("INSERT OR IGNORE INTO task_owner_turns VALUES(?,?)", (task, turn))


def call_turn(store, call):
    row = store.one("SELECT turn_id FROM model_call_turns WHERE call_id=?", (call,))
    return row["turn_id"] if row else None


def attribute_call(store, call, turn):
    if turn:
        store.db.execute("INSERT OR IGNORE INTO model_call_turns VALUES(?,?)", (call, turn))


def close(store, task, final):
    # Only answer owner inputs actually included in the final decision's context.
    row = store.one("SELECT model_call_id FROM items WHERE id=?", (final,))
    boundary = store.one(
        "SELECT max(item_id) AS n FROM model_call_items WHERE call_id=?", (row["model_call_id"],)
    )["n"]
    if boundary is not None:
        store.db.execute(
            "UPDATE owner_turns SET reply_item=?,replied_at=? WHERE task_id=? AND source_item<=? AND replied_at IS NULL",
            (final, now(), task, boundary),
        )


def usage(store, turn):
    return store.one(
        "SELECT count(*) AS calls,coalesce(sum(input_tokens),0) AS input,coalesce(sum(cached_tokens),0) AS cached,coalesce(sum(output_tokens),0) AS output,coalesce(sum(input_tokens IS NULL OR cached_tokens IS NULL OR output_tokens IS NULL),0) AS unknown_calls FROM model_calls c JOIN model_call_turns t ON t.call_id=c.id WHERE t.turn_id=?",
        (turn,),
    )


def usage_by_model(store, *, turn=None, task=None):
    """Use the physical request's model, including helpers run for another agent."""
    if (turn is None) == (task is None):
        raise ValueError("Choose one owner turn or legacy task tree")
    if turn is not None:
        prefix = ""
        source = "model_calls c JOIN model_call_turns t ON t.call_id=c.id WHERE t.turn_id=?"
        identity = turn
    else:
        prefix = "WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT t.id FROM tasks t JOIN tree p ON t.parent_id=p.id) "
        source = "model_calls c WHERE c.task_id IN tree"
        identity = task
    return store.rows(
        prefix + "SELECT c.model_requested AS model,count(*) AS calls,"
        "coalesce(sum(c.input_tokens),0) AS input,coalesce(sum(c.cached_tokens),0) AS cached,"
        "count(c.cached_tokens) AS cached_calls,"
        "coalesce(sum(c.input_tokens-c.cached_tokens),0) AS uncached,"
        "count(c.input_tokens-c.cached_tokens) AS uncached_calls,"
        "coalesce(sum(c.output_tokens),0) AS output,"
        "count(c.output_tokens) AS output_calls,"
        "coalesce(sum(c.input_tokens IS NULL OR c.cached_tokens IS NULL OR c.output_tokens IS NULL),0) AS unknown_calls "
        "FROM " + source + " GROUP BY c.model_requested ORDER BY c.model_requested",
        (identity,),
    )


def latest_context(store, task, turn=None):
    """Last answering-agent request, never a sum of calls or worker contexts."""
    scope = (
        " AND EXISTS (SELECT 1 FROM model_call_turns t WHERE t.call_id=c.id AND t.turn_id=?)"
        if turn
        else ""
    )
    return store.one(
        "SELECT c.model_requested AS model,c.input_tokens,a.role FROM model_calls c "
        "JOIN tasks task ON task.id=c.task_id JOIN agents a ON a.id=task.agent_id "
        "WHERE c.task_id=? AND c.agent_id=task.agent_id AND c.purpose='turn'"
        + scope
        + " ORDER BY c.rowid DESC LIMIT 1",
        (task, turn) if turn else (task,),
    )
