import asyncio
import hashlib
from typing import Literal

from bloom_safari.web_document import page_document


async def run(
    tool,
    query: str,
    question: str = "",
    urls: list[str] | None = None,
    start: int = 0,
    width: Literal[512, 1024] = 512,
    height: Literal[512, 1024, 2048, 4096] = 2048,
):
    """Search ordinary Safari and capture up to 20 distinct candidates. Read each usable source independently for the actual question, with exact excerpts, surrounding summaries and native images. Source/window reads run concurrently and reuse identical completed work. Select up to seven relevant distinct sources and at most two visual sources. No monolithic review or second generic curation pass; full originals and capture failures remain available."""
    capture = await tool.service(
        "safari", query=query, urls=urls, start=start, width=width, height=height
    )
    sources = capture.pop("sources")
    result = {**capture, "selected": [], "candidates": [], "files": [], "reference_files": []}
    usable = []
    for source in sources:
        raw = tool.path(source["text_path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["text_hash"]:
            raise ValueError("Captured text changed before reading")
        for native in source.get("images", []):
            if (
                hashlib.sha256(tool.path(native["path"]).read_bytes()).hexdigest()
                != native["sha256"]
            ):
                raise ValueError("Captured native image changed before reading")
        # This is the same allowlisted document projection used for every web
        # model view: text/tables/linked references once, never raw DOM inventories.
        document = page_document({**source, "text": raw.decode()})
        candidate = {
            k: source[k]
            for k in ("id", "rank", "url", "status", "text_path", "metadata_path")
            if k in source
        }
        result["candidates"].append(candidate)
        if source["status"] not in ("ok", "partial"):
            candidate["reader_status"] = "capture_failed"
            continue
        projected = document["text"].encode()
        path = tool.path(source["text_path"])
        if projected != raw:
            path = tool.scratch / ("search-" + tool.job) / source["id"] / "document.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(projected)
        usable.append((source, document, candidate, path))
    if not usable:
        return {
            **result,
            "status": "capture_failed",
            "summary": "No usable source was captured. Originals and access failures are retained.",
            "error": capture.get("error"),
        }

    async def read(item):
        source, document, candidate, path = item
        return await tool.invoke(
            "extract",
            {
                "path": str(path),
                "question": question or query,
                "images": [i["path"] for i in source.get("images", [])],
                "context": {
                    k: document[k]
                    for k in ("id", "url", "title", "status", "coverage")
                    if k in document
                },
            },
        )

    reviewed = await asyncio.gather(*(read(item) for item in usable), return_exceptions=True)
    choices, limitations = [], []
    for item, view in zip(usable, reviewed):
        source, document, candidate, path = item
        if isinstance(view, BaseException):
            candidate.update(
                reader_status="unavailable", error=type(view).__name__ + ": " + str(view)
            )
            limitations.append("Reader unavailable for " + source["id"])
            continue
        candidate.update(
            reader_status="read",
            relevant=bool(view["relevant"] or view["visual_relevant"]),
            summary=view["summary"],
            source_id=view["source_id"],
        )
        if candidate["relevant"]:
            choices.append((source, document, view))
    choices.sort(key=lambda item: (-item[2]["priority"], item[0].get("rank", 0)))
    seen, visual_count = set(), 0
    for source, document, view in choices:
        if view["source_id"] in seen or len(result["selected"]) >= 7:
            continue
        seen.add(view["source_id"])
        selected = {
            "id": source["id"],
            "url": source["url"],
            "title": document.get("title", ""),
            "source_id": view["source_id"],
            "evidence": view,
        }
        result["reference_files"].extend(
            [source["text_path"], source["metadata_path"], *view["reference_files"]]
        )
        if view["visual_relevant"] and visual_count < 2:
            visual_count += 1
            selected["images"] = source["images"]
            result["files"].extend(i["path"] for i in source["images"])
        result["selected"].append(selected)
    if len(result["selected"]) < 3:
        limitations.append(
            f"Only {len(result['selected'])} relevant distinct sources were available; no padding with irrelevant sources."
        )
    result.update(
        status="completed",
        limitations=limitations,
        summary="\n\n".join(
            f"{s['title']} ({s['url']}) — {s['source_id']}\n{s['evidence']['summary']}"
            for s in result["selected"]
        )
        or "No relevant evidence was found; see candidate decisions and limitations.",
    )
    result["reference_files"] = list(dict.fromkeys(result["reference_files"]))
    return result
