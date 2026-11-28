"""Controlled reader-effort evaluation with immutable requests and independent samples.

Input is a local JSON protocol containing cases, questions, source paths, native
images, and a source-backed scoring rubric. Runtime evidence stays outside Git.
Only reasoning.effort changes within a case. Explicit repetitions get distinct
purchase IDs and bypass result memoization; uncertain purchases are never retried.
"""

import argparse
import asyncio
import base64
import copy
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import random
import re
import signal
import socket
import sys

from bloom41.agents import Actor, resource_directory
from bloom41.evidence import NumberedSource, READER_INSTRUCTION
from bloom41.models import Models, Responses, final_text
from bloom41.store import Store, atomic, digest, encoded, now
from bloom41.tool import file_media


EFFORTS = ("medium", "high", "xhigh", "max")


def request_for(case):
    source = Path(case["source_path"]).read_bytes()
    assert digest(source) == case["source_sha256"]
    text = source.decode()
    document = NumberedSource.index(text)
    windows = list(document.windows(0, 32_000))
    if len(windows) != 1:
        raise ValueError("Each evaluation case must fit one complete 32k-token reader window")
    window = windows[0]
    content = [
        {"type": "input_text", "text": READER_INSTRUCTION},
        {
            "type": "input_text",
            "text": json.dumps(
                {
                    "question": case["question"],
                    "source_id": "source:" + case["source_sha256"],
                    "source_sha256": case["source_sha256"],
                    "context": case["context"],
                    "range_start_char": 0,
                    "range_end_char": len(text),
                    **window.metadata(),
                }
            ),
        },
        {"type": "input_text", "text": window.render()},
    ]
    for image in case["images"]:
        raw = Path(image["path"]).read_bytes()
        assert digest(raw) == image["sha256"]
        content.extend(
            [
                {"type": "input_text", "text": "Native image source: source:" + image["sha256"]},
                {
                    "type": "input_image",
                    "image_url": "data:"
                    + file_media(image["path"])
                    + ";base64,"
                    + base64.b64encode(raw).decode(),
                    "detail": "high",
                },
            ]
        )
    return {
        "model": "gpt-6-luna",
        "instructions": (resource_directory("profiles") / "luna.md").read_text(),
        "input": [{"role": "user", "content": content}],
        "tools": [],
        "store": False,
        "stream": True,
        "reasoning": {"effort": "medium"},
        "include": ["reasoning.encrypted_content"],
    }


def prepare(protocol, destination):
    if (
        protocol["efforts"] != list(EFFORTS)
        or type(protocol["repeats"]) is not int
        or protocol["repeats"] < 1
    ):
        raise ValueError("Specify the four supported efforts and a positive repetition count")
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Use a fresh experiment directory")
    destination.mkdir(parents=True)
    atomic(destination / "protocol.json", encoded(protocol))
    store = Store(destination / "workspace")
    templates = {}
    try:
        for case in protocol["cases"]:
            templates[case["id"]] = store.put(request_for(case))
        sequence = []
        rng = random.Random(protocol["random_seed"])
        blind_ids = [
            f"sample-{n:02d}" for n in range(1, len(templates) * 4 * protocol["repeats"] + 1)
        ]
        rng.shuffle(blind_ids)
        waves = [(case, repeat) for repeat in range(protocol["repeats"]) for case in templates]
        rng.shuffle(waves)
        for wave, (case, repeat) in enumerate(waves):
            efforts = list(EFFORTS)
            rng.shuffle(efforts)
            for effort in efforts:
                body = copy.deepcopy(store.json(templates[case]))
                body["reasoning"]["effort"] = effort
                ident = blind_ids.pop()
                sequence.append(
                    {
                        "id": ident,
                        "case": case,
                        "repeat": repeat + 1,
                        "wave": wave,
                        "effort": effort,
                        "request": store.put(body),
                    }
                )
        manifest = {
            "prepared_at": now(),
            "protocol_sha256": digest(encoded(protocol)),
            "templates": templates,
            "samples": sequence,
        }
        atomic(destination / "manifest.json", encoded(manifest))
        store.event("ablation.prepared", "experiment", manifest, key="ablation-prepared")
        return manifest
    finally:
        store.close()


def normalized(request):
    request = copy.deepcopy(request)
    request["reasoning"].pop("effort")
    return encoded(request)


def validate_packet(case, response):
    text = final_text(response).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", text, re.S)
    result = json.loads(fenced.group(1) if fenced else text)
    if (
        type(result.get("relevant")) is not bool
        or not isinstance(result.get("summary"), str)
        or not isinstance(result.get("passages"), list)
        or not isinstance(result.get("summaries", []), list)
    ):
        raise ValueError("Invalid reader response shape")
    source = Path(case["source_path"]).read_bytes()
    assert digest(source) == case["source_sha256"]
    source_text = source.decode()
    window = NumberedSource.index(source_text).window(0, len(source_text))
    view = window.view(result, "source:" + case["source_sha256"])
    if result["relevant"] and not view["passages"]:
        raise ValueError("Relevant reader response has no valid quoted ranges")
    if not result["relevant"] and result["passages"]:
        raise ValueError("Irrelevant reader response proposes quotes")
    return {
        "raw_selection": result,
        "validated": view,
        "source_characters": len(source_text),
        "quoted_characters": sum(len(p["text"]) for p in view["passages"]),
    }


