"""Reviewed TOML worker presets; no source interpolation or hidden launches."""

from __future__ import annotations

import json
import tomllib

from .privacy import clean
from .store import now


NAMES = (
    "recall",
    "inspect-files",
    "compare-evidence",
    "check-result",
    "sol-research",
    "sol-implementation",
)
BINDINGS = {"question": str, "cwd": str, "sources": list, "criteria": str}


def bundled(config, value):
    """Relative catalog paths deliberately resolve in the active frozen source."""
    for name in NAMES:
        relative = "config/agent-presets/" + name + ".toml"
        path = config.source / relative
        if value in (relative, str(path)):
            if (
                path.is_symlink()
                or path.resolve().parent != (config.source / "config/agent-presets").resolve()
            ):
                raise ValueError("Bundled preset must be an ordinary release file")
            return path
    return None


def expand(document, bindings):
    expected = {"preset_version", "kind", "description", "instructions", "required", "optional"}
    if (
        set(document) != expected
        or type(document["preset_version"]) is not int
        or document["preset_version"] != 1
        or document["kind"] not in ("recall", "evidence", "sol")
    ):
        raise ValueError("Unsupported worker preset contract")
    required, optional = document["required"], document["optional"]
    if (
        not isinstance(required, list)
        or not isinstance(optional, list)
        or not all(isinstance(k, str) for k in [*required, *optional])
    ):
        raise ValueError("Preset bindings must be declared by name")
    names = required + optional
    if len(names) != len(set(names)) or not set(names) <= BINDINGS.keys():
        raise ValueError("Unsupported or duplicate preset binding")
    if (
        not isinstance(bindings, dict)
        or set(bindings) - set(names)
        or not set(required) <= set(bindings)
    ):
        raise ValueError("Provide exactly the required/optional bindings declared by this preset")
    for key, value in bindings.items():
        if not isinstance(value, BINDINGS[key]) or (isinstance(value, str) and not value.strip()):
            raise ValueError("Invalid preset binding: " + key)
        if isinstance(value, list) and (
            not value or not all(isinstance(v, str) and v.strip() for v in value)
        ):
            raise ValueError("sources must be a nonempty list of exact references")
    if not isinstance(document["instructions"], str) or not document["instructions"].strip():
        raise ValueError("Preset instructions must describe a bounded assignment")
    if "question" not in bindings or (document["kind"] == "evidence" and "cwd" not in bindings):
        raise ValueError("Workers require question; file evidence also requires cwd")
    # Bindings remain data after parsing; quotes/newlines cannot inject TOML.
    if document["kind"] == "sol":
        return {
            "operation": "delegate",
            "role": "sol",
            "mode": "full",
            "network": True,
            "objective": document["instructions"]
            + "\nAssignment inputs:\n"
            + json.dumps(bindings, ensure_ascii=False),
            **({"cwd": bindings["cwd"]} if "cwd" in bindings else {}),
        }
    return {
        "operation": "launch_worker",
        "worker_kind": document["kind"],
        "question": bindings["question"],
        "objective": document["instructions"]
        + "\nAssignment inputs:\n"
        + json.dumps(bindings, ensure_ascii=False),
        "sources": bindings.get("sources", []),
        **({"cwd": bindings["cwd"]} if "cwd" in bindings else {}),
    }


def capture(store, agent, task, operation, path, raw, bindings, required=True):
    request = expand(tomllib.loads(raw.decode()), bindings)
    request["required"] = required
    receipt = (
        operation,
        task,
        agent,
        str(path),
        store.put(clean(raw.decode()).encode(), "application/toml"),
        store.put(bindings),
        store.put(request),
        now(),
    )
    store.db.execute("INSERT INTO preset_invocations VALUES(?,?,?,?,?,?,?,?)", receipt)
    return request
