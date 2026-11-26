"""Standalone tool discovery and the small client injected into isolated scripts."""

import ast
import asyncio
import importlib.util
import inspect
import json
from pathlib import Path
import types
import uuid

from .store import digest, identifier


def annotation(node):
    if node is None:
        return {}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return {"anyOf": [annotation(node.left), annotation(node.right)]}
    name = ast.unparse(node)
    primitive = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "None": "null",
        "dict": "object",
        "list": "array",
    }
    if name in primitive:
        return {"type": primitive[name]}
    if isinstance(node, ast.Subscript):
        base = ast.unparse(node.value)
        if base == "list":
            return {"type": "array", "items": annotation(node.slice)}
        if base == "dict":
            return {"type": "object"}
    raise ValueError("Unsupported tool annotation: " + name)


def definition(path):
    """Inspect source without importing agent-authored code in the privileged host."""
    path = Path(path)
    identifier(path.stem)
    data = path.read_bytes()
    tree = ast.parse(data, filename=str(path))
    functions = [
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run"
    ]
    if len(functions) != 1:
        raise ValueError("A tool must define exactly one run(tool, ...) entry point")
    fn = functions[0]
    if (
        not fn.args.args
        or fn.args.args[0].arg != "tool"
        or fn.args.vararg
        or fn.args.kwarg
        or fn.args.posonlyargs
        or fn.args.kwonlyargs
    ):
        raise ValueError("Use run(tool, named typed arguments), without variadic arguments")
    params = fn.args.args[1:]
    default_count = len(fn.args.defaults)
    required_count = len(params) - default_count
    if required_count < 0:
        raise ValueError("Tool() cannot have a model-supplied default")
    properties = {p.arg: annotation(p.annotation) for p in params}
    options = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                    "HANDLES_MESSAGE",
                    "NOTIFY_ON_SUCCESS",
                ):
                    value = ast.literal_eval(node.value)
                    if type(value) is not bool:
                        raise ValueError("Tool execution options must be booleans")
                    options[target.id] = value
    return {
        "name": path.stem,
        "sha256": digest(data),
        "handles_message": options.get("HANDLES_MESSAGE", False),
        "notify_on_success": options.get("NOTIFY_ON_SUCCESS", True),
        "schema": {
            "type": "function",
            "name": path.stem,
            "description": ast.get_docstring(fn) or path.stem,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [p.arg for p in params[:required_count]],
                "additionalProperties": False,
            },
        },
    }


def discover(directory):
    return [
        definition(p) for p in sorted(Path(directory).glob("*.py")) if not p.name.startswith("_")
    ]


async def execute(path, tool, args):
    name = "bloom_tool_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = module.run(tool, **args)
    return await value if inspect.isawaitable(value) else value


class Tool:
    """Agent-scoped filesystem and authenticated API; no privileged runtime reference."""

    def __init__(self, specification):
        self._spec = specification
        self._counter = 0
        self.actor = specification["actor"]
        self.scope = specification["scope"]
        self.job = specification["job"]
        self.root = Path(specification["agent_root"])
        self.workspace = self.root / "work"
        self.read_roots = [Path(p) for p in specification.get("read_roots", [])]
        self.shared = (
            Path(specification["shared_root"]) if specification.get("shared_root") else None
        )

    def path(self, value, *, write=False):
        value = Path(value)
        if value.is_absolute():
            result = value.resolve()
        elif value.parts and value.parts[0] == "shared" and self.shared:
            result = (self.shared / Path(*value.parts[1:])).resolve()
        elif value.parts and value.parts[0] in (
            "work",
            "memory",
            "tools",
            "inbox",
            "outbound",
            "history",
        ):
            result = (self.root / value).resolve()
        else:
            result = (self.workspace / value).resolve()
        if not result.is_relative_to(self.root) and not (
            not write and any(result.is_relative_to(p) for p in self.read_roots)
        ):
            raise PermissionError("File is outside this agent's workspace")
        if write and not any(result.is_relative_to(self.root / p) for p in ("work", "tools")):
            raise PermissionError("Write only work/tools; publish memory through the owning API")
        return result

    async def request(self, action, **arguments):
        reader, writer = await asyncio.open_connection(
            *self._spec["address"], limit=128 * 1024 * 1024
        )
        try:
            packet = {
                "token": self._spec["token"],
                "job": self.job,
                "action": action,
                "arguments": arguments,
            }
            writer.write(json.dumps(packet).encode() + b"\n")
            await writer.drain()
            raw = await reader.readline()
            if not raw:
                raise RuntimeError("The tool API disconnected; original job is retained")
            result = json.loads(raw)
            if "error" in result:
                raise RuntimeError(result["error"])
            return result["value"]
        finally:
            writer.close()
            await writer.wait_closed()

    async def model(self, request, *, profile="luna", sources=None):
        self._counter += 1
        return await self.request(
            "model", request=request, profile=profile, sources=sources or [], ordinal=self._counter
        )

    async def service(self, name, **arguments):
        self._counter += 1
        return await self.request("service", name=name, arguments=arguments, ordinal=self._counter)

    async def invoke(self, name, arguments):
        """Run a child tool in this same isolated execution, with its own durable receipt."""
        self._counter += 1
        child = await self.request(
            "prepare_child", name=name, arguments=arguments, ordinal=self._counter
        )
        if "result" in child:
            return child["result"]
        nested = Tool({**self._spec, "job": child["job"], "token": child["token"]})
        try:
            value = await execute(child["script"], nested, arguments)
            await nested.request("finish_child", value=value)
            return value
        except Exception as exc:
            await nested.request("finish_child", error=type(exc).__name__ + ": " + str(exc))
            raise

    async def result(self, job):
        return await self.request("result", job_id=job)

    async def wait_result(self, job):
        while True:
            result = await self.result(job)
            if result["status"] in ("completed", "failed", "cancelled", "uncertain"):
                return result
            await asyncio.sleep(0.1)
