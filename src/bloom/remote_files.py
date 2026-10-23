"""Standalone stdlib helper sent through SSH stdin; no remote install required."""

import fcntl
import base64
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path


def handle(request):
    root = Path(request["root"]).expanduser().resolve()
    path = Path(request["path"]).expanduser()
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    name = request["name"]
    if name == "read_file":
        with path.open("rb") as stream:
            data = stream.read(32_000_001)
        if len(data) > 32_000_000:
            raise ValueError("Use explicit process byte ranges for files above 32MB")
        mime = (
            "image/png"
            if data.startswith(b"\x89PNG\r\n\x1a\n")
            else "image/jpeg"
            if data.startswith(b"\xff\xd8\xff")
            else "image/gif"
            if data.startswith((b"GIF87a", b"GIF89a"))
            else "image/webp"
            if data.startswith(b"RIFF") and data[8:12] == b"WEBP"
            else None
        )
        if mime:
            if len(data) > 8_000_000:
                raise ValueError("Image exceeds the 8MB input limit")
            if "start" in request or "end" in request or request.get("exact"):
                raise ValueError("Line ranges and exact are text-only")
            return {
                "path": str(path),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "_image": base64.b64encode(data).decode(),
                "_mime": mime,
                "_detail": request.get("detail", "high"),
            }
        lines = data.decode().splitlines(keepends=True)
        start, end = request.get("start", 1), request.get("end", len(lines))
        if start < 1 or end < start:
            raise ValueError("Invalid range")
        return {
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "line_count": len(lines),
            "text": "".join(lines[start - 1 : end]),
        }
    lock_root = root / ".bloom-file-locks"
    lock_root.mkdir(exist_ok=True)
    with (lock_root / hashlib.sha256(str(path).encode()).hexdigest()).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = path.read_bytes() if path.exists() else None
        digest = hashlib.sha256(before).hexdigest() if before is not None else "absent"
        if digest != request["expected_sha256"]:
            raise ValueError("Stale file: " + digest)
        if name == "patch_file":
            text = before.decode() if before is not None else ""
            if not request["old"] or text.count(request["old"]) != 1:
                raise ValueError("Patch must match exactly one source range")
            data = text.replace(request["old"], request["new"], 1).encode()
        elif name == "write_file":
            data = request["text"].encode()
        else:
            raise ValueError("Unknown file action")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".pending")
        try:
            with temp.open("xb") as file:
                os.chmod(temp, path.stat().st_mode & 0o777 if path.exists() else 0o600)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, path)
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temp.unlink(missing_ok=True)
        return {
            "path": str(path),
            "before_sha256": digest,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }


if __name__ == "__main__":
    try:
        print(json.dumps({"result": handle(json.load(sys.stdin))}))
    except (ValueError, FileNotFoundError, UnicodeError) as exc:
        print(json.dumps({"error": str(exc), "uncertain": False}))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "uncertain": True}))
