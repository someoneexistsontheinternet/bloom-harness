import hashlib

INTERACTIVE = True


async def run(tool, path: str, start: int = 1, end: int | None = None):
    """Read an exact file range, list a directory, or expose an image as native image input. Paths are relative to your work directory; use memory/, tools/, inbox/ or history/ explicitly."""
    target = tool.path(path)
    if target.is_dir():
        return {
            "path": str(target),
            "entries": [
                {"name": p.name, "directory": p.is_dir()} for p in sorted(target.iterdir())
            ],
        }
    raw = target.read_bytes()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(target.suffix.lower(), "text/plain")
    if mime.startswith("image/"):
        return {
            "path": str(target),
            "image_path": str(target),
            "media": mime,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    text = raw.decode()
    lines = text.splitlines(keepends=True)
    if not lines and start == 1 and end is None:
        return {
            "path": str(target),
            "text": "",
            "start": 1,
            "end": 0,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    last = len(lines) if end is None else end
    if start < 1 or last < start or start > len(lines) + 1:
        raise ValueError("Invalid 1-based inclusive line range")
    selected = "".join(lines[start - 1 : last])
    if len(selected) > 50_000:
        return await tool.invoke(
            "extract",
            {
                "path": str(target),
                "question": "Extract evidence useful to the current request; preserve relevant exact passages and explain coverage.",
            },
        )
    return {
        "path": str(target),
        "text": selected,
        "start": start,
        "end": min(last, len(lines)),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
