import asyncio
import hashlib
import json
import re

from bloom41.evidence import NumberedSource, READER_INSTRUCTION


def parsed(value):
    text = value["text"].strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", text, re.S)
    return json.loads(fenced.group(1) if fenced else text)


async def run(
    tool,
    path: str,
    question: str,
    start_char: int | None = None,
    end_char: int | None = None,
    images: list[str] | None = None,
    context: dict | None = None,
):
    """Explicitly read one source for an actual question. Returns validated exact excerpts plus summaries of omitted ranges above/between/below them. Optional character bounds are 0-based half-open. Independent 32k-token windows run concurrently; missing coverage is explicit. Original files/native images are retained. Repeated identical completed reader requests are reused, without new search or hidden retries."""
    if not question.strip():
        raise ValueError("Supply the actual evidence question")
    source = tool.path(path)
    raw = source.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    published = await tool.request("attach_file", path=path, reference=True)
    source_id = published["source_id"]
    native = []
    for image in images or []:
        record = await tool.request("attach_file", path=image, reference=True)
        native.append(record["source_id"])
    if tool.media(path).startswith("image/"):
        answer = await tool.model(
            "Inspect this native image for the actual question. Return JSON with summary and limitations. Distinguish visible facts from inference.\n"
            + json.dumps({"question": question, "source_id": source_id, "context": context or {}}),
            sources=[source_id],
        )
        result = parsed(answer)
        return {
            "summary": result["summary"],
            "limitations": result.get("limitations", []),
            "source_id": source_id,
            "source_sha256": checksum,
            "calls": [answer["call_id"]],
            "image_path": source_id,
            "reference_files": [source_id],
        }
    text = raw.decode()
    left, right = start_char or 0, len(text) if end_char is None else end_char
    if not 0 <= left <= right <= len(text):
        raise ValueError("Requested source range is outside the file")
    document = NumberedSource.index(text)
    windows = []
    for window in document.windows(left, 32_000):
        if window.start >= right:
            break
        windows.append(document.window(window.start, min(window.end, right)))

    async def read(window, number):
        answer = await tool.model(
            [
                {"type": "input_text", "text": READER_INSTRUCTION},
                {
                    "type": "input_text",
                    "text": json.dumps(
                        {
                            "question": question,
                            "source_id": source_id,
                            "source_sha256": checksum,
                            "context": context or {},
                            "range_start_char": window.start,
                            "range_end_char": window.end,
                            **window.metadata(),
                        }
                    ),
                },
                {"type": "input_text", "text": window.render()},
            ],
            sources=native if number == 0 else [],
        )
        result = parsed(answer)
        if (
            type(result.get("relevant")) is not bool
            or not isinstance(result.get("summary"), str)
            or not isinstance(result.get("passages"), list)
            or not isinstance(result.get("summaries", []), list)
        ):
            raise ValueError("Reader returned an invalid source selection; original retained")
        view = window.view(result, source_id)
        if result["relevant"] and not view["passages"]:
            raise ValueError("Relevant source selection has no valid exact range")
        if not result["relevant"] and result["passages"]:
            raise ValueError("Irrelevant selection proposed quoted ranges")
        return result, view, answer["call_id"]

    results = await asyncio.gather(
        *(read(w, n) for n, w in enumerate(windows)), return_exceptions=True
    )
    passages, sections, summaries, calls, coverage, limitations = [], [], [], [], [], []
    relevant, priority, visual, visual_reasons = False, 0, False, []
    for window, value in zip(windows, results):
        bounds = {"start_char": window.start, "end_char": window.end, **window.metadata()}
        if isinstance(value, BaseException):
            limitation = type(value).__name__ + ": " + str(value)
            limitations.append(limitation)
            coverage.append({**bounds, "status": "unavailable", "error": limitation})
            sections.append({"kind": "unread", **bounds})
            continue
        result, view, call = value
        offset = len(passages)
        passages.extend(view["passages"])
        sections.extend(
            {**s, **({"passage": s["passage"] + offset} if s["kind"] == "quote" else {})}
            for s in view["sections"]
        )
        summaries.append(result["summary"])
        calls.append(call)
        relevant |= result["relevant"]
        score = result.get("priority", 2 if result["relevant"] else 0)
        priority = max(priority, score if type(score) is int and 0 <= score <= 3 else 0)
        visual |= bool(result.get("visual_relevant"))
        if result.get("visual_reason"):
            visual_reasons.append(str(result["visual_reason"]))
        limitations.extend(str(v) for v in result.get("limitations", []) if isinstance(v, str))
        coverage.append(
            {
                **bounds,
                "status": "read",
                **{k: view[k] for k in ("invalid_ranges", "invalid_summaries", "summary_complete")},
            }
        )
        if not view["summary_complete"]:
            limitations.append(
                "Some omitted ranges have no valid summary; see unavailable_summary sections."
            )
    if native and len(windows) > 1:
        limitations.append(
            "Native images were assessed alongside the first text window; later windows contain text only."
        )
    return {
        "summary": "\n".join(summaries),
        "relevant": relevant,
        "priority": priority,
        "passages": passages,
        "sections": sections,
        "coverage": coverage,
        "limitations": list(dict.fromkeys(limitations)),
        "source_id": source_id,
        "source_path": source_id,
        "source_sha256": checksum,
        "calls": calls,
        "visual_relevant": visual,
        "visual_reason": "\n".join(visual_reasons),
        "reference_files": [source_id, *native],
    }
