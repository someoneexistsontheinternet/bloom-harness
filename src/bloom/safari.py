"""Sequential capture in a task-owned window of the owner's ordinary Safari.

Apple Events are the DOM bridge; window-only screenshots preserve the normal
profile's cookies without exporting them or attaching to unrelated tabs.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from .privacy import clean
from .safari_helper import SafariHelper
from .store import atomic, encoded, now

ASSETS = Path(__file__).with_name("web_assets")


class NavigationChanged(RuntimeError):
    """A changing document invalidated this capture, not the whole search."""


async def command(*args, timeout=30):
    proc = await asyncio.create_subprocess_exec(
        *map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException as exc:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        if isinstance(exc, TimeoutError):
            raise TimeoutError(f"{Path(args[0]).name} timed out after {timeout} seconds") from exc
        raise
    if proc.returncode:
        raise RuntimeError(
            clean(stderr.decode(errors="replace"))[:1800] or f"Command exited {proc.returncode}"
        )
    return stdout


def validate_url(url):
    value = urlsplit(url)
    if (
        value.scheme not in ("http", "https")
        or not value.hostname
        or value.username
        or value.password
    ):
        raise ValueError("A normal HTTP(S) URL without embedded credentials is required")
    return url


class Safari:
    def __init__(self, workspace, operation, settings=None):
        self.root = Path(workspace) / "web"
        self.operation = operation
        self.directory = self.root / operation
        self.settings = settings or {}
        self.window = None
        self.last_url = "about:blank"
        self.lock = None
        self.last = {}
        self.retired_windows = []
        self.native = SafariHelper(workspace)

    def checkpoint(self, status, **extra):
        atomic(
            self.directory / "browser.json",
            encoded(
                {
                    "operation_id": self.operation,
                    "window_id": self.window,
                    "last_url": self.last_url,
                    "pid": os.getpid(),
                    "status": status,
                    "updated_at": now(),
                    "retired_windows": self.retired_windows,
                    **extra,
                }
            ),
        )

    async def bridge(self, action, *args):
        parameters = [action] if action == "open" else [action, str(self.window), *map(str, args)]
        return (await self.native.call(*parameters)).strip()

    async def __aenter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root / "safari.lock").open("a")
        deadline = time.monotonic() + 60
        try:
            while True:
                try:
                    fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Another Bloom capture owns Safari; retry this search later"
                        )
                    await asyncio.sleep(0.5)
            self.checkpoint("opening")
            self.window = int(await self.bridge("open"))
            self.checkpoint("owned")
            await self.metrics()
            await self.image_helper()
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        try:
            if self.window is not None:
                try:
                    await self.reconcile_redirect()
                    await asyncio.shield(self.bridge("close", self.last_url))
                    self.checkpoint("closed")
                except Exception as exc:
                    self.checkpoint("cleanup_needs_review", error=clean(str(exc)))
        finally:
            if self.lock:
                self.lock.close()
                self.lock = None

    async def reconcile_redirect(self):
        current = await self.bridge("url")
        before = urlsplit(self.last_url)
        after = urlsplit(current)
        # Same-origin redirects in our single-tab window are normal (e.g. a
        # challenge returning to its requested page). A different origin or
        # added tab may be owner takeover; retain that window for inspection.
        if (before.scheme, before.netloc) == (after.scheme, after.netloc):
            self.last_url = current

    async def reset(self):
        old = {"window_id": self.window, "last_url": self.last_url, "at": now()}
        try:
            await self.reconcile_redirect()
            await self.bridge("close", self.last_url)
            old["status"] = "closed"
        except Exception as exc:
            old.update(status="cleanup_needs_review", error=clean(str(exc)))
        self.retired_windows.append(old)
        self.window = None
        self.last_url = "about:blank"
        self.checkpoint("reopening")
        self.window = int(await self.bridge("open"))
        self.checkpoint("owned")

    async def evaluate(self, mode, **options):
        # Files avoid shell/AppleScript interpolation of untrusted URLs or text.
        code = "(function(){\nconst module = undefined;\n"
        if mode == "extract":
            code += (ASSETS / "Readability.js").read_text() + "\n"
        code += (ASSETS / "extract.js").read_text().replace(
            "BLOOM_OPTIONS", json.dumps({"mode": mode, **options}), 1
        ) + "\n})()"
        path = self.directory / "evaluate.js"
        atomic(path, code.encode())
        raw = await self.bridge("eval", path)
        if len(raw) > self.settings.get("max_source_chars", 2_000_000) * 3:
            raise ValueError(
                "Rendered DOM extraction exceeds the configured source limit; no silent truncation"
            )
        return json.loads(raw)

    async def metrics(self):
        self.last = await self.evaluate("metrics")
        return self.last

    async def navigate(self, url):
        validate_url(url)
        if self.window is None:
            raise RuntimeError("Safari is not open")
        # If the owner took over our window, stop rather than navigate it away.
        current = await self.bridge("url")
        if current != self.last_url:
            raise NavigationChanged("Task-owned Safari window changed outside this capture")
        self.checkpoint("navigating", requested_url=url)
        await self.bridge("navigate", url)
        deadline = time.monotonic() + self.settings.get("page_timeout_seconds", 20)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.35)
            try:
                state = await self.metrics()
            except (RuntimeError, ValueError):
                continue
            if (
                state["url"] != current
                and state["url"] != "about:blank"
                and state["ready"] in ("interactive", "complete")
            ):
                self.last_url = state["url"]
                await asyncio.sleep(self.settings.get("settle_seconds", 0.6))
                self.last = await self.metrics()
                self.last_url = self.last["url"]
                self.checkpoint("loaded", requested_url=url)
                return self.last
            if current == url and state["url"] == url and state["ready"] == "complete":
                self.last_url = url
                return state
        # Record the actual URL for safe cleanup, but do not claim successful load.
        self.last_url = await self.bridge("url")
        self.checkpoint("navigation_timeout", requested_url=url)
        raise TimeoutError("Safari did not finish navigating within the page deadline")

    async def discover_page(self, url):
        await self.navigate(url)
        return await self.evaluate("search")

    async def image_helper(self):
        source = ASSETS / "image_strip.swift"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        folder = self.root / "native"
        folder.mkdir(exist_ok=True)
        binary = folder / ("image-strip-" + digest)
        if not binary.exists():
            pending = binary.with_suffix(".pending")
            output = await command(
                "/usr/bin/swiftc",
                "-O",
                "-module-cache-path",
                folder / "module-cache",
                source,
                "-o",
                pending,
                timeout=60,
            )
            atomic(folder / ("build-" + digest + ".log"), output)
            os.replace(pending, binary)
        return binary

    async def capture(self, url, destination, *, start=0, width=512, height=2048):
        if start < 0 or width not in (512, 1024) or height not in (512, 1024, 2048, 4096):
            raise ValueError("Invalid capture geometry")
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        started = now()
        clock = time.monotonic()
        await self.bridge("resize", width, 950)
        await self.navigate(url)
        page = await self.evaluate("extract")
        self.last_url = page["url"]
        if page["status"] != "ok":
            return {
                **page,
                "captured_at": now(),
                "capture_started_at": started,
                "images": [],
                "elapsed_seconds": round(time.monotonic() - clock, 3),
            }
        if len(page["text"]) > self.settings.get("max_source_chars", 2_000_000):
            raise ValueError(
                "Page text exceeds the configured source limit; original not silently shortened"
            )
        # Crop 512px sections from a taller viewport, avoiding rounded bottom
        # window corners and leaving room to exclude repeated sticky headers.
        geometry = await self.metrics()
        if geometry["width"] != width or geometry["height"] < 512:
            raise RuntimeError("Safari did not provide the requested viewport geometry")
        parts, coverage = [], []
        success = False
        cursor = start
        try:
            while cursor < start + height:
                await self.evaluate("scroll", start=cursor)
                await asyncio.sleep(0.2)
                state = await self.metrics()
                if cursor and state.get("occlusion_top", 0):
                    await self.evaluate("scroll", start=max(0, cursor - state["occlusion_top"]))
                    await asyncio.sleep(0.1)
                    state = await self.metrics()
                if state["url"] != self.last_url:
                    raise NavigationChanged("Page navigated during screenshot capture")
                available = min(512, start + height - cursor, state["page_height"] - cursor)
                if available <= 0:
                    break
                scroll = state["scroll_y"]
                dpr = state["dpr"]
                if scroll > cursor or cursor - scroll + available > state["height"]:
                    raise RuntimeError("Requested screenshot range is not visible")
                # macOS screencapture silently skips dot-prefixed filenames.
                path = destination / f"viewport-{cursor}.png"
                await command(
                    "/usr/sbin/screencapture", "-x", "-o", "-l", self.window, path, timeout=10
                )
                if not path.is_file():
                    raise RuntimeError("Window screenshot returned no image file")
                parts.append(
                    {
                        "path": str(path),
                        "x": 0,
                        "y": round(
                            (state["outerHeight"] - state["height"] + cursor - scroll) * dpr
                        ),
                        "width": round(width * dpr),
                        "height": round(available * dpr),
                        "outputHeight": available,
                    }
                )
                coverage.append(
                    {"start_y": cursor, "end_y": cursor + available, "observed_scroll_y": scroll}
                )
                cursor += available
            if not parts:
                raise ValueError("Requested scroll range is beyond the loaded page")
            output = destination / f"render-{start:06d}-{cursor:06d}-{width}px.png"
            job = self.directory / "image-job.json"
            atomic(job, encoded({"output": str(output), "width": width, "parts": parts}))
            image_size = json.loads(await command(await self.image_helper(), job))
            # Extract again after scrolling: newly loaded text is included explicitly.
            page = await self.evaluate("extract")
            if page["url"] != self.last_url:
                raise NavigationChanged("Page changed during extraction")
            if len(page.get("text", "")) > self.settings.get("max_source_chars", 2_000_000):
                raise ValueError(
                    "Page text exceeded the source limit after scrolling; no silent truncation"
                )
            success = True
            return {
                **page,
                "captured_at": now(),
                "capture_started_at": started,
                "elapsed_seconds": round(time.monotonic() - clock, 3),
                "images": [
                    {
                        "path": str(output),
                        "mime": "image/png",
                        "detail": "high",
                        **image_size,
                        "start_y": start,
                        "end_y": cursor,
                    }
                ],
                "capture_coverage": coverage,
                "more_below": page["page_height"] > cursor,
                "safari_session": "ordinary signed-in Safari, task-owned window; no cookie export",
            }
        finally:
            if success:
                for part in parts:
                    Path(part["path"]).unlink(missing_ok=True)
