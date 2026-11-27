"""Reconstruct elapsed-operation traces from immutable receipts; never runs the harness."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
TERMINAL = {"completed", "failed", "cancelled", "uncertain"}


def micros(value):
    delta = datetime.fromisoformat(value) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def union(intervals):
    merged = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def duration(intervals):
    return sum(b - a for a, b in union(intervals)) / 1e6


def clip(intervals, start, end):
    return [(max(a, start), min(b, end)) for a, b in intervals if a < end and b > start]


def subtract(intervals, excluded):
    result = []
    blockers = union(excluded)
    for start, end in union(intervals):
        for a, b in blockers:
            if b <= start:
                continue
            if a >= end:
                break
            if a > start:
                result.append((start, min(a, end)))
            start = max(start, b)
        if start < end:
            result.append((start, end))
    return result


def public(value, roots=(), depth=0):
    """A visible projection, never the provider's private reasoning or credentials."""
    if depth > 80:
        return "[deep value omitted; original retained]"
    if isinstance(value, dict):
        if value.get("type") in ("reasoning", "reasoning_summary", "reasoning_text"):
            return {"type": "private_reasoning_omitted"}
        if value.get("type") == "message" and value.get("phase") not in (
            None,
            "commentary",
            "final_answer",
        ):
            return {"type": "nonpublic_message_omitted"}
        out = {}
        for key, item in value.items():
            if key in ("encrypted_content", "logprobs"):
                continue
            if re.fullmatch(
                r"(?i)(authorization|access_token|refresh_token|id_token|api_key|password|secret|token)",
                key,
            ):
                out[key] = "[REDACTED]"
            elif key == "image_url" and isinstance(item, str) and item.startswith("data:"):
                out[key] = "[native image omitted; original retained]"
            else:
                out[key] = public(item, roots, depth + 1)
        return out
    if isinstance(value, list):
        return [public(v, roots, depth + 1) for v in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(public(json.loads(value), roots, depth + 1), ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        for root in roots:
            value = value.replace(str(root), "<workspace>" if root == roots[0] else "<project>")
        value = re.sub(r"/Users/[^/\s]+", "<home>", value)
        value = re.sub(
            r'"encrypted_content"\s*:\s*"(?:\\.|[^"\\])*"', '"private_state":"[omitted]"', value
        )
        value = re.sub(
            r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+", "[native image omitted]", value
        )
        value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~-]{12,}", "Bearer [REDACTED]", value)
        value = re.sub(
            r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b",
            "[REDACTED JWT]",
            value,
        )
        value = re.sub(
            r"\b(?:sk-(?:proj-)?|gh[pousr]_)[A-Za-z0-9_-]{30,}\b", "[REDACTED key]", value
        )
        return value
    return value


class Trace:
    def __init__(self, workspace, run_directory):
        self.root = Path(workspace).resolve()
        self.run_directory = Path(run_directory).resolve()
        db = sqlite3.connect(f"file:{self.root / 'index.sqlite3'}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        self.events = [
            {**dict(r), "data": json.loads(r["data"]), "us": micros(r["at"])}
            for r in db.execute("SELECT * FROM events ORDER BY seq")
        ]
        db.close()
        if not self.events:
            raise ValueError("The receipt index is empty")
        # The index is only a navigation aid. Verify it against file originals;
        # fail on a stale index instead of silently omitting accepted operations.
        originals = {p.name for p in (self.root / "control/events").glob("*.json")}
        if originals != {e["path"] for e in self.events}:
            raise ValueError("Journal and index disagree; rebuild the index before exporting")
        for event in self.events:
            original = json.loads((self.root / "control/events" / event["path"]).read_bytes())
            checksum = original.pop("digest")
            raw = json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if hashlib.sha256(raw.encode()).hexdigest() != checksum or checksum != event["digest"]:
                raise ValueError("Journal checksum mismatch: " + event["path"])
            if any(
                original[k] != event[k] for k in ("seq", "key", "kind", "subject", "at", "data")
            ):
                raise ValueError("Journal projection mismatch: " + event["path"])
        self.keys = {e["key"]: e for e in self.events}
        self.kinds = defaultdict(list)
        for e in self.events:
            self.kinds[e["kind"]].append(e)
        self.first = min(e["us"] for e in self.events)
        self.last = max(e["us"] for e in self.events)
        self.spans, self.edges, self.details = [], [], {}
        self.span_by_id = {}
        self.processes = []
        for path in sorted(self.run_directory.glob("*-process.json")):
            p = json.loads(path.read_bytes())
            if p.get("started_at") and p.get("ended_at"):
                self.processes.append((micros(p["started_at"]), micros(p["ended_at"])))

    def object(self, ref):
        raw = (self.root / "artifacts/objects" / ref["sha256"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("Original object checksum mismatch")
        return (
            json.loads(raw) if ref["media"] == "application/json" else raw.decode(errors="replace")
        )

    def add(
        self,
        ident,
        name,
        category,
        actor,
        start,
        end,
        *,
        begin=None,
        finish=None,
        parent=None,
        status="ok",
        attributes=None,
        detail=None,
    ):
        if end < start:
            raise ValueError("Negative recorded span: " + ident)
        if ident in self.span_by_id:
            raise ValueError("Duplicate span: " + ident)
        item = {
            "id": ident,
            "name": name,
            "category": category,
            "actor": actor,
            "start_us": start,
            "end_us": end,
            "start_event": begin,
            "end_event": finish,
            "parent": parent,
            "status": status,
            "attributes": attributes or {},
        }
        self.spans.append(item)
        self.span_by_id[ident] = item
        if detail is not None:
            self.details[ident] = detail
        return item

    def edge(self, source, target, relation):
        if source and target:
            self.edges.append({"source": source, "target": target, "relation": relation})

    def build(self):
        job_rows = defaultdict(list)
        turn_rows = defaultdict(list)
        for e in self.kinds["job.state"]:
            job_rows[e["subject"]].append(e)
        for e in self.kinds["turn.state"]:
            turn_rows[e["subject"]].append(e)
        self.jobs = {j: rows[-1]["data"] for j, rows in job_rows.items()}
        self.turns = {t: rows[-1]["data"] for t, rows in turn_rows.items()}
        received = {e["data"]["turn"]: e for e in self.kinds["turn.received"]}
        model_started = {e["data"]["id"]: e for e in self.kinds["model.started"]}
        model_ended = {
            e["data"]["id"]: e
            for kind in ("model.completed", "model.failed")
            for e in self.kinds[kind]
        }
        readers = {e["data"]["call"]: e["data"]["job"] for e in self.kinds["reader.sources"]}
        missing = {
            i
            for i, e in model_started.items()
            if e["data"].get("purpose") == "tool_reader" and i not in readers
        }
        # Failed readers lack reader.sources. Recover only exact, unique ID matches.
        for job in self.jobs:
            for ordinal in range(1, 257):
                call = (
                    "reader_"
                    + hashlib.sha256(
                        json.dumps([job, ordinal], sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()[:24]
                )
                if call in missing:
                    readers[call] = job
        self.unlinked_readers = sorted(missing - readers.keys())

        for turn, rows in turn_rows.items():
            a = received.get(turn, rows[0])
            ended = next(
                (r for r in rows if r["data"]["status"] in ("completed", "blocked")), rows[-1]
            )
            data = rows[-1]["data"]
            self.add(
                "turn:" + turn,
                "Turn " + str(a["seq"]),
                "Turn",
                data["scope"],
                a["us"],
                ended["us"],
                begin=a["seq"],
                finish=ended["seq"],
                status="error" if ended["data"]["status"] == "blocked" else "ok",
                attributes={
                    "native_id": turn,
                    "origin": data.get("origin"),
                    "state": data["status"],
                },
            )
            model = model_started.get(turn)
            if model:
                self.add(
                    "admission:" + turn,
                    "Prepare / admit model",
                    "Admission",
                    data["scope"],
                    a["us"],
                    model["us"],
                    begin=a["seq"],
                    finish=model["seq"],
                    parent="turn:" + turn,
                    attributes={
                        "measurement": "Includes unseparated token estimation, model-slot wait and local preparation after turn.received"
                    },
                )
        for job, rows in job_rows.items():
            first, last = rows[0], rows[-1]
            start = next((r for r in rows if r["data"]["status"] in ("launching", "running")), None)
            end = next((r for r in rows if r["data"]["status"] in TERMINAL), None)
            data = last["data"]
            actor = data.get("branch") or data["agent"]
            queue_end = start or end or last
            self.add(
                "queue:" + job,
                data["name"] + " · queued",
                "Tool queue",
                actor,
                first["us"],
                queue_end["us"],
                begin=first["seq"],
                finish=queue_end["seq"],
                attributes={
                    "native_id": job,
                    "tool": data["name"],
                    "parent_job": data.get("parent"),
                    "turn": data.get("turn"),
                    "state": data["status"],
                },
            )
            if start:
                end = end or last
                status = "ok" if end["data"]["status"] == "completed" else "error"
                self.add(
                    "job:" + job,
                    data["name"],
                    "Tool execution",
                    actor,
                    start["us"],
                    end["us"],
                    begin=start["seq"],
                    finish=end["seq"],
                    status=status,
                    attributes={
                        "native_id": job,
                        "tool": data["name"],
                        "parent_job": data.get("parent"),
                        "turn": data.get("turn"),
                        "error": data.get("error"),
                        "state": data["status"],
                        "arguments": data["arguments"],
                    },
                    detail={
                        "tool": data["name"],
                        "arguments": data["arguments"],
                        "state": data["status"],
                        "error": data.get("error"),
                        "result_ref": data.get("result"),
                    },
                )
                self.edge("queue:" + job, "job:" + job, "admitted to execution")
                self.edge("model:" + (data.get("turn") or ""), "queue:" + job, "requested tool")
                if data.get("parent"):
                    self.edge("job:" + data["parent"], "job:" + job, "invoked child")
                notified = self.keys.get("job-notified:" + job)
                if notified:
                    self.add(
                        "notification:" + job,
                        data["name"] + " · result delivery",
                        "Result delivery",
                        actor,
                        end["us"],
                        notified["us"],
                        begin=end["seq"],
                        finish=notified["seq"],
                        attributes={
                            "native_id": job,
                            "woke_owner": notified["data"]["woke"],
                            "measurement": "Terminal result to notification receipt; may include separate curation",
                        },
                    )
                    self.edge("job:" + job, "notification:" + job, "result notification")

        for ident, start in model_started.items():
            end = model_ended.get(ident)
            if end is None:
                raise ValueError("Model request has no terminal receipt: " + ident)
            data = start["data"]
            purpose = data["purpose"]
            profile = data["profile"]
            category = (
                "Compaction"
                if purpose == "compact"
                else "Luna / reader"
                if purpose == "tool_reader"
                else "Model decision"
            )
            usage = end["data"].get("usage") or {}
            caller = readers.get(ident)
            parent = "job:" + caller if caller else "turn:" + ident if ident in turn_rows else None
            if parent and not (
                self.span_by_id[parent]["start_us"]
                <= start["us"]
                <= end["us"]
                <= self.span_by_id[parent]["end_us"]
            ):
                parent = None
            self.add(
                "model:" + ident,
                profile.title() + " · " + purpose + " #" + str(start["seq"]),
                category,
                start["subject"],
                start["us"],
                end["us"],
                begin=start["seq"],
                finish=end["seq"],
                parent=parent,
                status="error" if end["kind"] == "model.failed" else "ok",
                attributes={
                    "native_id": ident,
                    "profile": profile,
                    "purpose": purpose,
                    "caller_job": caller,
                    "origin": data.get("origin"),
                    "input_estimate": data["input_estimate"],
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "error": end["data"].get("error"),
                    "measurement": "Request intent to terminal receipt, including transport and local persistence; not provider CPU time",
                },
                detail={
                    "request_ref": data["request"],
                    "response_ref": end["data"].get("response"),
                    "error": end["data"].get("error"),
                },
            )
            if caller:
                self.edge("job:" + caller, "model:" + ident, "shared model call")

        services = defaultdict(list)
        for start in self.kinds["service.started"]:
            data = start["data"]
            key = start["key"].removesuffix(":started")
            end = self.keys.get(key + ":completed") or self.keys.get(key + ":failed")
            job_span = self.span_by_id.get("job:" + data["job"])
            stop = end["us"] if end else job_span["end_us"] if job_span else self.last
            parent = job_span["id"] if job_span and stop <= job_span["end_us"] else None
            span = self.add(
                key,
                "Service · " + data["name"],
                "Service RPC",
                start["subject"],
                start["us"],
                stop,
                begin=start["seq"],
                finish=end["seq"] if end else None,
                parent=parent,
                status="warning"
                if not end
                else "error"
                if end["kind"] == "service.failed"
                else "ok",
                attributes={
                    "job": data["job"],
                    "service": data["name"],
                    "measurement": "Service intent to receipt, or executor settlement if no service terminal receipt",
                },
                detail={
                    "arguments": data["arguments"],
                    "result_ref": end["data"].get("result") if end else None,
                    "error": end["data"].get("error") if end else "No service terminal receipt",
                },
            )
            services[data["job"]].append(span)

        for start in self.kinds["web.started"]:
            data = start["data"]
            operation = data["operation"]
            complete = self.keys.get("web-completed:" + operation)
            job_span = self.span_by_id.get("job:" + data["job"])
            end = complete["us"] if complete else job_span["end_us"] if job_span else self.last
            parent = "job:" + data["job"] if job_span and end <= job_span["end_us"] else None
            enclosing = [
                s
                for s in services[data["job"]]
                if s["start_us"] <= start["us"] <= end <= s["end_us"]
            ]
            if len(enclosing) == 1:
                parent = enclosing[0]["id"]
            self.add(
                "web:" + operation,
                "Safari · " + data["query"],
                "Browser service",
                start["subject"],
                start["us"],
                end,
                begin=start["seq"],
                finish=complete["seq"] if complete else None,
                parent=parent,
                status="ok" if complete else "warning",
                attributes={
                    "native_id": operation,
                    "job": data["job"],
                    "query": data["query"],
                    "measurement": "Service interval includes lease queue, discovery and captures; lease acquisition was not separately timed",
                },
            )
        self.capture_manifests = {}
        for event in self.kinds["web.source"]:
            data = event["data"]
            page = self.object(data["source"])
            self.capture_manifests[data["source_id"]] = page
            start = (
                micros(page["capture_started_at"])
                if page.get("capture_started_at")
                else event["us"]
            )
            end = (
                micros(page["captured_at"])
                if page.get("capture_started_at") and page.get("captured_at")
                else event["us"]
            )
            parent = "web:" + data["operation"]
            if not (
                self.span_by_id[parent]["start_us"]
                <= start
                <= end
                <= self.span_by_id[parent]["end_us"]
            ):
                parent = None
            self.add(
                "capture:" + data["source_id"],
                page.get("title") or page.get("url") or "Capture",
                "Browser capture",
                event["subject"],
                start,
                end,
                begin=event["seq"],
                finish=event["seq"],
                parent=parent,
                status="ok" if page["status"] == "ok" else "warning",
                attributes={
                    "native_id": data["source_id"],
                    "url": page.get("url"),
                    "state": page["status"],
                    "text_characters": page.get("text_characters"),
                    "image_count": len(page.get("images", [])),
                    "measurement": "Capture start/end from source manifest"
                    if page.get("capture_started_at")
                    else "Timestamp only; capture start was not recorded",
                },
                detail={"source_manifest": page},
            )

        messages = {e["subject"]: e for e in self.kinds["message.prepared"]}
        self.messages = messages
        context_returns = defaultdict(list)
        for ident, prepared in messages.items():
            data = prepared["data"]
            delivered = self.keys.get("message-delivered:" + ident)
            consumed = self.keys.get("message-consumed:" + ident)
            filed = self.keys.get("message-filed:" + ident)
            target_turn = consumed["data"]["turn"] if consumed else None
            admitted = received.get(target_turn) if target_turn else None
            if delivered:
                end = admitted or filed
                stop = end["us"] if end else delivered["us"]
                span = self.add(
                    "message:" + ident,
                    data["sender"] + " → " + data["recipient"] + " · " + data["kind"],
                    "Inbox wait" if end else "Message",
                    data["recipient"],
                    delivered["us"],
                    stop,
                    begin=delivered["seq"],
                    finish=end["seq"] if end else delivered["seq"],
                    attributes={
                        "native_id": ident,
                        "kind": data["kind"],
                        "sender": data["sender"],
                        "recipient": data["recipient"],
                        "consumer_turn": target_turn,
                        "measurement": "Delivery to consuming turn start, or filing; point if not consumed",
                    },
                    detail={"envelope": data, "body_ref": data["body"]},
                )
                origin_job = data["metadata"].get("sender_job") or data["metadata"].get("job")
                self.edge(
                    "job:" + origin_job if origin_job else None, span["id"], "delivered message"
                )
                self.edge(
                    span["id"], "turn:" + target_turn if target_turn else None, "consumed input"
                )
                info = data["metadata"]
                if data["kind"] == "context" and info.get("request_id"):
                    context_returns[info["request_id"]].append(delivered)
        for start in self.kinds["context.requested"]:
            ident = start["data"]["request_id"]
            returns = context_returns.get(ident)
            if returns:
                end = min(returns, key=lambda e: e["seq"])
                self.add(
                    "context:" + ident,
                    "Terra context for " + start["subject"],
                    "Context preparation",
                    start["subject"],
                    start["us"],
                    end["us"],
                    begin=start["seq"],
                    finish=end["seq"],
                    attributes={
                        "native_id": ident,
                        "input_messages": start["data"]["original_ids"],
                        "measurement": "Preparation request to returned context, including tool work and queueing",
                    },
                )
        for a, b in self.processes:
            self.add(
                "process:" + str(a),
                "CLI process",
                "Runner",
                "harness",
                a,
                b,
                attributes={"measurement": "Supervisor launch/exit timestamps"},
            )
        for recovery in self.kinds["operator.recovery"]:
            start = recovery["data"].get("delay_started_at")
            resolutions = [
                event
                for event in self.kinds["context.finished"]
                if event["data"].get("operator_resolution") == recovery["key"]
            ]
            if not start or not resolutions:
                continue
            if len(resolutions) != 1:
                raise ValueError("Ambiguous operator recovery resolution: " + recovery["key"])
            end = resolutions[0]
            self.add(
                "recovery:" + recovery["key"],
                "Operator recovery",
                "Operator recovery",
                "operator",
                micros(start),
                end["us"],
                finish=end["seq"],
                status="warning",
                attributes={
                    "recovery_event": recovery["seq"],
                    "reason": recovery["data"].get("reason"),
                    "measurement": "Operator-declared delay start to its explicitly linked context resolution receipt; not model compute time",
                },
            )
        self.edges = [
            e
            for e in self.edges
            if e["source"] in self.span_by_id and e["target"] in self.span_by_id
        ]
        self.cases = []
        for answer in messages.values():
            p = answer["data"]
            if p["recipient"] != "user" or p["sender"] != "astra":
                continue
            question = p["metadata"]["origin"]
            start = self.keys["message-delivered:" + question]
            end = self.keys["message-delivered:" + p["id"]]
            self.cases.append(
                {
                    "id": question.removesuffix("-question"),
                    "start_us": start["us"],
                    "end_us": end["us"],
                    "input_event": start["seq"],
                    "answer_event": end["seq"],
                }
            )
        self.cases.sort(key=lambda c: c["start_us"])
        counts = Counter(c["id"] for c in self.cases)
        for case in self.cases:
            if counts[case["id"]] > 1:
                case["id"] += "-answer-" + str(case["answer_event"])
            for n, (a, b) in enumerate(
                subtract([(case["start_us"], case["end_us"])], self.processes)
                if self.processes
                else []
            ):
                self.add(
                    "pause:" + case["id"] + ":" + str(n),
                    "CLI not running · setup / repair",
                    "Operator pause",
                    "operator",
                    a,
                    b,
                    attributes={
                        "measurement": "Complement of recorded CLI process lifetimes; not harness CPU"
                    },
                )
        return self

    def metrics(self, case):
        start, end = case["start_us"], case["end_us"]
        selected = [s for s in self.spans if s["start_us"] < end and s["end_us"] > start]
        categories = {}
        for category in sorted({s["category"] for s in selected}):
            spans = [s for s in selected if s["category"] == category]
            times = clip([(s["start_us"], s["end_us"]) for s in spans], start, end)
            categories[category] = {
                "count": len(spans),
                "union_seconds": duration(times),
                "summed_seconds": sum(b - a for a, b in times) / 1e6,
            }
        # Exhaustive wall classification by simultaneous recorded activity, without double counting.
        groups = {
            "Model call": [
                (s["start_us"], s["end_us"])
                for s in selected
                if s["category"] in ("Model decision", "Luna / reader", "Compaction")
            ],
            "Browser service": [
                (s["start_us"], s["end_us"]) for s in selected if s["category"] == "Browser service"
            ],
            "Tool process": [
                (s["start_us"], s["end_us"]) for s in selected if s["category"] == "Tool execution"
            ],
            "Operator pause": subtract([(start, end)], self.processes) if self.processes else [],
            "Operator recovery": [
                (s["start_us"], s["end_us"])
                for s in selected
                if s["category"] == "Operator recovery"
            ],
        }
        changes = defaultdict(Counter)
        for group, intervals in groups.items():
            for a, b in union(clip(intervals, start, end)):
                changes[a][group] += 1
                changes[b][group] -= 1
        changes[start]
        changes[end]
        active = Counter()
        wall = Counter()
        previous = start
        for at, delta in sorted(changes.items()):
            if at > previous:
                label = (
                    " + ".join(k for k in groups if active[k])
                    or "No recorded model / browser / tool span"
                )
                wall[label] += (at - previous) / 1e6
            active.update(delta)
            previous = at
        assert abs(sum(wall.values()) - (end - start) / 1e6) < 0.0001
        queue = sorted(
            [s for s in selected if s["category"] == "Tool queue"],
            key=lambda s: s["end_us"] - s["start_us"],
            reverse=True,
        )
        context = sorted(
            [s for s in selected if s["category"] == "Context preparation"],
            key=lambda s: s["end_us"] - s["start_us"],
            reverse=True,
        )
        model = [
            s
            for s in selected
            if s["category"] in ("Model decision", "Luna / reader", "Compaction")
        ]
        return {
            **case,
            "wall_seconds": (end - start) / 1e6,
            "wall_states": dict(wall),
            "categories": categories,
            "model_profiles": dict(Counter(s["attributes"]["profile"] for s in model)),
            "top_queues": [
                {
                    "id": s["id"],
                    "actor": s["actor"],
                    "name": s["name"],
                    "seconds": (s["end_us"] - s["start_us"]) / 1e6,
                    "start_event": s["start_event"],
                    "end_event": s["end_event"],
                }
                for s in queue[:15]
            ],
            "top_context_waits": [
                {
                    "id": s["id"],
                    "actor": s["actor"],
                    "seconds": (s["end_us"] - s["start_us"]) / 1e6,
                    "start_event": s["start_event"],
                    "end_event": s["end_event"],
                }
                for s in context[:10]
            ],
        }

    def write(self, destination, details=False):
        destination = Path(destination).resolve()
        if destination.is_relative_to(self.root):
            raise ValueError("Profiler outputs must be outside the original workspace")
        destination.mkdir(parents=True, exist_ok=True)
        roots = (self.root, self.run_directory.parents[2])
        report = {
            "source_events": len(self.events),
            "source_first": self.events[0]["at"],
            "source_last": self.events[-1]["at"],
            "spans": len(self.spans),
            "edges": len(self.edges),
            "unlinked_readers": self.unlinked_readers,
            "model_requests": len(self.kinds["model.started"]),
            "model_completed": len(self.kinds["model.completed"]),
            "model_failed": len(self.kinds["model.failed"]),
            "runner_lifetimes_recorded": bool(self.processes),
            "cases": [self.metrics(c) for c in self.cases],
            "limitations": [
                "Reconstructed elapsed intervals, not sampled CPU or provider compute time.",
                "Categories overlap; use wall_states for an additive breakdown.",
                "Browser lease acquisition and per-RPC CPU timers were not recorded.",
                "Point events have no fabricated duration.",
            ],
        }
        (destination / "summary.json").write_text(json.dumps(report, indent=2))
        data = public(
            {
                "cases": self.cases,
                "spans": self.spans,
                "edges": self.edges,
                "events": [
                    {k: e[k] for k in ("seq", "kind", "subject", "at", "us")} for e in self.events
                ],
            },
            roots,
        )
        (destination / "operations.json").write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        )
        self.write_view(destination, data, report)
        lanes = {
            name: i + 1
            for i, name in enumerate(
                sorted({s["actor"] + " / " + s["category"] for s in self.spans})
            )
        }
        trace = [
            {"ph": "M", "name": "thread_name", "pid": 1, "tid": tid, "args": {"name": name}}
            for name, tid in lanes.items()
        ]
        for s in self.spans:
            trace.append(
                {
                    "ph": "X",
                    "pid": 1,
                    "tid": lanes[s["actor"] + " / " + s["category"]],
                    "name": public(s["name"], roots),
                    "cat": s["category"],
                    "ts": s["start_us"] - self.first,
                    "dur": s["end_us"] - s["start_us"],
                    "args": public(
                        {
                            "id": s["id"],
                            "start_event": s["start_event"],
                            "end_event": s["end_event"],
                            **s["attributes"],
                        },
                        roots,
                    ),
                }
            )
        for e in self.events:
            trace.append(
                {
                    "ph": "i",
                    "s": "t",
                    "pid": 2,
                    "tid": 1,
                    "ts": e["us"] - self.first,
                    "name": e["kind"],
                    "args": {"event": e["seq"], "subject": e["subject"]},
                }
            )
        (destination / "chrome-trace.json").write_text(
            json.dumps(
                {
                    "traceEvents": trace,
                    "displayTimeUnit": "ms",
                    "metadata": {"source": "Bloom v4.1 receipt trace", "cpu_profile": False},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        if details:
            folder = destination / "details"
            folder.mkdir(exist_ok=True)
            index = {}
            for ident, item in self.details.items():
                output = dict(item)
                for key in ("request_ref", "response_ref", "result_ref", "body_ref"):
                    if output.get(key):
                        output[key.removesuffix("_ref")] = self.object(output[key])
                name = hashlib.sha256(ident.encode()).hexdigest()[:24] + ".json"
                (folder / name).write_text(
                    json.dumps(public(output, roots), ensure_ascii=False, indent=2)
                )
                index[ident] = "details/" + name
            (destination / "details-index.json").write_text(json.dumps(index, indent=2))
        return report

    def write_view(self, destination, data, report):
        categories = sorted({s["category"] for s in data["spans"]})
        actors = sorted({s["actor"] for s in data["spans"]})
        indices = {s["id"]: n for n, s in enumerate(data["spans"])}
        # Compact transport, expanded to the widget's documented span API locally.
        rows = [
            [
                s["id"],
                s["name"],
                categories.index(s["category"]),
                actors.index(s["actor"]),
                round((s["start_us"] - self.first) / 1e3, 3),
                round((s["end_us"] - s["start_us"]) / 1e3, 3),
                s["start_event"],
                s["end_event"],
                indices.get(s["parent"]),
                s["status"],
            ]
            for s in data["spans"]
        ]
        cases = [
            {
                "id": c["id"],
                "start": (c["start_us"] - self.first) / 1e3,
                "end": (c["end_us"] - self.first) / 1e3,
                "calls": sum(c["model_profiles"].values()),
                "downtime": c["categories"].get("Operator pause", {}).get("union_seconds", 0),
                "maxQueue": max((q["seconds"] for q in c["top_queues"]), default=0),
            }
            for c in report["cases"]
        ]
        cases.append(
            {
                "id": "full-run",
                "start": min(0, min(r[4] for r in rows)),
                "end": max((self.last - self.first) / 1e3, max(r[4] + r[5] for r in rows)),
                "calls": report["model_requests"],
                "downtime": None,
                "maxQueue": max(
                    (r[5] / 1e3 for r in rows if categories[r[2]] == "Tool queue"), default=0
                ),
            }
        )
        payload = {
            "run": self.run_directory.name,
            "cases": cases,
            "categories": categories,
            "actors": actors,
            "rows": rows,
            "sourceEvents": len(self.events),
        }
        literal = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        literal = literal.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        template = Path(__file__).with_name("profiler_view.html").read_text()
        fragment = template.replace("__TRACE_DATA__", literal)
        if len(fragment.encode()) >= 1_000_000:
            raise ValueError("Inline trace exceeds 1 MB; use the complete Chrome trace export")
        (destination / "operation-timeline.html").write_text(fragment)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--run-directory", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--details", action="store_true")
    a = p.parse_args()
    report = Trace(a.workspace, a.run_directory).build().write(a.out, a.details)
    print(
        json.dumps({k: report[k] for k in ("source_events", "spans", "edges", "unlinked_readers")})
    )
    for c in report["cases"]:
        print(
            json.dumps(
                {
                    "case": c["id"],
                    "wall_seconds": c["wall_seconds"],
                    "wall_states": c["wall_states"],
                    "top_queues": c["top_queues"][:3],
                }
            )
        )


if __name__ == "__main__":
    main()
