"""Check tracked source for accidental private data and broken local doc links.

Reports paths/line numbers/categories, never matching secret values. This is a
focused publication guard, not a complete credential or Git-history scanner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit


FORBIDDEN = {
    "workspace",
    "artifacts",
    "logs",
    "memories",
    "worktrees",
    "notes",
    "codex-misc",
    ".venv",
    "dist",
    "build",
}
PATTERNS = {
    "Discord-shaped token": re.compile(
        r"(?:mfa\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,})"
    ),
    "JWT-shaped literal": re.compile(
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    ),
    "credential-shaped literal": re.compile(
        r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,}|xox[baprs]-[A-Za-z0-9-]{20,})"
    ),
    "private-key block": re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"),
    "personal home path": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
    "private Discord message URL": re.compile(r"https://discord\.com/channels/\d+"),
    "credential in URL": re.compile(r"https?://[^\s/:@]+:[^\s/@]+@"),
}


def check_package(path, tracked):
    """Reject private/generated paths even when a build itself succeeds."""
    issues = []
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        relative = names
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path) as archive:
            entries = archive.getmembers()
            names = [entry.name for entry in entries if entry.isfile()]
            if any(not (entry.isfile() or entry.isdir()) for entry in entries):
                issues.append({"path": path.name, "category": "Non-regular package member"})
        relative = ["/".join(Path(name).parts[1:]) for name in names]
    else:
        return []
    for name, member in zip(names, relative, strict=True):
        parts = Path(name).parts
        if (
            Path(name).is_absolute()
            or ".." in parts
            or FORBIDDEN.intersection(parts)
            or "__pycache__" in parts
            or any(p.startswith(".env") for p in parts)
            or name.endswith(("config/local.toml", ".pyc", ".key", ".p12", ".pfx"))
        ):
            issues.append(
                {
                    "path": path.name,
                    "member": name,
                    "category": "Private/generated or unsafe package path",
                }
            )
        elif path.name.endswith(".tar.gz") and member not in tracked and member != "PKG-INFO":
            issues.append(
                {
                    "path": path.name,
                    "member": name,
                    "category": "Source package contains an untracked file",
                }
            )
        elif path.suffix == ".whl" and not (
            name.startswith("bloom/") or any(p.endswith(".dist-info") for p in parts)
        ):
            issues.append(
                {"path": path.name, "member": name, "category": "Unexpected wheel member"}
            )
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, help="Also inspect built wheels/source archives")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    names = [name for name in result.stdout.decode().split("\0") if name]
    findings = []
    if not names:
        findings.append({"path": ".", "category": "No tracked/staged source files to inspect"})
    for name in names:
        relative = Path(name)
        path = root / relative
        if (
            relative.parts[0] in FORBIDDEN
            or name.startswith(("scripts/codex-misc/", "docs/requirements/", "docs/reports/"))
            or relative.name.startswith(".env")
            or name == "config/local.toml"
            or re.search(r"\.(?:sqlite3?(?:-wal|-shm)?|db|key|p12|pfx)$", name)
        ):
            findings.append({"path": name, "category": "Private/generated path is tracked"})
        if path.is_symlink() or not path.is_file():
            findings.append({"path": name, "category": "Tracked path is not an ordinary file"})
            continue
        data = path.read_bytes()
        if len(data) > 5_000_000:
            findings.append({"path": name, "category": "Unexpectedly large source file"})
        try:
            text = data.decode()
        except UnicodeDecodeError:
            findings.append({"path": name, "category": "Unexpected binary in source draft"})
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for category, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append({"path": name, "line": number, "category": category})
        if path.suffix == ".md":
            prose = re.sub(r"```.*?```", "", text, flags=re.S)
            for match in re.finditer(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", prose):
                target = match.group(1).strip().strip("<>")
                parsed = urlsplit(target)
                if parsed.scheme or target.startswith("#") or not parsed.path:
                    continue
                destination = (path.parent / unquote(parsed.path)).resolve()
                if not destination.is_relative_to(root) or not destination.exists():
                    findings.append(
                        {
                            "path": name,
                            "category": "Broken/outside local Markdown link",
                            "target": target,
                        }
                    )
    config = tomllib.loads((root / "config/default.toml").read_text())
    if config["discord"]["enabled"] or config["storage"]["enabled"]:
        findings.append(
            {
                "path": "config/default.toml",
                "category": "External integration enabled in distributed defaults",
            }
        )
    if any(
        config["discord"][key]
        for key in ("owner_id", "bot_id", "channel_id", "guild_id", "activity_channel_id")
    ):
        findings.append(
            {
                "path": "config/default.toml",
                "category": "Distributed account/channel IDs must be empty",
            }
        )
    if any(
        not profile["model"].startswith("configure-") for profile in config["profiles"].values()
    ):
        findings.append(
            {
                "path": "config/default.toml",
                "category": "Distributed model selection is not explicit",
            }
        )
    provenance = json.loads((root / "src/bloom/web_assets/readability-provenance.json").read_text())
    for name, metadata in provenance["files"].items():
        path = root / "src/bloom/web_assets" / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != metadata["sha256"]:
            findings.append(
                {
                    "path": str(path.relative_to(root)),
                    "category": "Vendored Readability provenance mismatch",
                }
            )
    packages = []
    if args.dist:
        packages = sorted(
            p for p in args.dist.iterdir() if p.suffix == ".whl" or p.name.endswith(".tar.gz")
        )
        if not packages:
            findings.append(
                {"path": str(args.dist), "category": "No distribution archives to inspect"}
            )
        for package in packages:
            findings.extend(check_package(package, set(names)))
    print(
        json.dumps(
            {
                "checked_files": len(names),
                "accepted": not findings,
                "findings": findings[:40],
                "finding_count": len(findings),
                "packages_checked": [p.name for p in packages],
                "scope": "Tracked working files, known credential/private-path patterns, defaults, vendored hashes and local Markdown links. Not exhaustive secret detection or historical-object scanning.",
            },
            indent=2,
        )
    )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
