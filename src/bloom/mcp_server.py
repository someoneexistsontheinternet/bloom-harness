"""Small stdio MCP entry point to the same persistent Astra runtime.

This accepts requests, never starts an independent main-agent loop. Run Bloom's
service separately. It can be attached by an MCP-capable ChatGPT/Codex client.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .config import Config
from .operations import report
from .store import Store, atomic, uid


TOOLS = [
    {
        "name": "bloom_message",
        "description": "Queue a message for the one continuous Astra. Returns a task receipt; inspect status for replies.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bloom_status",
        "description": "Read current work, unresolved outcomes and recent replies.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "bloom_steer",
        "description": "Steer an existing task without discarding its completed work.",
        "inputSchema": {
            "type": "object",
            "properties": {"task": {"type": "string"}, "text": {"type": "string"}},
            "required": ["task", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bloom_pause",
        "description": "Pause new work across a task tree. Existing processes retain their handles.",
        "inputSchema": {
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"],
            "additionalProperties": False,
        },
    },
]


def dispatch(store, method, params):
    if method == "initialize":
        requested = params.get("protocolVersion")
        supported = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
        return {
            "protocolVersion": requested if requested in supported else "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "bloom", "version": "0.1.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method != "tools/call":
        raise ValueError("Unsupported method")
    name, args = params["name"], params.get("arguments", {})
    if name == "bloom_status":
        result = report(store)
        result["recent_replies"] = store.rows(
            "SELECT task_id,phase,text FROM items WHERE role='assistant' AND kind='message' ORDER BY id DESC LIMIT 10"
        )
    else:
        import jsonschema

        definition = next(t for t in TOOLS if t["name"] == name)
        jsonschema.validate(args, definition["inputSchema"])
        operation = {"bloom_message": "message", "bloom_steer": "steer", "bloom_pause": "pause"}[
            name
        ]
        # JSON quoted strings are valid basic TOML strings for these scalar inputs.
        content = "operation = " + json.dumps(operation) + "\n"
        request_id = uid("request")
        content += "request_id = " + json.dumps(request_id) + "\n"
        content += "".join(
            k + " = " + json.dumps(v, ensure_ascii=False) + "\n" for k, v in args.items()
        )
        path = store.config.workspace / "requests" / (uid("mcp") + ".toml")
        atomic(path, content.encode())
        result = {
            "queued": True,
            "request_id": request_id,
            "request_file": str(path),
            "runtime_required": True,
        }
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "isError": False,
    }


def main():
    root = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(os.environ.get("BLOOM_ROOT", str(Path(__file__).resolve().parents[2])))
    )
    store = Store(Config(root))
    try:
        for line in sys.stdin:
            request = json.loads(line)
            if "id" not in request:
                continue
            try:
                result = dispatch(store, request["method"], request.get("params", {}))
                answer = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            except Exception as exc:
                from .privacy import clean

                answer = {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32602, "message": clean(str(exc))},
                }
            print(json.dumps(answer, ensure_ascii=False), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
