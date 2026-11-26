"""Scoped Safari capture service. Selection and extraction belong to search.py."""

import asyncio
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bloom41.agents import Agents
from bloom41.store import atomic, digest, encoded, now

from .privacy import clean
from .safari import Safari, NavigationChanged, validate_url


def canonical(url):
    validate_url(url)
    p = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")
    ]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ""))


class BrowserService:
    """One serial ordinary-Safari lease; callers cannot choose output paths or actors."""

    def __init__(self, store, browser_factory=Safari, *, browser_workspace=None):
        self.store = store
        self.agents = Agents(store)
        self.browser_factory = browser_factory
        self.browser_workspace = (
            Path(browser_workspace).resolve() if browser_workspace else store.root / "workspace"
        )
        self.lock = asyncio.Lock()

    def retain(self, actor, operation, source_id, path, raw, media):
        ref = self.store.put(raw, media)
        if path.exists() and path.read_bytes() != raw:
            raise ValueError("Captured source path already has different bytes")
        atomic(path, raw)
        self.store.event(
            "web.file",
            actor.scope,
            {"operation": operation, "source_id": source_id, "path": str(path), "source": ref},
            key="web-file:" + digest(encoded([operation, str(path)])),
        )
        return ref

    def save(self, actor, job, operation, source, page, folder, export):
        manifest = {
            **clean(page),
            **source,
            "requested_url": source["url"],
            "url": page.get("url", source["url"]),
            "operation_id": operation,
            "scope": actor.scope,
            "job": job["id"],
            "turn": job.get("turn"),
        }
        text = manifest.pop("text", "")
        ref = self.retain(
            actor, operation, source["id"], folder / "page.txt", text.encode(), "text/plain"
        )
        atomic(export / "page.txt", text.encode())
        manifest.update(
            text_path=str(export / "page.txt"),
            text_hash=ref["sha256"],
            text_characters=len(text),
            original_text_path=str(folder / "page.txt"),
        )
        images = []
        for index, item in enumerate(page.get("images", [])):
            path = Path(item["path"])
            if not path.resolve().is_relative_to(folder.resolve()):
                raise ValueError("Browser image escaped its capture folder")
            raw = path.read_bytes()
            ref = self.retain(
                actor, operation, source["id"], path, raw, item.get("mime", "image/png")
            )
            destination = export / (f"image-{index:02d}" + path.suffix)
            atomic(destination, raw)
            images.append(
                {
                    **item,
                    "path": str(destination),
                    "original_path": str(path),
                    "sha256": ref["sha256"],
                    "source_id": source["id"],
                }
            )
        manifest.update(images=images, metadata_path=str(export / "source.json"))
        ref = self.retain(
            actor,
            operation,
            source["id"],
            folder / "source.json",
            encoded(manifest),
            "application/json",
        )
        atomic(export / "source.json", encoded(manifest))
        self.store.event(
            "web.source",
            actor.scope,
            {
                "operation": operation,
                "source": ref,
                "source_id": source["id"],
                "status": manifest["status"],
            },
            key="web-source:" + source["id"],
        )
        return manifest

    async def discover(self, browser, query, operation):
        candidates, seen, pages = [], set(), []
        for index in range(4):
            url = "https://www.google.com/search?" + urlencode(
                {"q": query, "num": 10, "start": index * 10}
            )
            page = await browser.discover_page(url)
            pages.append(
                {
                    "url": page["url"],
                    "results": page.get("results", []),
                    "notice": page.get("page_notice", ""),
                    "captured_at": now(),
                }
            )
            fresh = 0
            for hit in page.get("results", []):
                try:
                    target = canonical(hit["url"])
                except ValueError:
                    continue
                if target in seen:
                    continue
                seen.add(target)
                fresh += 1
                candidates.append(
                    {
                        "id": "source_" + digest(encoded([operation, target]))[:24],
                        "rank": len(candidates) + 1,
                        "url": target,
                        "title": hit.get("title", ""),
                        "snippet": hit.get("snippet", ""),
                    }
                )
                if len(candidates) == 20:
                    break
            if len(candidates) == 20 or not fresh:
                break
        return candidates, pages

    async def __call__(self, actor, job, args):
        if set(args) - {"query", "urls", "start", "width", "height"}:
            raise ValueError("Unknown browser capture option")
        query = args.get("query", "")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
            raise ValueError("Search query must contain 1–2000 characters")
        urls = args.get("urls")
        if urls is not None and (not isinstance(urls, list) or not 1 <= len(urls) <= 20):
            raise ValueError("Direct capture takes 1–20 URLs")
        if urls:
            urls = list(dict.fromkeys(canonical(u) for u in urls))
        geometry = {k: args[k] for k in ("start", "width", "height") if k in args}
        if (
            type(geometry.get("start", 0)) is not int
            or geometry.get("start", 0) < 0
            or geometry.get("width", 512) not in (512, 1024)
            or geometry.get("height", 2048) not in (512, 1024, 2048, 4096)
        ):
            raise ValueError("Invalid capture geometry")
        operation = "web_" + digest(encoded([job["id"], args]))[:24]
        old = self.store.by_key("web-completed:" + operation)
        if old:
            return self.store.json(old["data"]["result"])
        if self.store.by_key("web-started:" + operation):
            raise RuntimeError(
                "Unsettled Safari operation retained; do not replay accepted navigation"
            )
        self.store.event(
            "web.started",
            actor.scope,
            {
                "job": job["id"],
                "turn": job.get("turn"),
                "operation": operation,
                "query": query,
                "urls": urls,
            },
            key="web-started:" + operation,
        )
        folder = self.store.root / "artifacts/web" / now()[:10] / operation
        export = self.agents.path(actor) / "work/.sources/web" / operation
        sources, candidates, pages, error = [], [], [], None
        started = time.monotonic()
        try:
            async with asyncio.timeout(720), self.lock:
                async with self.browser_factory(self.browser_workspace, operation) as browser:
                    if urls:
                        candidates = [
                            {
                                "id": "source_" + digest(encoded([operation, u]))[:24],
                                "rank": n + 1,
                                "url": u,
                                "title": "",
                            }
                            for n, u in enumerate(urls)
                        ]
                    else:
                        candidates, pages = await self.discover(browser, query, operation)
                    discovery = {"candidates": candidates, "pages": pages}
                    self.retain(
                        actor,
                        operation,
                        None,
                        folder / "discovery.json",
                        encoded(discovery),
                        "application/json",
                    )
                    for source in candidates:
                        destination = folder / source["id"]
                        try:
                            try:
                                page = await browser.capture(source["url"], destination, **geometry)
                            except NavigationChanged:
                                await browser.reset()
                                page = await browser.capture(
                                    source["url"], destination / "navigation-retry", **geometry
                                )
                        except Exception as exc:
                            page = {
                                "status": "capture_failed",
                                "url": source["url"],
                                "text": "",
                                "images": [],
                                "error": clean(str(exc)),
                                "captured_at": now(),
                                "coverage": {"text": "Not inspected: capture failed"},
                            }
                        sources.append(
                            self.save(
                                actor,
                                job,
                                operation,
                                source,
                                page,
                                destination,
                                export / source["id"],
                            )
                        )
        except Exception as exc:
            error = type(exc).__name__ + ": " + clean(str(exc))
        requested = len(urls) if urls else 20
        result = {
            "status": "captured" if sources else "capture_failed",
            "query": query,
            "operation": operation,
            "search_receipt": {"operation": operation, "job": job["id"], "turn": job.get("turn")},
            "sources": sources,
            "requested_candidates": requested,
            "candidate_count": len(candidates),
            "discovery_complete": len(candidates) == requested,
            "discovery_limitation": None
            if len(candidates) == requested
            else "Fewer distinct accessible sources were discovered; no invented candidates.",
            "capture_complete": len(sources) == len(candidates) and bool(candidates),
            "error": error,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        ref = self.retain(
            actor, operation, None, folder / "capture.json", encoded(result), "application/json"
        )
        self.store.event(
            "web.completed",
            actor.scope,
            {"operation": operation, "result": ref},
            key="web-completed:" + operation,
        )
        return result
