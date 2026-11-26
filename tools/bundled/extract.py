import hashlib
import json
import re

from bloom41.evidence import NumberedSource, READER_INSTRUCTION


def parsed(value):
    text = value["text"].strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", text, re.S)
    return json.loads(fenced.group(1) if fenced else text)


async def run(tool, path: str, question: str):
    """Ask Luna to extract question-specific evidence from an existing file or image. Text is read in numbered 128k-token passes. Quotations are extracted verbatim from validated ranges, with separate surrounding summaries. Originals and exact coordinates remain available; this performs no new web search."""
    source = tool.path(path)
    raw = source.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    if source.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        answer = await tool.model(
            "Inspect the supplied native image for this question. Distinguish visible facts from inference and state limitations. Return JSON with summary and limitations.\n"
            + question,
            sources=[str(source)],
        )
        result = parsed(answer)
        return {
            "summary": result["summary"],
            "limitations": result.get("limitations", []),
            "files": [str(source)],
            "source_sha256": checksum,
            "calls": [answer["call_id"]],
            "image_path": str(source),
        }
    text = raw.decode()
    document = NumberedSource.index(text)
    passages, summaries, calls, coverage = [], [], [], []
    for window in document.windows(0, 128_000):
        answer = await tool.model(
            [
                {"type": "input_text", "text": READER_INSTRUCTION},
                {
                    "type": "input_text",
                    "text": json.dumps(
                        {
                            "question": question,
                            "path": str(source),
                            "source_sha256": checksum,
                            **window.metadata(),
                        }
                    ),
                },
                {"type": "input_text", "text": window.render()},
            ]
        )
        result = parsed(answer)
        if (
            type(result.get("relevant")) is not bool
            or not isinstance(result.get("summary"), str)
            or not isinstance(result.get("passages"), list)
        ):
            raise ValueError("Reader did not return a valid numbered selection; original retained")
        quotes, invalid, invalid_context = window.selections(result["passages"], str(source))
        if result["relevant"] and not quotes:
            raise ValueError("Relevant reader response contains no valid source range")
        if not result["relevant"] and result["passages"]:
            raise ValueError("Irrelevant response proposed passages")
        passages.extend(quotes)
        summaries.append(result["summary"])
        calls.append(answer["call_id"])
        coverage.append(
            {
                "start_char": window.start,
                "end_char": window.end,
                **window.metadata(),
                "invalid_ranges": invalid,
                "invalid_context": invalid_context,
            }
        )
    return {
        "summary": "\n".join(summaries),
        "passages": passages,
        "coverage": coverage,
        "source_path": str(source),
        "source_sha256": checksum,
        "calls": calls,
    }
