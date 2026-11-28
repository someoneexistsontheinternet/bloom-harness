"""Unprivileged subprocess entry; authentication arrives through an inherited pipe."""

import asyncio
import json
import os
import signal
import sys

from .tool import Tool, execute


async def main():
    with os.fdopen(int(sys.argv[1]), "rb") as pipe:
        spec = json.load(pipe)
    tool = Tool(spec)
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        value = await execute(spec["script"], tool, spec["arguments"])
        await tool.request("finish", value=value)
    except asyncio.CancelledError:
        try:
            await tool.request("finish", cancelled=True)
        except Exception:
            pass
        raise
    except BaseException as exc:
        try:
            await tool.request("finish", error=type(exc).__name__ + ": " + str(exc))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    asyncio.run(main())
