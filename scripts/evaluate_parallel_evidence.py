"""Submit exact owner prompts after reset or a delivered predecessor; inspect usage."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import socket
import time
from pathlib import Path

from bloom.config import Config
from bloom.reset import reader
from bloom.service import health, process_identity
from bloom.store import Store, atomic, encoded, now
from bloom import turns


def submit(root, run, prompt_file, max_turns, after_task=None):
    if not 1 <= max_turns <= 20:
        raise ValueError("Replay guard must be between one and twenty Astra calls")
    config = Config(root)
    if not config.data["discord"]["enabled"]:
        raise ValueError("This replay uses the configured Discord workflow")
    text = prompt_file.read_text()
    session = json.loads((config.workspace / "session.json").read_text())
    identity = "parallel-replay-" + hashlib.sha256(str(run).encode()).hexdigest()[:24]
    path = run / "replay-intent.json"
    with reader(config) as old_store:
        old = old_store.one("SELECT * FROM owner_requests WHERE id=?", (identity,))
        if old:
            previous = json.loads(old_store.bytes(old["source_hash"], touch=False, cache=False))
            if (
                previous["prompt"] != text
                or previous["reset_id"] != session["reset_id"]
                or previous["max_astra_calls"] != max_turns
                or previous.get("after_task") != after_task
            ):
                raise ValueError("Replay identity already belongs to different input or session")
            result = json.loads(old_store.bytes(old["result_hash"], touch=False, cache=False))
            atomic(run / "replay.json", encoded(result))
            return result
    current = health(root)
    if not current["healthy"] or current["active_tasks"]:
        raise ValueError("Replay requires a healthy idle runtime")
    intent = {
        "request_id": identity,
        "prompt": text,
        "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "reset_id": session["reset_id"],
        "release": current["release"],
        "source": str(prompt_file),
        "max_astra_calls": max_turns,
    }
    if after_task:
        intent["after_task"] = after_task
    run.mkdir(parents=True, exist_ok=True)
    if path.exists() and json.loads(path.read_text()) != intent:
        raise ValueError("Replay identity already belongs to different input or session")
    atomic(path, encoded(intent))  # Intent precedes task acceptance.
    store = Store(config)
    try:
        with store.transaction():
            old = store.one("SELECT * FROM owner_requests WHERE id=?", (identity,))
            if old:
                return store.get(old["result_hash"])
            if after_task:
                previous = store.one("SELECT * FROM tasks WHERE id=?", (after_task,))
                request = store.one(
                    "SELECT result_hash FROM owner_requests WHERE task_id=?", (after_task,)
                )
                latest = store.one(
                    "SELECT id FROM tasks WHERE agent_id='astra' ORDER BY created_at DESC,rowid DESC LIMIT 1"
                )
                if (
                    not previous
                    or previous["agent_id"] != "astra"
                    or previous["status"] != "completed"
                    or not previous["final_item"]
                ):
                    raise ValueError(
                        "Follow-up requires a completed predecessor with its final answer"
                    )
                # Both ordinary replay and verified rewind acceptance receipts
                # identify the resulting task/session. Rewind source specs do not.
                accepted = store.get(request["result_hash"]) if request else {}
                if (
                    accepted.get("task") != after_task
                    or accepted.get("reset_id") != session["reset_id"]
                    or latest["id"] != after_task
                ):
                    raise ValueError(
                        "Follow-up predecessor must be the latest replay in this reset session"
                    )
                sent_final = store.one(
                    "SELECT 1 FROM deliveries WHERE task_id=? AND item_id=? AND kind='message' AND status='sent'",
                    (after_task, previous["final_item"]),
                )
                sent_usage = store.one(
                    "SELECT 1 FROM deliveries WHERE task_id=? AND kind='completion' AND status='sent'",
                    (after_task,),
                )
                pending = store.one(
                    "SELECT 1 FROM deliveries WHERE task_id=? AND status!='sent'", (after_task,)
                )
                if not sent_final or not sent_usage or pending:
                    raise ValueError(
                        "Follow-up waits for predecessor answer, usage card and outbox delivery"
                    )
                if store.one("SELECT 1 FROM tasks WHERE status IN ('ready','running','waiting')"):
                    raise ValueError("Follow-up waits for active work to settle")
            elif any(
                store.one("SELECT count(*) n FROM " + table)["n"]
                for table in ("tasks", "items", "model_calls", "contexts", "memory_facts")
            ):
                raise ValueError(
                    "Replay requires an empty conversation and memory after verified reset"
                )
            task = store.new_task(
                text, channel="discord", address=config.data["discord"]["channel_id"], key=identity
            )
            store.db.execute("UPDATE tasks SET max_turns=? WHERE id=?", (max_turns, task))
            result = {
                "task": task,
                "reset_id": session["reset_id"],
                "accepted_at": now(),
                "release": current["release"],
            }
            if after_task:
                result["after_task"] = after_task
            source_hash = store.put(intent)
            store.event(
                "owner.replay",
                {"request_id": identity, "source_hash": source_hash, "explicit_owner_test": True},
                task,
                "astra",
            )
            store.db.execute(
                "INSERT INTO owner_requests VALUES(?,?,?,?,?,?,?,?)",
                (
                    identity,
                    source_hash,
                    "message",
                    "completed",
                    task,
                    store.put(result),
                    str(path),
                    now(),
                ),
            )
        atomic(run / "replay.json", encoded(result))
        return result
    finally:
        store.close()


def inspect(root, run):
    replay = json.loads((run / "replay.json").read_text())
    with reader(Config(root)) as store:
        task = store.one("SELECT * FROM tasks WHERE id=?", (replay["task"],))
        owner = store.one("SELECT * FROM owner_turns WHERE task_id=?", (task["id"],))
        calls = store.rows(
            "SELECT c.id,c.agent_id,c.task_id,c.model_requested,c.purpose,c.status,c.input_tokens,c.cached_tokens,c.output_tokens,c.started_at,c.completed_at FROM model_calls c JOIN model_call_turns t ON t.call_id=c.id WHERE t.turn_id=? ORDER BY c.rowid",
            (owner["id"],),
        )
        batches = store.rows("SELECT * FROM evidence_batches WHERE parent_task=?", (task["id"],))
        workers = store.rows(
            "SELECT t.id,t.agent_id,t.status,t.turns,t.error,w.operation_id,w.position FROM evidence_workers w JOIN tasks t ON t.id=w.task_id WHERE t.parent_id=? ORDER BY w.position",
            (task["id"],),
        )
        worker_ids = {w["agent_id"] for w in workers}
        intervals = [c for c in calls if c["agent_id"] in worker_ids and c["completed_at"]]
        overlaps = [
            {"left": a["id"], "right": b["id"]}
            for i, a in enumerate(intervals)
            for b in intervals[i + 1 :]
            if a["agent_id"] != b["agent_id"]
            and max(a["started_at"], b["started_at"]) < min(a["completed_at"], b["completed_at"])
        ]
        final = (
            store.one("SELECT id,text FROM items WHERE id=?", (task["final_item"],))
            if task["final_item"]
            else None
        )
        deliveries = store.rows(
            "SELECT id,kind,status,address,external_id,error FROM deliveries WHERE task_id=? ORDER BY rowid",
            (task["id"],),
        )
        result = {
            "observed_at": now(),
            "replay": replay,
            "task": task,
            "usage": turns.usage(store, owner["id"]),
            "by_model": turns.usage_by_model(store, turn=owner["id"]),
            "calls": calls,
            "batches": batches,
            "workers": workers,
            "worker_call_overlaps": overlaps,
            "final": final,
            "deliveries": deliveries,
        }
    atomic(run / "replay-observation.json", encoded(result))
    # Helpers can run under Astra's agent ID while purchasing Luna. Count the
    # actual main-model names, matching the usage card, rather than their owner.
    astra_models = {
        c["model_requested"] for c in calls if c["agent_id"] == "astra" and c["purpose"] == "turn"
    }
    return {
        "status": task["status"],
        "astra_calls": sum(c["model_requested"] in astra_models for c in calls),
        "workers": workers,
        "overlapping_worker_call_pairs": len(overlaps),
        "usage": result["usage"],
        "final_item": task["final_item"],
        "completion_sent": any(
            d["kind"] == "completion" and d["status"] == "sent" for d in deliveries
        ),
    }


def sequence(
    root, run, intent_file, max_turns=20, *, after_task=None, poll_seconds=10, timeout_seconds=3600
):
    """Resume an exact ordered replay; delivery gates and request IDs prevent duplicates."""
    intent = json.loads(intent_file.read_text())
    if intent.get("after_task", after_task) != after_task:
        raise ValueError("Replay predecessor differs from the authorized sequence intent")
    prompts = intent["prompts"]
    if not prompts or [p["sequence"] for p in prompts] != list(range(1, len(prompts) + 1)):
        raise ValueError("Replay intent must have contiguous ordered prompts")

    def verify_prompt(prompt):
        path = Path(prompt["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != prompt["sha256"]:
            raise ValueError("Exact replay prompt changed after authorization")
        return path

    for prompt in prompts:
        verify_prompt(prompt)
    run.mkdir(parents=True, exist_ok=True)
    with (run / "sequence.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        command = [
            "evaluate_parallel_evidence.py",
            "sequence",
            "--root",
            str(root),
            "--run",
            str(run),
            "--intent",
            str(intent_file),
            "--max-astra-calls",
            str(max_turns),
        ]
        if after_task:
            command.extend(["--after-task", after_task])
        atomic(
            run / "sequence-process.json",
            encoded(
                {
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "start_identity": process_identity(os.getpid()),
                    "started_at": now(),
                    "intent": str(intent_file),
                    "command": command,
                    "stop": "Recheck PID/start identity before SIGTERM. Stopping this observer does not cancel accepted Bloom tasks.",
                }
            ),
        )
        deadline = time.monotonic() + timeout_seconds
        previous = after_task
        completed = []
        last = None
        try:
            for prompt in prompts:
                directory = run / ("question-" + str(prompt["sequence"]))
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            "Replay observation deadline reached; accepted tasks remain preserved"
                        )
                    path = verify_prompt(prompt)
                    try:
                        receipt = submit(root, directory, path, max_turns, previous)
                    except ValueError as exc:
                        if "healthy idle runtime" not in str(exc):
                            raise
                        atomic(
                            run / "sequence-state.json",
                            encoded(
                                {
                                    "status": "waiting_for_idle",
                                    "sequence": prompt["sequence"],
                                    "observed_at": now(),
                                }
                            ),
                        )
                        time.sleep(poll_seconds)
                        continue
                    summary = inspect(root, directory)
                    detail = json.loads((directory / "replay-observation.json").read_text())
                    state = {
                        "status": "running",
                        "sequence": prompt["sequence"],
                        "task": receipt["task"],
                        "task_status": summary["status"],
                        "astra_calls": summary["astra_calls"],
                        "observed_at": now(),
                    }
                    atomic(run / "sequence-state.json", encoded(state))
                    signature = (state["sequence"], state["task_status"], state["astra_calls"])
                    if signature != last:
                        print(json.dumps(state), flush=True)
                        last = signature
                    if summary["status"] in ("blocked", "cancelled"):
                        raise RuntimeError(
                            "Replay stopped at retained task "
                            + receipt["task"]
                            + ": "
                            + str(detail["task"]["error"])
                        )
                    delivered = (
                        summary["status"] == "completed"
                        and summary["completion_sent"]
                        and all(d["status"] == "sent" for d in detail["deliveries"])
                    )
                    if delivered:
                        completed.append(
                            {
                                "sequence": prompt["sequence"],
                                "task": receipt["task"],
                                "summary": summary,
                            }
                        )
                        previous = receipt["task"]
                        break
                    time.sleep(poll_seconds)
            result = {"status": "completed", "finished_at": now(), "questions": completed}
            atomic(run / "sequence-state.json", encoded(result))
            return result
        except BaseException as exc:
            atomic(
                run / "sequence-failure.json",
                encoded(
                    {
                        "observed_at": now(),
                        "error": str(exc),
                        "completed_questions": completed,
                        "instruction": "Inspect retained receipts; rerun sequence with the same run to resume, never resubmit under a new identity.",
                    }
                ),
            )
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("submit", "inspect", "sequence"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument(
        "--max-astra-calls",
        type=int,
        default=6,
        help="Main decision guard; physical retries remain separately counted",
    )
    parser.add_argument(
        "--after-task",
        help="Continue the same reset session after this replay answer and usage card are delivered",
    )
    parser.add_argument(
        "--intent", type=Path, help="Authorized ordered prompt paths and hashes for sequence"
    )
    args = parser.parse_args()
    if args.action == "submit" and args.prompt_file is None:
        parser.error("submit requires --prompt-file")
    if args.action == "sequence" and args.intent is None:
        parser.error("sequence requires --intent")
    if args.action == "sequence":
        result = sequence(
            args.root.resolve(),
            args.run.resolve(),
            args.intent.resolve(),
            args.max_astra_calls,
            after_task=args.after_task,
        )
    else:
        result = (
            submit(
                args.root.resolve(),
                args.run.resolve(),
                args.prompt_file.resolve(),
                args.max_astra_calls,
                args.after_task,
            )
            if args.action == "submit"
            else inspect(args.root.resolve(), args.run.resolve())
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
