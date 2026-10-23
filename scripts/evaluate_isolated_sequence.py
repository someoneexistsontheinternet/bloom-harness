"""Replay exact ordered prompts in an isolated store, with local delivery only."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
from pathlib import Path

from bloom.config import Config
from bloom.runtime import Runtime
from bloom.service import process_identity
from bloom.store import Store, atomic, encoded, now
from bloom.toolchain import directory, verify


def prepare(root, run, intent_file):
    if not run.is_relative_to(root / "logs") or run == root / "logs":
        raise ValueError("An isolated replay needs a dedicated project log run directory")
    source = Path(os.environ.get("BLOOM_SOURCE_ROOT", root)).resolve()
    specification = json.loads(intent_file.read_text())
    prompts = specification["prompts"]
    if [p["sequence"] for p in prompts] != list(range(1, len(prompts) + 1)) or not prompts:
        raise ValueError("Expected an exact ordered prompt manifest")
    for prompt in prompts:
        if hashlib.sha256(Path(prompt["path"]).read_bytes()).hexdigest() != prompt["sha256"]:
            raise ValueError("Replay prompt changed: " + str(prompt["sequence"]))
    fixture = run / "isolated"
    intent = {
        "root": str(root),
        "isolated": str(fixture),
        "source": str(source),
        "prompts": prompts,
        "scope": "Independent SQLite/context/persona; local delivery only; existing disk corpus is not pristine.",
        "max_astra_calls": 20,
    }
    run.mkdir(parents=True, exist_ok=True)
    saved_intent = run / "intent.json"
    if saved_intent.exists():
        if json.loads(saved_intent.read_text()) != intent:
            raise ValueError("Resume must preserve source, prompts and isolated workspace")
    else:
        if fixture.exists():
            raise ValueError("Unowned isolated directory already exists")
        atomic(saved_intent, encoded(intent))
    marker = fixture / ".replay-prepared"
    if not marker.exists():
        if fixture.exists():
            raise ValueError("Partial preparation retained; inspect before resuming")
        fixture.mkdir()
        # Private operational notes are neither a distribution dependency nor
        # implicit evaluation input. Explicit prompt/source manifests own scope.
        for name in ("config", "prompts", "docs", "src"):
            shutil.copytree(
                source / name,
                fixture / name,
                ignore=shutil.ignore_patterns("local.toml", "__pycache__"),
            )
        for name in ("README.md", "AGENTS.md", "pyproject.toml", "uv.lock"):
            shutil.copyfile(source / name, fixture / name)
        with (fixture / "AGENTS.md").open("a") as f:
            f.write(
                "\n\nIsolated replay scope: this directory owns the current runtime, context and accepted memory. "
                "Production Bloom remains live elsewhere. Read existing source evidence as needed, but perform "
                "all requested memory changes through this runtime’s memory tools. Do not alter production "
                "files/state, stop services, extract archives or send external messages for this replay.\n"
            )
        (fixture / ".venv").symlink_to(root / ".venv", target_is_directory=True)
        shutil.copytree(directory(root), directory(fixture))
        atomic(
            fixture / "config/local.toml", b"[discord]\nenabled=false\n[storage]\nenabled=false\n"
        )
        atomic(marker, b"Prepared independent runtime; production was not reset.\n")
    verify(fixture)
    # Carry provider/model choices in memory only; never copy credential values.
    live = Config(root)
    config = Config(
        fixture,
        {
            "providers": live.data["providers"],
            "profiles": live.data["profiles"],
            "discord": {"enabled": False},
            "storage": {"enabled": False},
        },
    )
    return config, intent


def observe(store, task, sequence):
    row = store.one("SELECT * FROM tasks WHERE id=?", (task,))
    owner = store.one("SELECT id FROM owner_turns WHERE task_id=?", (task,))
    calls = store.rows(
        "SELECT c.id,c.agent_id,c.task_id,c.model_requested,c.purpose,c.status,c.started_at,c.completed_at,c.input_tokens,c.cached_tokens,c.output_tokens FROM model_calls c JOIN model_call_turns o ON o.call_id=c.id WHERE o.turn_id=? ORDER BY c.rowid",
        (owner["id"],),
    )
    breakdown = []
    for call in calls:
        if call["agent_id"] == "astra" and call["purpose"] == "turn":
            tools = store.rows(
                "SELECT c.name,c.operation_id,o.status FROM tool_calls c JOIN items i ON i.id=c.item_id LEFT JOIN operations o ON o.id=c.operation_id WHERE i.model_call_id=? ORDER BY c.rowid",
                (call["id"],),
            )
            updates = store.rows(
                "SELECT text,phase FROM items WHERE model_call_id=? AND kind='message'",
                (call["id"],),
            )
            breakdown.append({**call, "tools": tools, "public_messages": updates})
    final = (
        store.one("SELECT id,text FROM items WHERE id=?", (row["final_item"],))
        if row["final_item"]
        else None
    )
    return {
        "observed_at": now(),
        "sequence": sequence,
        "task": row,
        "calls": calls,
        "astra_calls": len(breakdown),
        "astra_breakdown": breakdown,
        "final": final,
        "workers": store.rows(
            "SELECT id,agent_id,status,turns,error FROM tasks WHERE parent_id=?", (task,)
        ),
        "deliveries": store.rows(
            "SELECT channel,kind,status,external_id FROM deliveries WHERE task_id=?", (task,)
        ),
    }


async def execute(config, intent, run, timeout=1800):
    os.environ["BLOOM_ROOT"] = str(config.root)
    store = Store(config)
    atomic(
        run / "process.json",
        encoded(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "start_identity": process_identity(os.getpid()),
                "started_at": now(),
                "root": str(config.root),
                "stop": "Verify PID/start identity, then SIGINT; only this isolated runtime is affected.",
            }
        ),
    )
    completed = []
    try:
        with store.runtime_lock():
            for prompt in intent["prompts"]:
                sequence = prompt["sequence"]
                task = "isolated-sequence-" + str(sequence)
                text = Path(prompt["path"]).read_text()
                if hashlib.sha256(text.encode()).hexdigest() != prompt["sha256"]:
                    raise ValueError("Prompt changed before execution")
                prior = store.one("SELECT * FROM tasks WHERE id=?", (task,))
                if prior and prior["objective"] != text:
                    raise ValueError("Task identity belongs to another prompt")
                if not prior:
                    task = store.new_task(text, key=task)
                    store.db.execute(
                        "UPDATE tasks SET max_turns=? WHERE id=?", (intent["max_astra_calls"], task)
                    )
                runtime = Runtime(store)
                async with asyncio.timeout(timeout):
                    await runtime.run(until_idle=True)
                result = observe(store, task, sequence)
                atomic(run / f"question-{sequence}.json", encoded(result))
                if result["task"]["status"] != "completed":
                    raise RuntimeError(
                        "Isolated task did not complete: " + str(result["task"]["error"])
                    )
                if any(
                    d["channel"] != "local" or d["status"] != "sent" for d in result["deliveries"]
                ):
                    raise RuntimeError("Replay must use acknowledged local-only delivery")
                completed.append(
                    {
                        "sequence": sequence,
                        "task": task,
                        "astra_calls": result["astra_calls"],
                        "luna_calls": sum(
                            c["model_requested"] == config.profile("luna")["model"]
                            for c in result["calls"]
                        ),
                    }
                )
                atomic(
                    run / "state.json",
                    encoded({"status": "running", "completed": completed, "observed_at": now()}),
                )
                print(json.dumps(completed[-1]), flush=True)
            result = {
                "status": "completed",
                "completed": completed,
                "finished_at": now(),
                "memories": store.rows("SELECT topic,revision,summary,detail_hash FROM memories"),
                "external_deliveries": store.one(
                    "SELECT count(*) n FROM deliveries WHERE channel!='local'"
                )["n"],
            }
            atomic(run / "result.json", encoded(result))
            atomic(run / "state.json", encoded(result))
    except BaseException as exc:
        atomic(
            run / "failure.json", encoded({"at": now(), "error": str(exc), "completed": completed})
        )
        raise
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--intent", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    config, intent = prepare(args.root.resolve(), args.run.resolve(), args.intent.resolve())
    if args.prepare_only:
        print(
            json.dumps(
                {"prepared": str(config.root), "discord_enabled": config.data["discord"]["enabled"]}
            )
        )
    else:
        asyncio.run(execute(config, intent, args.run.resolve()))


if __name__ == "__main__":
    main()
