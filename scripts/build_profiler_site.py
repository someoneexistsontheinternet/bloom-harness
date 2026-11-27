"""Build the lane/event/inspector viewer from a verified Bloom v4.1 receipt trace."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil

from profile_run import Trace, public


def iso(us):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=us)).isoformat(
        timespec="microseconds"
    )


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def build(trace, out):
    out = Path(out).resolve()
    if out.is_relative_to(trace.root):
        raise ValueError("Viewer output must be outside the original runtime")
    out.mkdir(parents=True, exist_ok=True)
    if (out / "v41-graph.json").exists() and json.loads((out / "v41-graph.json").read_text())[
        "run"
    ] != trace.run_directory.name:
        raise ValueError("Output directory belongs to another recorded run")
    roots = (trace.root, trace.run_directory.parents[2])
    models = {e["data"]["id"]: e for e in trace.kinds["model.started"]}
    branches = {e["subject"]: e["data"] for e in trace.kinds["branch.created"]}
    messages = {e["subject"]: e["data"] for e in trace.kinds["message.prepared"]}
    web_operations = {e["data"]["operation"]: e for e in trace.kinds["web.started"]}
    lanes = {}
    short_names = {"astra": "Astra", "company-catalysts": "Catalysts", "market-verifier": "Markets"}
    branch_names = {
        name: f"{short_names.get(data.get('owner'), data.get('owner', 'Owner'))} · fork {i + 1}"
        for i, (name, data) in enumerate(branches.items())
    }

    def actor_label(actor):
        if actor.startswith("terra-"):
            return "Terra · " + short_names.get(actor[6:], actor[6:].replace("-", " "))
        return branch_names.get(actor, short_names.get(actor, actor.replace("-", " ").capitalize()))

    def actor_lane(actor):
        role = (
            "Fork"
            if actor in branches
            else "Terra"
            if actor.startswith("terra-")
            else "Astra"
            if actor == "astra"
            else "Sol"
        )
        lanes.setdefault(actor, {"id": actor, "label": actor_label(actor), "role": role})
        return actor

    for e in trace.kinds["agent.created"]:
        if e["subject"] not in ("user", "system"):
            actor_lane(e["subject"])
    for actor in branches:
        actor_lane(actor)
    for ident, label, role in (
        ("luna", "Luna / readers", "Luna"),
        ("web", "Browser", "Web"),
        ("captures", "Captures", "Web"),
        ("inbox", "Inbox", "Inbox"),
        ("memory", "Context", "Runtime"),
        ("runtime", "Harness", "Runtime"),
    ):
        lanes[ident] = {"id": ident, "label": label, "role": role}

    model_by_turn = {ident: event["seq"] for ident, event in models.items()}
    aliases = {ident: e["seq"] for ident, e in models.items()}
    aliases.update({e["subject"]: e["seq"] for e in trace.kinds["branch.created"]})
    records, events, event_by_id = {}, [], {}
    detail_paths = {}
    for ident, item in trace.details.items():
        payload = dict(item)
        for key in ("request_ref", "response_ref", "result_ref", "body_ref"):
            if payload.get(key):
                payload[key.removesuffix("_ref")] = trace.object(payload[key])
        name = "v41-details/" + hashlib.sha256(ident.encode()).hexdigest()[:24] + ".json"
        write_json(out / name, public(payload, roots))
        detail_paths[ident] = name

    for row in trace.events:
        d, kind, subject = row["data"], row["kind"], row["subject"]
        job_id = (
            subject
            if kind.startswith("job.")
            else d.get("job")
            or web_operations.get(d.get("operation"), {}).get("data", {}).get("job")
        )
        job = trace.jobs.get(job_id, {}) if isinstance(job_id, str) else {}
        package = messages.get(subject, {}) if kind.startswith("message.") else {}
        metadata = package.get("metadata", {})
        actor = (
            d.get("branch")
            or d.get("agent")
            or d.get("scope")
            or job.get("branch")
            or job.get("agent")
            or subject
        )
        turn = d.get("turn") or metadata.get("turn") or job.get("turn")
        call = (
            d.get("id")
            if kind.startswith("model.")
            else d.get("call")
            if kind == "reader.sources"
            else turn
        )
        model = models.get(call, {})
        scope = (
            turn
            or (call if model else None)
            or metadata.get("request_id")
            or d.get("request_id")
            or ""
        )
        label = kind.replace(".", " · ").replace("_", " ")
        detail = []
        lane = (
            actor_lane(actor)
            if actor in lanes or actor in branches or kind.startswith(("model.", "turn.", "job."))
            else "runtime"
        )
        who = actor_label(actor) if actor in lanes else actor
        if kind.startswith("model."):
            source = models[d["id"]]["data"]
            if source["purpose"] == "tool_reader":
                lane = "luna"
            purpose = {"agent": "decision", "compact": "compaction", "tool_reader": "reader"}.get(
                source["purpose"], source["purpose"]
            )
            label = source["profile"].title() + " " + purpose + " · " + kind.split(".")[1]
            scope = d["id"]
            detail.append(
                ["Native request and eventual response", detail_paths["model:" + d["id"]]]
            )
        elif kind.startswith("job."):
            label = (
                job.get("name", "tool").replace("_", " ")
                + " · "
                + (d.get("status") or "owner notified")
            )
            aliases.setdefault(subject, row["seq"])
            if "job:" + subject in detail_paths:
                detail.append(["Invocation and eventual result", detail_paths["job:" + subject]])
        elif kind.startswith("message."):
            lane = "inbox"
            if package:
                who = actor_label(package["sender"]) + " → " + actor_label(package["recipient"])
                label = who + " · " + kind.split(".")[1]
                if kind in ("message.prepared", "message.delivered", "message.consumed"):
                    detail.append(["Message package", detail_paths["message:" + subject]])
            aliases.setdefault(subject, row["seq"])
        elif kind.startswith("web."):
            lane = "captures" if kind in ("web.file", "web.source") else "web"
            if kind == "web.source":
                page = trace.capture_manifests[d["source_id"]]
                label = "Capture · " + (page.get("title") or page.get("url") or d["source_id"])
                detail.append(
                    ["Captured source manifest", detail_paths["capture:" + d["source_id"]]]
                )
                aliases.setdefault(d["source_id"], row["seq"])
        elif kind.startswith("service."):
            lane = "web"
            ident = row["key"].rsplit(":", 1)[0]
            if ident in detail_paths:
                detail.append(["Service invocation and result", detail_paths[ident]])
        elif kind.startswith(("context.", "memory.", "history.", "attachment.")):
            lane = "memory"
        elif kind == "turn.received":
            label = "Receive " + str(len(d.get("inputs", []))) + " input messages"
            aliases.setdefault(d["turn"], row["seq"])
        elif kind == "turn.state":
            label = "Turn · " + d["status"]
            scope = subject
            call = subject
        event = {
            "n": row["seq"],
            "u": row["us"] - trace.first,
            "k": kind,
            "l": lane,
            "t": scope,
            "who": who,
            "label": label,
            "model": model_by_turn.get(call),
            "error": kind.endswith(".failed")
            or d.get("status") in ("failed", "uncertain", "blocked"),
            "details": detail,
        }
        events.append(event)
        event_by_id[event["n"]] = event
        records[str(event["n"])] = {
            k: row[k] for k in ("seq", "at", "kind", "subject", "data", "key", "digest")
        }

    spans, span_map = [], {}
    derived = 100000
    for source in trace.spans:
        category = source["category"]
        if source["start_us"] == source["end_us"]:
            if source["start_event"] in event_by_id and source["end_event"] in event_by_id:
                span_map[source["id"]] = {"a": source["start_event"], "b": source["end_event"]}
            continue
        category_kind = (
            "model"
            if category in ("Model decision", "Luna / reader", "Compaction")
            else "task"
            if category in ("Turn", "Runner", "Service RPC")
            else "tool"
            if category in ("Tool execution", "Browser capture", "Browser service", "Service RPC")
            else "wait"
        )
        actor = source["actor"]
        lane = (
            "luna"
            if category == "Luna / reader"
            else "captures"
            if category == "Browser capture"
            else "web"
            if category in ("Browser service", "Service RPC")
            else "inbox"
            if category == "Inbox wait"
            else "memory"
            if category in ("Context preparation", "Result delivery")
            else "runtime"
            if category in ("Operator pause", "Runner")
            else actor_lane(actor)
        )
        boundaries = []
        for phase, field, time in (
            ("start", "start_event", "start_us"),
            ("end", "end_event", "end_us"),
        ):
            n = source[field]
            if n is None:
                derived += 1
                n = derived
                event = {
                    "n": n,
                    "u": source[time] - trace.first,
                    "k": "timer." + phase,
                    "l": lane,
                    "t": "",
                    "who": actor_label(actor),
                    "label": source["name"] + " · " + phase,
                    "derived": True,
                    "details": [],
                }
                events.append(event)
                event_by_id[n] = event
                records[str(n)] = {
                    "record_type": "derived timer boundary",
                    "at": iso(source[time]),
                    "source_span": source["id"],
                    "phase": phase,
                    "provenance": source["attributes"],
                }
            boundaries.append(n)
        span = {
            "id": source["id"],
            "a": boundaries[0],
            "b": boundaries[1],
            "u": source["start_us"] - trace.first,
            "v": source["end_us"] - trace.first,
            "l": lane,
            "k": category_kind,
            "label": source["name"],
            "category": category,
            "measurement": source["attributes"].get(
                "measurement",
                "Prepared invocation to launch intent. The tool has not begun execution yet."
                if category == "Tool queue"
                else "Recorded elapsed interval; may include asynchronous waiting. Not CPU time.",
            ),
        }
        spans.append(span)
        span_map[source["id"]] = span

    edges = set()

    def edge(a, b, reason):
        if a != b and a in event_by_id and b in event_by_id:
            edges.add((a, b, reason))

    for link in trace.edges:
        a, b = span_map.get(link["source"]), span_map.get(link["target"])
        if not a or not b:
            continue
        reason = link["relation"]
        end = reason in ("requested tool", "result notification")
        edge(
            a["b"] if end else a["a"], b["b"] if reason == "result notification" else b["a"], reason
        )
    for span in spans:
        edge(span["a"], span["b"], "paired " + span["category"].lower())
    latest_state = {}
    received = {}
    for row in trace.events:
        d, kind, n, subject = row["data"], row["kind"], row["seq"], row["subject"]
        if kind in ("job.state", "turn.state"):
            key = (kind, subject)
            if key in latest_state:
                edge(latest_state[key], n, "next recorded state")
            latest_state[key] = n
        if kind == "turn.received":
            received[d["turn"]] = n
            for message in d.get("inputs", []):
                delivery = trace.keys.get("message-delivered:" + message)
                if delivery:
                    edge(delivery["seq"], n, "input admitted to turn")
        if kind == "model.started" and d["id"] in received:
            edge(received[d["id"]], n, "prepare model request")
        if kind == "reader.sources":
            edge(model_by_turn.get(d["call"]), n, "reader source receipt")
        if kind in ("service.started", "web.started") and "job:" + str(d.get("job")) in span_map:
            edge(
                span_map["job:" + d["job"]]["a"],
                n,
                "job invokes " + ("browser" if kind == "web.started" else "service"),
            )
        if (
            kind.startswith("web.")
            and kind != "web.started"
            and d.get("operation") in web_operations
        ):
            edge(web_operations[d["operation"]]["seq"], n, "recorded browser operation")
        if kind == "message.delivered":
            prepared = trace.keys.get("message-prepare:" + subject)
            if prepared:
                edge(prepared["seq"], n, "atomic message delivery")
        if kind == "context.requested":
            for message in d["original_ids"]:
                delivery = trace.keys.get("message-delivered:" + message)
                if delivery:
                    edge(delivery["seq"], n, "request context for input")
    for ident in trace.jobs:
        curated = "curate_" + hashlib.sha256(ident.encode()).hexdigest()[:24]
        if "job:" + curated in span_map and "job:" + ident in span_map:
            edge(
                span_map["job:" + ident]["b"],
                span_map["job:" + curated]["a"],
                "extract result before delivery",
            )
            notification = trace.keys.get("job-notified:" + ident)
            if notification:
                edge(
                    span_map["job:" + curated]["b"],
                    notification["seq"],
                    "prepared result delivered",
                )

    events.sort(key=lambda e: (e["u"], e["n"]))
    chunks = defaultdict(dict)
    for event in events:
        chunk = event["n"] // 200
        event["record"] = f"v41-records/{chunk:05}.json"
        chunks[chunk][str(event["n"])] = records[str(event["n"])]
    for chunk, records in chunks.items():
        write_json(out / f"v41-records/{chunk:05}.json", public(records, roots))

    views = {}
    for case in trace.cases:
        start, end = case["start_us"] - trace.first, case["end_us"] - trace.first
        inside = [s for s in spans if s["u"] >= start and s["v"] <= end]
        model_spans = [s for s in inside if s["k"] == "model"]
        queue = sorted(
            [s for s in inside if s["category"] == "Tool queue"],
            key=lambda s: s["v"] - s["u"],
            reverse=True,
        )
        longest = max(model_spans, key=lambda s: s["v"] - s["u"], default=None)
        landmarks = [[case["input_event"], "Question received"]]
        if longest:
            landmarks.append([longest["a"], "Longest model call"])
        if queue:
            landmarks.append([queue[0]["a"], "Longest tool queue"])
        landmarks.append([case["answer_event"], "Answer delivered"])
        views[case["id"]] = {
            "label": case["id"].upper(),
            "start": iso(case["start_us"]),
            "end": iso(case["end_us"]),
            "lo": start,
            "hi": end,
            "firstReceipt": 1 if not views else case["input_event"],
            "lastReceipt": case["answer_event"],
            "initial": case["input_event"],
            "brief": case["id"].upper()
            + " · question received to answer delivered. Full run includes later background work.",
            "landmarks": landmarks,
        }
    full_end = max(trace.last, max(s["end_us"] for s in trace.spans))
    views["full"] = {
        "label": "Full run",
        "start": iso(trace.first),
        "end": iso(full_end),
        "lo": 0,
        "hi": full_end - trace.first,
        "firstReceipt": 1,
        "lastReceipt": trace.events[-1]["seq"],
        "initial": trace.cases[0]["input_event"] if trace.cases else trace.events[0]["seq"],
        "brief": "Both questions, setup, restarts and post-answer background work. All recorded requests are included.",
        "landmarks": [item for view in views.values() for item in view["landmarks"]],
    }
    roles = {
        "Astra": 0,
        "Sol": 1,
        "Fork": 2,
        "Terra": 3,
        "Luna": 4,
        "Web": 5,
        "Inbox": 6,
        "Runtime": 7,
    }
    ordered_lanes = sorted(lanes.values(), key=lambda lane: (roles[lane["role"]], lane["label"]))
    dataset = public(
        {
            "schema": 1,
            "run": trace.run_directory.name,
            "views": views,
            "events": events,
            "spans": spans,
            "lanes": ordered_lanes,
            "edges": [list(edge) for edge in sorted(edges)],
            "aliases": list(aliases.items()),
            "models": {
                str(e["seq"]): {
                    "file": detail_paths["model:" + ident],
                    "end": span_map["model:" + ident]["b"],
                }
                for ident, e in models.items()
            },
        },
        roots,
    )
    write_json(out / "v41-graph.json", dataset)
    source = Path(__file__).with_name("profiler")
    script = "\n".join(
        (source / name).read_text() for name in ("timeline.mjs", "explorer.mjs", "load.mjs")
    )
    (out / "v41-explorer.js").write_text(script)
    shutil.copy2(source / "style.css", out / "v41-style.css")
    shutil.copy2(source / "bloom.svg", out / "bloom.svg")
    page = (
        (source / "page.html").read_text().replace("__PANEL__", (source / "panel.html").read_text())
    )
    for name, token in (
        ("v41-style.css", "__STYLE_HASH__"),
        ("v41-explorer.js", "__SCRIPT_HASH__"),
    ):
        page = page.replace(token, hashlib.sha256((out / name).read_bytes()).hexdigest()[:12])
    (out / "v41.html").write_text(page)
    report = {
        "receipts": len(trace.events),
        "timer_boundaries": len(events) - len(trace.events),
        "model_requests": len(models),
        "spans": len(spans),
        "links": len(edges),
        "payload_files": len(detail_paths),
        "record_chunks": len(chunks),
        "schema": 1,
    }
    write_json(out / "v41-audit.json", report)
    return dataset, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    _, report = build(Trace(args.workspace, args.run_directory).build(), args.out)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
