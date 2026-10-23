"""Opt-in retained-web integration: configured curation and Astra compaction, no delivery."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
from pathlib import Path

from bloom.config import Config
from bloom.context import Context
from bloom.models import Models
from bloom.reset import reader
from bloom.results import text_of
from bloom.runtime import Runtime
from bloom.store import Store, atomic, encoded, now


async def evaluate(
    project,
    output,
    database,
    source_task,
    resume=False,
    revalidate=False,
    operation_id=None,
    curation_only=False,
):
    output.mkdir(parents=True, exist_ok=resume)
    root = output / "project"
    if not root.exists():
        root.mkdir()
        for name in ("config", "prompts"):
            shutil.copytree(
                project / name, root / name, ignore=shutil.ignore_patterns("local.toml")
            )
        (root / ".venv").symlink_to(project / ".venv", target_is_directory=True)
    elif not resume:
        raise ValueError("Use --resume for the same evidence check")
    config = Config(root, {"discord": {"enabled": False}, "storage": {"enabled": False}})
    for provider in config.data["providers"].values():
        provider["max_retries"] = 0
    store = Store(config)
    runtime = Runtime(store)
    report = {
        "started_at": now(),
        "source_database": str(database),
        "source_task": source_task,
        "source_manifest": {
            str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (project / "src/bloom").glob("*.py")
        },
        "external_posts": 0,
        "results": [],
    }
    try:
        if (output / "result.json").exists():
            old = json.loads((output / "result.json").read_text())
            if old["source_database"] != str(database) or old["source_task"] != source_task:
                raise ValueError("Cannot resume different evidence")
            atomic(
                output / ("attempt-" + old["completed_at"].replace(":", "-") + ".json"),
                encoded(old),
            )
            if old.get("accepted"):
                report = old
                return report
        with reader(Config(project), database) as original:
            source = original.one("SELECT * FROM tasks WHERE id=?", (source_task,))
            if not source:
                raise ValueError("Source task missing")
            prior = original.rows(
                "SELECT i.id,i.text,i.created_at FROM items i JOIN item_origins o ON o.item_id=i.id WHERE o.kind IN ('owner','owner_steering') AND i.session_id='astra' AND i.id<=(SELECT max(id) FROM items WHERE task_id=?) ORDER BY i.id DESC LIMIT 5",
                (source_task,),
            )
            question = (
                "Retained research validation. These are the actual recent owner messages, oldest first:\n"
                + json.dumps(list(reversed(prior)), ensure_ascii=False)
            )
            existing = store.one("SELECT id,objective FROM tasks ORDER BY created_at LIMIT 1")
            if existing and existing["objective"] != question:
                raise ValueError("Fixture question changed")
            task = existing["id"] if existing else store.new_task(question)
            store.status(task, "running")
            ops = original.rows(
                "SELECT o.*,t.id tool_id,t.arguments_hash FROM operations o JOIN tool_calls t ON t.operation_id=o.id WHERE o.task_id=? AND o.name IN ('web_search','web_open') AND o.status='completed' ORDER BY o.started_at",
                (source_task,),
            )
            if operation_id:
                ops = [op for op in ops if op["id"] == operation_id]
            if not ops:
                raise ValueError("No completed retained browser operations")
            for index, operation in enumerate(ops):
                raw = json.loads(original.bytes(operation["result_hash"], touch=False, cache=False))
                for picture in raw.get("_images", []):
                    assert (
                        store.put(
                            original.bytes(picture["hash"], touch=False, cache=False),
                            picture["mime"],
                        )
                        == picture["hash"]
                    )
                # Saved operation receipts are the exact native backend result.
                arguments = json.loads(
                    original.bytes(operation["arguments_hash"], touch=False, cache=False)
                )
                ident = f"fixture-capture-{index}"
                native = {
                    "type": "function_call",
                    "call_id": ident,
                    "name": operation["name"],
                    "arguments": json.dumps(arguments),
                }
                response = {"output": [native]}
                store.db.execute(
                    "INSERT OR IGNORE INTO model_calls(id,task_id,agent_id,purpose,model_requested,provider,status,response_hash,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        ident,
                        task,
                        "astra",
                        "fixture",
                        "retained-operation",
                        "fixture",
                        "completed",
                        store.put(response),
                        now(),
                    ),
                )
                if not store.one("SELECT 1 FROM tool_calls WHERE id=?", (ident + ":" + ident,)):
                    runtime.ingest_response(task, ident, response)

                async def captured(*args, value=raw, **kwargs):
                    return value

                runtime.tools.run = captured
                tool_id = ident + ":" + ident
                if not store.one("SELECT 1 FROM tool_results WHERE call_id=?", (tool_id,)):
                    await runtime.execute(
                        task, store.one("SELECT * FROM tool_calls WHERE id=?", (tool_id,))
                    )
                result = store.one(
                    "SELECT i.* FROM tool_results r JOIN items i ON i.id=r.item_id WHERE r.call_id=?",
                    (tool_id,),
                )
                envelope = store.get(result["envelope_hash"])
                file = store.one("SELECT * FROM tool_result_files WHERE tool_id=?", (tool_id,))
                additional = None
                if file and file["status"] == "unable" and revalidate:
                    # Explicit diagnostic retry: preserve the prior tool envelope
                    # and provider receipts, append the newly validated view.
                    atomic(
                        output / (tool_id.replace(":", "-") + "-before-revalidation.json"),
                        encoded(file),
                    )
                    store.db.execute(
                        "UPDATE tool_result_files SET status='prepared',summary_hash=NULL WHERE operation_id=?",
                        (file["operation_id"],),
                    )
                    presented = await runtime.results.present(
                        "astra",
                        task,
                        store.one("SELECT * FROM tool_calls WHERE id=?", (tool_id,)),
                        raw,
                        store.put(raw),
                    )
                    additional = {
                        "role": "user",
                        "content": json.dumps(
                            {"retained_source_revalidation": presented}, ensure_ascii=False
                        ),
                    }
                    store.item(
                        "astra",
                        additional,
                        task,
                        key="revalidated:" + file["operation_id"],
                        origin="runtime_notice",
                    )
                    file = store.one("SELECT * FROM tool_result_files WHERE tool_id=?", (tool_id,))
                exact = text_of(raw)[0]
                if len(exact) > runtime.results.threshold and not file:
                    raise AssertionError("Large browser result bypassed curation")
                summary = store.get(file["summary_hash"]) if file else {"status": "not_required"}
                if file:
                    assert file["status"] in ("found", "partial", "not_found", "unable"), summary
                    assert Path(file["path"]).read_text() == exact
                text = json.dumps(envelope, ensure_ascii=False)
                images = (
                    [
                        x
                        for x in envelope.get("output", [])
                        if isinstance(x, dict) and x.get("type") == "input_image"
                    ]
                    if isinstance(envelope.get("output"), list)
                    else []
                )
                expected_images = len(raw.get("_images", [])) + (1 if raw.get("_image") else 0)
                assert len(images) == expected_images, (len(images), expected_images)
                estimate = runtime.context.count(envelope) + (
                    runtime.context.count(additional) if additional else 0
                )
                text_parts = (
                    "\n".join(x.get("text", "") for x in envelope["output"])
                    if isinstance(envelope.get("output"), list)
                    else envelope.get("output", "")
                )
                if file:
                    assert len(text_parts) < runtime.results.threshold
                report["results"].append(
                    {
                        "operation": operation["id"],
                        "name": operation["name"],
                        "raw_characters": len(exact),
                        "raw_text_tokens": runtime.context.count(exact),
                        "model_tokens_with_images": estimate,
                        "curation_status": summary["status"],
                        "curation": summary,
                        "images": len(images),
                        "original_artifact": operation["result_hash"],
                        "saved_text": file["path"] if file else None,
                    }
                )
                atomic(output / "progress.json", encoded(report))
                print(
                    json.dumps(
                        {
                            k: report["results"][-1][k]
                            for k in (
                                "name",
                                "raw_characters",
                                "model_tokens_with_images",
                                "images",
                                "curation_status",
                            )
                        }
                    ),
                    flush=True,
                )
        context = runtime.context
        assert any(r["curation_status"] in ("found", "partial") for r in report["results"])
        if curation_only:
            assert all(r["curation_status"] != "unable" for r in report["results"])
            report.update(
                accepted=True,
                scope="Configured retained-source curation only; no compaction/answer inference",
                permanent_memory_unchanged=not store.rows("SELECT * FROM memory_facts"),
            )
            return report
        accepted_before = {
            table: store.rows("SELECT * FROM " + table)
            for table in ("memories", "memory_facts", "memory_fact_revisions")
        }
        originals = {r["id"]: r for r in store.rows("SELECT * FROM items")}
        # Reproduce a rejected permanent-memory proposal without asking Luna to repair/publish it.
        bad = store.put({"updates": [{"text": "x" * 2573}]})
        store.db.execute(
            "INSERT OR IGNORE INTO memory_batches VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "retained-rejected-proposal",
                "astra",
                task,
                bad,
                "retained-unpublished",
                "failed",
                0,
                0,
                bad,
                "Fact needs bounded text and an exact evidence quote",
                now(),
            ),
        )
        report["working_context"] = await context.compact("astra", task)
        assert accepted_before == {
            table: store.rows("SELECT * FROM " + table) for table in accepted_before
        }
        assert all(
            store.one("SELECT * FROM items WHERE id=?", (i,)) == r for i, r in originals.items()
        )
        selected = context.selected("astra", task)
        assert selected and all(r["kind"] == "message" for r in selected)
        first = await context.compose("astra", task, [])
        second = await Context(store, runtime.models).compose("astra", task, [])
        assert first == second
        call, response = await runtime.models.infer(
            "astra",
            task,
            first[0]
            + "\nThis is an isolated integration check. Briefly summarize the retained research and its limitations; do not take actions.",
            first[1],
            [],
            retry_limit=0,
        )
        assert any(x.get("type") == "message" for x in response.get("output", []))
        assert not any(x.get("type") == "function_call" for x in response.get("output", []))
        report.update(
            accepted=True,
            answer_call=call,
            working_tokens=context.count(first[1]),
            permanent_memory_unchanged=True,
            originals_unchanged=True,
            stable_prefix=True,
            zero_native_tail=True,
        )
    except BaseException as exc:
        from bloom.privacy import clean

        report.update(accepted=False, error=clean(str(exc)), error_type=type(exc).__name__)
        raise
    finally:
        report["completed_at"] = now()
        report["calls"] = store.rows(
            "SELECT id,purpose,model_requested,status,input_tokens,cached_tokens,output_tokens,error FROM model_calls WHERE purpose!='fixture' ORDER BY rowid"
        )
        atomic(output / "result.json", encoded(report))
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--revalidate",
        action="store_true",
        help="Explicitly validate retained failed reviewer responses and append any corrected evidence; keep originals",
    )
    parser.add_argument(
        "--operation",
        help="Evaluate one retained operation rather than the task's complete web sequence",
    )
    parser.add_argument(
        "--curation-only",
        action="store_true",
        help="Require usable configured curation; skip already independently verified compaction",
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required for configured inference")
    asyncio.run(
        evaluate(
            args.project.resolve(),
            args.output.resolve(),
            args.database.resolve(),
            args.task,
            args.resume,
            args.revalidate,
            args.operation,
            args.curation_only,
        )
    )
