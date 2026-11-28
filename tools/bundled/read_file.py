import hashlib
import json
from bisect import bisect_left, bisect_right

INTERACTIVE = True


async def run(
    tool,
    path: str,
    start: int = 1,
    end: int | None = None,
    start_char: int | None = None,
    end_char: int | None = None,
):
    """Read exact text without inference, list a directory, or expose a native image. Use 1-based inclusive line ranges OR 0-based half-open character ranges. Large ranges return an exact page with next_start_char and requested_end_char; continue with those character bounds. Paths accept published source:<sha256> IDs, work-relative paths, or explicit memory/tools/inbox/history paths."""
    target = tool.path(path)
    if target.is_dir():
        return {
            "path": str(target),
            "entries": [
                {"name": p.name, "directory": p.is_dir()} for p in sorted(target.iterdir())
            ],
        }
    raw = target.read_bytes()
    mime = tool.media(path)
    if mime.startswith("image/"):
        return {
            "path": str(target),
            "image_path": str(target),
            "media": mime,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    text = raw.decode()
    lines = text.splitlines(keepends=True)
    ends, position = [], 0
    for line in lines:
        position += len(line)
        ends.append(position)
    if start_char is not None or end_char is not None:
        if start != 1 or end is not None:
            raise ValueError("Choose line bounds or character bounds, not both")
        left, right = start_char or 0, len(text) if end_char is None else end_char
        if not 0 <= left <= right <= len(text):
            raise ValueError("Character bounds are outside the file")
        first_line, last_line = None, None
    else:
        last = len(lines) if end is None else end
        if start < 1 or last < start and lines or start > len(lines) + 1:
            raise ValueError("Invalid 1-based inclusive line range")
        left, right = len("".join(lines[: start - 1])), len("".join(lines[:last]))
        first_line, last_line = start, min(last, len(lines))
    checksum = hashlib.sha256(raw).hexdigest()
    result = {
        "path": str(target),
        "source_id": "source:" + checksum,
        "sha256": checksum,
        "requested_start_line": first_line,
        "requested_end_line": last_line,
        "start_char": left,
        "requested_end_char": right,
        "total_chars": len(text),
        "total_lines": len(lines),
        "reference_files": [str(target)],
    }
    # Bound the serialized page as well as its text. Quotes/newlines in a JSON
    # source must not accidentally push an otherwise small read out of context.
    low, high = left, min(right, left + 45_000)
    while low < high:
        take = (low + high + 1) // 2
        page = {
            **result,
            "text": text[left:take],
            "end_char": take,
            "next_start_char": take if take < right else None,
        }
        if len(json.dumps(page, ensure_ascii=False)) <= 48_000:
            low = take
        else:
            high = take - 1
    return {
        **result,
        "text": text[left:low],
        "start": bisect_right(ends, left) + 1,
        "end": bisect_left(ends, low) + 1 if low > left else 0,
        "end_char": low,
        "next_start_char": low if low < right else None,
    }