async def execute(destination, provider):
    destination = Path(destination)
    manifest = json.loads((destination / "manifest.json").read_bytes())
    protocol = json.loads((destination / "protocol.json").read_bytes())
    assert digest(encoded(protocol)) == manifest["protocol_sha256"]
    store = Store(destination / "workspace")
    lock = (store.control / "runtime.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    models = Models(store, provider)
    if store.events("model.started") or store.events("ablation.started"):
        raise ValueError(
            "This experiment already started; inspect receipts instead of repurchasing"
        )
    models.grant(len(manifest["samples"]))
    cases = {case["id"]: case for case in protocol["cases"]}
    for sample in manifest["samples"]:
        expected = store.json(manifest["templates"][sample["case"]])
        actual = store.json(sample["request"])
        assert normalized(actual) == normalized(expected)
        assert actual["reasoning"]["effort"] == sample["effort"]
    process = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": now(),
        "interpreter": sys.executable,
        "argv": sys.argv,
        "status": "running",
        "protocol_sha256": manifest["protocol_sha256"],
        "stop": "Recheck process identity/start; send SIGINT. Accepted requests remain recorded and are not retried.",
    }
    atomic(destination / "process.json", encoded(process))
    store.event("ablation.started", "experiment", process, key="ablation-started")
    stop = asyncio.Event()
    active = []
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop.set)

    async def one(sample):
        case = cases[sample["case"]]
        try:
            response = await models.call(
                Actor("reader-evaluation"),
                store.json(sample["request"]),
                call_id=sample["id"],
                purpose="effort_ablation",
                profile="luna",
                origin=sample["case"],
            )
        except Exception as exc:
            packet = {
                "error": provider.clean(type(exc).__name__ + ": " + str(exc))
                if hasattr(provider, "clean")
                else str(exc)
            }
        else:
            try:
                packet = validate_packet(case, response)
            except Exception as exc:
                packet = {"validation_error": str(exc), "raw_text": final_text(response)}
        atomic(
            destination / "blind" / (sample["id"] + ".json"),
            encoded({"id": sample["id"], "case": sample["case"], **packet}),
        )

    try:
        for wave in sorted({s["wave"] for s in manifest["samples"]}):
            if stop.is_set():
                break
            samples = [s for s in manifest["samples"] if s["wave"] == wave]
            active = [asyncio.create_task(one(s)) for s in samples]
            print(
                json.dumps({"wave": wave + 1, "case": samples[0]["case"], "status": "running"}),
                flush=True,
            )
            await asyncio.gather(*active)
            print(
                json.dumps(
                    {
                        "wave": wave + 1,
                        "terminal_requests": len(store.events("model.completed"))
                        + len(store.events("model.failed")),
                    }
                ),
                flush=True,
            )
        process.update(status="stopped" if stop.is_set() else "completed", ended_at=now())
    finally:
        for task in active:
            if not task.done():
                task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        if process["status"] == "running":
            process.update(status="interrupted", ended_at=now())
        process.update(
            started_requests=len(store.events("model.started")),
            completed_requests=len(store.events("model.completed")),
            failed_requests=len(store.events("model.failed")),
        )
        atomic(destination / "process.json", encoded(process))
        store.close()
        lock.close()
    return process


def metrics(destination):
    destination = Path(destination)
    manifest = json.loads((destination / "manifest.json").read_bytes())
    store = Store(destination / "workspace")
    values = []
    try:
        for sample in manifest["samples"]:
            start = store.by_key("model-started:" + sample["id"])
            end = store.by_key("model-completed:" + sample["id"])
            failure = store.by_key("model-failed:" + sample["id"])
            usage = end["data"].get("usage") or {} if end else {}
            finished = end or failure
            values.append(
                {
                    **{k: sample[k] for k in ("id", "case", "repeat", "wave", "effort")},
                    "status": "completed"
                    if end
                    else "failed"
                    if failure
                    else "unsettled"
                    if start
                    else "unstarted",
                    "seconds": (
                        datetime.fromisoformat(finished["at"]) - datetime.fromisoformat(start["at"])
                    ).total_seconds()
                    if start and finished
                    else None,
                    "input_tokens": usage.get("input_tokens"),
                    "cached_input_tokens": usage.get("input_tokens_details", {}).get(
                        "cached_tokens"
                    ),
                    "output_tokens": usage.get("output_tokens"),
                    "reasoning_tokens": usage.get("output_tokens_details", {}).get(
                        "reasoning_tokens"
                    ),
                    "failure": failure["data"]["error"] if failure else None,
                }
            )
        atomic(destination / "metrics.json", encoded(values))
        return values
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("protocol", type=Path)
    prep.add_argument("destination", type=Path)
    run = sub.add_parser("run")
    run.add_argument("destination", type=Path)
    report = sub.add_parser("metrics")
    report.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        result = prepare(json.loads(args.protocol.read_bytes()), args.destination)
        print(
            json.dumps(
                {
                    "prepared_samples": len(result["samples"]),
                    "protocol_sha256": result["protocol_sha256"],
                }
            )
        )
    elif args.mode == "run":
        provider = Responses("codex", timeout=90, deadline=240)
        provider.headers()  # Check credentials without printing or storing them.
        result = asyncio.run(execute(args.destination, provider))
        print(json.dumps(result))
        return int(result["failed_requests"] > 0 or result["status"] != "completed")
    else:
        result = metrics(args.destination)
        print(json.dumps({"samples": len(result), "path": str(args.destination / "metrics.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
