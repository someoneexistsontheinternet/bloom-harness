"""Visual Mac control through public CoreGraphics APIs and screencapture."""

from __future__ import annotations

import ctypes as C
import fcntl
import subprocess
import time
from pathlib import Path


class Point(C.Structure):
    _fields_ = [("x", C.c_double), ("y", C.c_double)]


class Desktop:
    def __init__(self, root):
        self.root = Path(root)
        self.q = C.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self.a = C.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.q.CGPreflightScreenCaptureAccess.restype = C.c_bool
        self.a.AXIsProcessTrusted.restype = C.c_bool
        self.q.CGEventCreateMouseEvent.argtypes = [C.c_void_p, C.c_uint32, Point, C.c_uint32]
        self.q.CGEventCreateMouseEvent.restype = C.c_void_p
        self.q.CGEventCreateKeyboardEvent.argtypes = [C.c_void_p, C.c_uint16, C.c_bool]
        self.q.CGEventCreateKeyboardEvent.restype = C.c_void_p
        self.q.CGEventKeyboardSetUnicodeString.argtypes = [
            C.c_void_p,
            C.c_ulong,
            C.POINTER(C.c_uint16),
        ]
        self.q.CGEventSetFlags.argtypes = [C.c_void_p, C.c_uint64]
        self.q.CGEventPost.argtypes = [C.c_uint32, C.c_void_p]
        self.cf.CFRelease.argtypes = [C.c_void_p]

    def capabilities(self):
        return {
            "screen_recording": self.q.CGPreflightScreenCaptureAccess(),
            "accessibility": self.a.AXIsProcessTrusted(),
        }

    def post(self, event):
        if not event:
            raise RuntimeError("Mac event creation failed")
        try:
            self.q.CGEventPost(0, event)
        finally:
            self.cf.CFRelease(event)

    def screenshot(self, destination):
        if not self.capabilities()["screen_recording"]:
            raise PermissionError("Screen Recording permission is missing for this executable")
        p = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-t", "png", str(destination)], capture_output=True
        )
        if p.returncode or not Path(destination).exists():
            raise RuntimeError("Mac screenshot failed")
        return Path(destination).read_bytes()

    def act(self, action, *, x=0, y=0, text="", key="", observed_at=None, max_age=90):
        if not self.capabilities()["accessibility"]:
            raise PermissionError("Accessibility permission is missing for this executable")
        if observed_at is None or time.time() - observed_at > max_age:
            raise ValueError(
                f"Observe the current desktop before acting (observation expires after {max_age} seconds)"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "desktop.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if action in ("click", "move"):
                for kind in [1, 2] if action == "click" else [5]:
                    self.post(
                        self.q.CGEventCreateMouseEvent(None, kind, Point(float(x), float(y)), 0)
                    )
            elif action == "type":
                if len(text) > 10000:
                    raise ValueError("Type at most 10,000 characters at a time")
                raw = text.encode("utf-16-le")
                values = (C.c_uint16 * (len(raw) // 2)).from_buffer_copy(raw)
                for down in (True, False):
                    event = self.q.CGEventCreateKeyboardEvent(None, 0, down)
                    self.q.CGEventKeyboardSetUnicodeString(event, len(values), values)
                    self.post(event)
            elif action == "key":
                keys = {
                    "enter": 36,
                    "tab": 48,
                    "escape": 53,
                    "backspace": 51,
                    "space": 49,
                    "left": 123,
                    "right": 124,
                    "down": 125,
                    "up": 126,
                    "a": 0,
                    "c": 8,
                    "v": 9,
                    "l": 37,
                    "w": 13,
                }
                parts = key.lower().split("+")
                if parts[-1] not in keys or any(
                    p not in ("cmd", "shift", "alt", "ctrl") for p in parts[:-1]
                ):
                    raise ValueError("Unsupported key combination")
                flags = sum(
                    {"cmd": 1 << 20, "shift": 1 << 17, "alt": 1 << 19, "ctrl": 1 << 18}[p]
                    for p in parts[:-1]
                )
                for down in (True, False):
                    event = self.q.CGEventCreateKeyboardEvent(None, keys[parts[-1]], down)
                    self.q.CGEventSetFlags(event, flags)
                    self.post(event)
            else:
                raise ValueError("Unknown desktop action")
