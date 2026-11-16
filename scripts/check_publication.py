"""Reject runtime/private files and obvious credentials in public source or distributions."""

import argparse
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {
    ".bloom",
    ".bloom-demo",
    "workspace",
    "artifacts",
    "logs",
    "notes",
    "worktrees",
    ".venv",
    "__pycache__",
}
PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub credential": re.compile(
        rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"
    ),
    "API credential": re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}"),
}


def check(name, data):
    """Report only a path and finding type, never matched credential bytes."""
    path = Path(name)
    if (
        FORBIDDEN.intersection(path.parts)
        or path.name.startswith(".env")
        or path.name == "config.local.toml"
        or path.suffix in {".sqlite3", ".pem", ".key", ".p12"}
    ):
        raise ValueError("Private/runtime file in publication: " + name)
    for label, pattern in PATTERNS.items():
        if pattern.search(data):
            raise ValueError(label + " pattern in " + name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args()
    files = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    checked = 0
    for name in files:
        if not name or not (ROOT / name).is_file():
            continue
        check(name, (ROOT / name).read_bytes())
        checked += 1
    if args.dist:
        for path in sorted(args.dist.iterdir()):
            if path.suffix == ".whl":
                with zipfile.ZipFile(path) as archive:
                    for name in archive.namelist():
                        check(name, archive.read(name))
            elif path.name.endswith(".tar.gz"):
                with tarfile.open(path) as archive:
                    for member in archive.getmembers():
                        if member.isfile():
                            check(member.name, archive.extractfile(member).read())
    print(
        f"Publication guard passed: {checked} source files; credential-pattern scan is bounded, not a security certification."
    )


if __name__ == "__main__":
    main()
