"""Sanitized failures with useful types and frame locations, never local values."""

from __future__ import annotations

import traceback

from .privacy import clean


def describe(exc, **context):
    causes = []
    current = exc
    seen = set()
    while current is not None and id(current) not in seen and len(causes) < 5:
        seen.add(id(current))
        causes.append(
            {
                "type": type(current).__module__ + "." + type(current).__name__,
                "message": clean(str(current))[:2000],
                "frames": [
                    {"file": f.filename, "line": f.lineno, "function": f.name}
                    for f in traceback.extract_tb(current.__traceback__)[-16:]
                ],
            }
        )
        current = current.__cause__ or current.__context__
    return clean({"exception": causes[0], "causes": causes[1:], **context})


def label(exc):
    message = clean(str(exc)).strip()
    return type(exc).__name__ + (
        ": " + message if message else ": transport/control failure without a message"
    )
