"""Pinned standalone command dependencies; installation is explicit, never inference."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

from .store import atomic, encoded, now


VERSION = "15.2.0"
PACKAGE = "ripgrep-" + VERSION + "-aarch64-apple-darwin"
URL = (
    "https://github.com/BurntSushi/ripgrep/releases/download/" + VERSION + "/" + PACKAGE + ".tar.gz"
)
ARCHIVE_SHA256 = "3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4"
BINARY_SHA256 = "a326a1fb48074202e9ad41e4cd1e389eeea372c8c6f7d7e80da81176d5d9430e"


def directory(root):
    return Path(root) / "workspace/tools" / PACKAGE


def search_path(root, inherited="/usr/bin:/bin:/usr/sbin:/sbin"):
    # Prefer real Git/tool binaries over Apple's /usr/bin xcrun shims, which
    # attempt writes to an unrelated user cache even for read-only commands.
    paths = [
        str(Path(root) / ".venv/bin"),
        str(directory(root)),
        "/opt/homebrew/bin",
        "/Library/Developer/CommandLineTools/usr/bin",
        *inherited.split(":"),
    ]
    return ":".join(dict.fromkeys(path for path in paths if path))


def verify(root):
    binary = directory(root) / "rg"
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError(
            "Standalone ripgrep is missing; run the project interpreter with -m bloom.toolchain prepare --root "
            + str(root)
        )
    if hashlib.sha256(binary.read_bytes()).hexdigest() != BINARY_SHA256:
        raise RuntimeError("Standalone ripgrep differs from its pinned release; refuse deployment")
    return {"path": str(binary), "version": VERSION, "sha256": BINARY_SHA256}


def prepare(root, archive=None):
    if (platform.system(), platform.machine()) != ("Darwin", "arm64"):
        raise RuntimeError(
            "This pinned package targets the configured Apple Silicon Mac; select a verified package for another platform"
        )
    root = Path(root).resolve()
    target = directory(root)
    if target.exists():
        return verify(root)  # Never replace a dependency an active release may use.
    if target.resolve() != target:
        raise ValueError("Managed tools must remain at their ordinary local workspace path")
    if archive is None:
        archive = root / "workspace/tools/downloads" / (PACKAGE + ".tar.gz")
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            with urllib.request.urlopen(URL, timeout=30) as response:
                data = response.read(8_000_001)
            if len(data) > 8_000_000:
                raise ValueError("Unexpected ripgrep package size")
            if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
                raise ValueError("Ripgrep package checksum differs from the pinned release")
            atomic(archive, data)
    data = Path(archive).read_bytes()
    if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Ripgrep package checksum differs from the pinned release")
    with tarfile.open(archive) as source:
        files = {}
        for name in ("rg", "COPYING", "LICENSE-MIT", "UNLICENSE"):
            member = source.getmember(PACKAGE + "/" + name)
            if not member.isfile() or member.size > 8_000_000:
                raise ValueError("Unexpected executable/license archive entry")
            files[name] = source.extractfile(member).read()
    if hashlib.sha256(files["rg"]).hexdigest() != BINARY_SHA256:
        raise ValueError("Ripgrep binary differs from the pinned executable")
    target.mkdir(parents=True, exist_ok=False)
    for name, value in files.items():
        atomic(target / name, value)
    (target / "rg").chmod(0o755)
    atomic(
        target / "provenance.json",
        encoded(
            {
                "installed_at": now(),
                "source": URL,
                "archive_sha256": ARCHIVE_SHA256,
                "binary_sha256": BINARY_SHA256,
                "version": VERSION,
            }
        ),
    )
    return verify(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(args.root, args.archive) if args.action == "prepare" else verify(args.root),
            indent=2,
        )
    )
