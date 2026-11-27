import hashlib
import os
from pathlib import Path
import uuid

INTERACTIVE = True


async def run(tool, path: str, text: str, expected_sha256: str | None = None):
    """Create a text file or replace its exact observed revision. Supply the SHA-256 from read_file when replacing. Memory changes require the owning model's explicit edit arguments."""
    if Path(path).parts and Path(path).parts[0] in ("memory", "shared"):
        return await tool.request(
            "memory_write", path=path, text=text, expected_sha256=expected_sha256
        )
    target = tool.path(path, write=True)
    current = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
    if current != expected_sha256:
        raise ValueError("File revision changed; read the current file before editing")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as output:
            output.write(text.encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": str(target),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "bytes": len(text.encode()),
    }
