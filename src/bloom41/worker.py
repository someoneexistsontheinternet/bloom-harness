"""Unprivileged subprocess entry; authentication arrives through an inherited pipe."""

import asyncio
import json
import os
import sys

from .tool import Tool, execute


async def main():
    with os.fdopen(int(sys.argv[1]), "rb") as pipe:
        spec = json.load(pipe)
    tool = Tool(spec)
    try:
        value = await execute(spec["script"], tool, spec["arguments"])
        await tool.request("finish", value=value)
    except BaseException as exc:
        try:
            await tool.request("finish", error=type(exc).__name__ + ": " + str(exc))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    asyncio.run(main())
