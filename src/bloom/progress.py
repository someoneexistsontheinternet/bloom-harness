"""Public decision updates carried by native calls, never private reasoning."""

from __future__ import annotations

import copy
import json

import jsonschema

from .privacy import clean


FIELDS = ("observed", "next", "why")
SCHEMA = {
    "type": "object",
    "description": "A brief public update, not private reasoning. Describe prior evidence, the next action and its purpose. Never claim the requested tools have already run.",
    "properties": {name: {"type": "string", "minLength": 1, "maxLength": 500} for name in FIELDS},
    "required": list(FIELDS),
    "additionalProperties": False,
}


def validate(value):
    jsonschema.validate(value, SCHEMA)
    if any(not value[key].strip() for key in FIELDS):
        raise ValueError("Public progress fields must contain text")
    return value


def render(value):
    validate(value)
    return clean(
        "\n".join(
            f"**{label}:** {value[key].strip()}"
            for key, label in zip(FIELDS, ("Observed", "Next", "Why"))
        )
    )


def strict_schema(schema):
    """Keep optional execution arguments optional via explicit null on the wire."""
    result = copy.deepcopy(schema)
    if "enum" in result and "type" not in result:
        result["type"] = "integer" if all(type(x) is int for x in result["enum"]) else "string"
    if result.get("type") == "object":
        required = result.get("required", [])
        for key, value in result.get("properties", {}).items():
            value = strict_schema(value)
            if key not in required:
                value = {"anyOf": [value, {"type": "null"}]}
            result["properties"][key] = value
        result["required"] = list(result.get("properties", {}))
        result["additionalProperties"] = False
    elif result.get("type") == "array":
        result["items"] = strict_schema(result["items"])
    return result


def execution_arguments(arguments, schema):
    """Remove only optional nulls. Required nulls still fail normal validation."""
    if isinstance(arguments, dict) and schema.get("type") == "object":
        properties = schema.get("properties", {})
        return {
            key: execution_arguments(value, properties.get(key, {}))
            for key, value in arguments.items()
            if value is not None or key in schema.get("required", []) or key not in properties
        }
    if isinstance(arguments, list) and schema.get("type") == "array":
        return [execution_arguments(value, schema["items"]) for value in arguments]
    return arguments


def offered_schema(store, call, name):
    row = store.one("SELECT request_hash FROM model_calls WHERE id=?", (call,))
    if not row or not row["request_hash"]:
        return None, False
    request = store.get(row["request_hash"])
    for tool in request.get("tools", []):
        tool = tool.get("function", tool)  # Chat and Responses wire shapes.
        if tool.get("name") == name:
            return tool, True
    return None, "tools" in request


def recent(store, task, limit=6):
    """Give curators the same public updates the owner sees, with provenance."""
    rows = store.rows(
        "SELECT id,kind,text,phase,model_call_id,envelope_hash FROM items WHERE task_id=? AND (kind='function_call' OR (role='assistant' AND kind='message')) ORDER BY id DESC LIMIT 100",
        (task,),
    )
    found, seen = [], set()
    for row in rows:
        text = row["text"]
        if row["kind"] == "function_call":
            try:
                envelope = store.get(row["envelope_hash"])
                text = render(json.loads(envelope["arguments"])["progress"])
            except (KeyError, TypeError, ValueError, jsonschema.ValidationError):
                continue
        key = (row["model_call_id"], text)
        if not text or key in seen:
            continue
        seen.add(key)
        found.append(
            {
                "id": row["id"],
                "text": text,
                "phase": row["phase"],
                "source": row["kind"],
                "model_call_id": row["model_call_id"],
            }
        )
        if len(found) == limit:
            break
    return list(reversed(found))
