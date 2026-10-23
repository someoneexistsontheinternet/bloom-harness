"""Build and call the named Safari helper; never prompt from a service request."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .privacy import clean
from .store import atomic, encoded

ASSETS = Path(__file__).with_name("web_assets")
IDENTIFIER = "dev.bloom.browser-helper"
PROTOCOL = 1
COUNTS = {
    "open": 0,
    "url": 1,
    "navigate": 2,
    "eval": 2,
    "resize": 3,
    "close": 2,
    "check": 0,
    "setup": 0,
}
FILES = ("request.json", "started.json", "response.json", "cancelled", "stdout.txt", "stderr.txt")


def layout(workspace):
    web = Path(workspace).resolve() / "web"
    return web, web / "native" / "Bloom Browser Helper.app", web / "native" / "browser-helper.json"


def fingerprint(workspace):
    digest = hashlib.sha256(str(Path(workspace).resolve()).encode())
    for name in ("browser_helper.swift", "safari.applescript"):
        digest.update(name.encode())
        digest.update((ASSETS / name).read_bytes())
    digest.update(str(PROTOCOL).encode())
    return digest.hexdigest()


def prepare(root, log_directory):
    root = Path(root).resolve()
    workspace = root / "workspace"
    web, app, manifest_path = layout(workspace)
    build = fingerprint(workspace)
    if manifest_path.exists() and app.exists():
        old = json.loads(manifest_path.read_text())
        if old["build_id"] == build:
            subprocess.run(
                ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                check=True,
                capture_output=True,
            )
            return old
    # Never replace a running helper (including another checkout's build).
    if (
        subprocess.run(
            [
                "/usr/bin/pgrep",
                "-f",
                "/Bloom Browser Helper[.]app/Contents/MacOS/BloomBrowserHelper$",
            ],
            capture_output=True,
        ).returncode
        == 0
    ):
        raise RuntimeError("Close Bloom Browser Helper before preparing a different build")
    log_directory = Path(log_directory)
    log_directory.mkdir(parents=True, exist_ok=True)
    app.parent.mkdir(parents=True, exist_ok=True)
    pending = app.with_name("Bloom Browser Helper.build-" + uuid.uuid4().hex + ".app")
    (pending / "Contents/MacOS").mkdir(parents=True)
    (pending / "Contents/Resources").mkdir()
    shutil.copyfile(
        ASSETS / "safari.applescript", pending / "Contents/Resources/safari.applescript"
    )
    info = {
        "CFBundleIdentifier": IDENTIFIER,
        "CFBundleName": "Bloom Browser Helper",
        "CFBundleDisplayName": "Bloom Browser Helper",
        "CFBundleExecutable": "BloomBrowserHelper",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": "0.1",
        "NSPrincipalClass": "NSApplication",
        "NSHighResolutionCapable": True,
        "LSUIElement": True,
        "NSAppleEventsUsageDescription": "Bloom opens and reads research pages in your existing signed-in Safari session.",
        "BloomWebRoot": str(web),
        "BloomBuildID": build,
    }
    atomic(pending / "Contents/Info.plist", plistlib.dumps(info))
    entitlement = log_directory / "entitlements.plist"
    atomic(entitlement, plistlib.dumps({"com.apple.security.automation.apple-events": True}))
    commands = [
        [
            "/usr/bin/swiftc",
            "-O",
            "-parse-as-library",
            "-module-cache-path",
            str(web / "native/module-cache"),
            str(ASSETS / "browser_helper.swift"),
            "-o",
            str(pending / "Contents/MacOS/BloomBrowserHelper"),
        ],
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            "--options",
            "runtime",
            "--entitlements",
            str(entitlement),
            "--identifier",
            IDENTIFIER,
            str(pending),
        ],
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(pending)],
    ]
    receipts = []
    for index, command in enumerate(commands):
        with (log_directory / f"build-{index}.log").open("wb") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=60)
        receipts.append({"command": command, "exit_code": result.returncode})
        atomic(
            log_directory / "build.json",
            encoded({"build_id": build, "commands": receipts, "pending_app": str(pending)}),
        )
        if result.returncode:
            raise RuntimeError(
                f"Native helper build failed; inspect {log_directory / f'build-{index}.log'}"
            )
    if app.exists():
        previous = root / "artifacts" / "native-helper" / ("previous-" + uuid.uuid4().hex)
        previous.mkdir(parents=True)
        os.replace(app, previous / app.name)
        if manifest_path.exists():
            shutil.copyfile(manifest_path, previous / "manifest.json")
    os.replace(pending, app)
    manifest = {
        "build_id": build,
        "protocol": PROTOCOL,
        "app": str(app),
        "workspace": str(workspace),
        "bundle_id": IDENTIFIER,
        "signature": "local ad-hoc; binary updates can require a new OS grant",
        "executable_sha256": hashlib.sha256(
            (app / "Contents/MacOS/BloomBrowserHelper").read_bytes()
        ).hexdigest(),
        "prepared_at": time.time(),
    }
    atomic(manifest_path, encoded(manifest))
    atomic(log_directory / "manifest.json", encoded(manifest))
    return manifest


class SafariHelper:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.web, self.app, self.manifest_path = layout(workspace)
        self.spool = self.web / "bridge"

    def manifest(self):
        if not self.manifest_path.is_file() or not self.app.is_dir():
            raise RuntimeError(
                "Bloom Browser Helper is not prepared. Run python -m bloom.safari_helper prepare --root PROJECT with the project interpreter."
            )
        value = json.loads(self.manifest_path.read_text())
        if (
            value.get("build_id") != fingerprint(self.workspace)
            or value.get("protocol") != PROTOCOL
        ):
            raise RuntimeError(
                "Bloom Browser Helper differs from this source; prepare the matching build before using Safari"
            )
        return value

    async def dispatch(self, request):
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/open",
            "-g",
            "-j",
            "-a",
            str(self.app),
            str(request),
            "--args",
            "--serve",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, error = await asyncio.wait_for(process.communicate(), 5)
        except BaseException:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()
            raise
        if process.returncode:
            raise RuntimeError(
                clean(error.decode(errors="replace"))[:1200]
                or "Could not launch Bloom Browser Helper"
            )

    def retire(self, directory, failed=False):
        # Only terminal receipts may move; an in-flight helper still owns its paths.
        if not (directory / "response.json").is_file():
            return
        if failed:
            request = json.loads((directory / "request.json").read_text())
            action = request.get("action") if request.get("action") in COUNTS else "invalid"
            destination = (
                self.workspace.parent
                / "logs"
                / "browser-helper"
                / time.strftime("%Y-%m-%d")
                / (action + "-" + directory.name)
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                os.replace(directory, destination)
        else:
            for name in FILES:
                (directory / name).unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass

    def sweep(self):
        if not self.spool.exists():
            return
        for directory in self.spool.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                uuid.UUID(hex=directory.name)
                if time.time() - directory.stat().st_mtime < 120:
                    continue
                response_path = directory / "response.json"
                if not response_path.exists():
                    request = json.loads((directory / "request.json").read_text())
                    if request["deadline"] >= time.time() - 120:
                        continue
                    atomic(directory / "cancelled", b"Expired orphan; do not start effects.\n")
                    atomic(
                        response_path,
                        encoded(
                            {
                                "id": request["id"],
                                "build_id": request["build_id"],
                                "ok": False,
                                "kind": "uncertain",
                                "error": "Client expiry receipt: helper did not provide a terminal result",
                                "receipt_source": "client_expiry",
                                "expired_at": time.time(),
                            }
                        ),
                    )
                response = json.loads(response_path.read_text())
                # A success arriving after caller cancellation is still an
                # unconsumed effect; retain its result for recovery.
                self.retire(
                    directory, failed=not response.get("ok") or (directory / "cancelled").exists()
                )
            except (OSError, ValueError):
                continue

    async def call(self, action, *args, timeout=30):
        if action not in COUNTS or len(args) != COUNTS[action]:
            raise ValueError("Invalid Safari helper operation")
        manifest = self.manifest()
        self.spool.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.spool, 0o700)
        self.sweep()
        identity = uuid.uuid4().hex
        directory = self.spool / identity
        directory.mkdir(mode=0o700)
        payload = {
            "protocol": PROTOCOL,
            "id": identity,
            "build_id": manifest["build_id"],
            "action": action,
            "args": list(map(str, args)),
            "deadline": time.time() + timeout,
            "caller_pid": os.getpid(),
        }
        atomic(directory / "request.json", encoded(payload))
        response = None
        succeeded = False
        try:
            await self.dispatch(directory / "request.json")
            deadline = time.monotonic() + timeout
            while not (directory / "response.json").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Bloom Browser Helper timed out; the request is cancelled. Receipt: {directory}"
                    )
                await asyncio.sleep(0.05)
            response = json.loads((directory / "response.json").read_text())
            if response.get("id") != identity or response.get("build_id") != manifest["build_id"]:
                raise RuntimeError("Native helper response identity/build mismatch")
            if not response.get("ok"):
                message = clean(str(response.get("error", "Native Safari operation failed")))[:1800]
                if response.get("kind") == "permission":
                    raise PermissionError(message)
                raise RuntimeError(message)
            succeeded = True
            return response.get("stdout", "")
        except BaseException:
            atomic(
                directory / "cancelled", b"Caller stopped waiting; do not start another effect.\n"
            )
            raise
        finally:
            if response is not None:
                self.retire(directory, failed=not succeeded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check", "setup"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--logs", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        if args.logs is None:
            parser.error("--logs is required for prepare")
        print(json.dumps(prepare(args.root, args.logs), indent=2))
    else:
        print(asyncio.run(SafariHelper(args.root / "workspace").call(args.command)))


if __name__ == "__main__":
    main()
