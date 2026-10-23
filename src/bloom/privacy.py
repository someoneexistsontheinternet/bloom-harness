"""Never archive authorization headers or known credential values."""

import os
import re

_secrets = set()


def register(value):
    if isinstance(value, str) and len(value) >= 8:
        _secrets.add(value)


for _name, _value in os.environ.items():
    if re.search(r"(?:^|_)(?:KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL)$", _name) and not _name.endswith(
        ("_FILE", "_PATH", "_ENV", "_URL")
    ):
        register(_value)


def clean(value):
    if isinstance(value, str):
        for secret in sorted(_secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(x) for x in value]
    return value


class StreamRedactor:
    """Preserve arbitrary output bytes and redact known credentials across reads."""

    def __init__(self):
        self.pending = b""

    def feed(self, data, final=False):
        data = self.pending + data
        secrets = sorted((s.encode() for s in _secrets), key=len, reverse=True)
        for secret in secrets:
            data = data.replace(secret, b"[REDACTED]")
        keep = 0
        if not final:
            for secret in secrets:
                start = max(0, len(data) - len(secret) + 1)
                while (position := data.find(secret[:1], start)) >= 0:
                    if secret.startswith(data[position:]):
                        keep = max(keep, len(data) - position)
                    start = position + 1
        self.pending = data[-keep:] if keep else b""
        return data[:-keep] if keep else data
