"""Redact known credential values without logging or exporting the environment."""

import os
import re


def clean(value):
    secrets = [
        v
        for k, v in os.environ.items()
        if re.search(r"KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL", k, re.I)
        and not re.search(r"_(FILE|PATH|ENV|URL)$", k, re.I)
        and len(v) >= 8
    ]

    def redact(item):
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[REDACTED]")
        elif isinstance(item, dict):
            item = {k: redact(v) for k, v in item.items()}
        elif isinstance(item, list):
            item = [redact(v) for v in item]
        return item

    return redact(value)
