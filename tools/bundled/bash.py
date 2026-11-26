import asyncio


async def run(tool, command: str, timeout: int = 60):
    """Run bash in your work directory. Independent commands can be separate calls. Waits happen locally; stdout, stderr and exit status are retained. Do not poll jobs with model turns."""
    if timeout < 1 or timeout > 86400:
        raise ValueError("Choose a timeout between 1 and 86400 seconds")
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-c",
        command,
        cwd=tool.workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        raise
    directory = tool.workspace / "command-output" / tool.job
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stdout.txt").write_bytes(stdout)
    (directory / "stderr.txt").write_bytes(stderr)
    text = stdout.decode(errors="replace") + "\n" + stderr.decode(errors="replace")
    result = {
        "exit_code": proc.returncode,
        "stdout_path": str(directory / "stdout.txt"),
        "stderr_path": str(directory / "stderr.txt"),
    }
    if len(text) > 50_000:
        result["evidence"] = await tool.invoke(
            "extract",
            {
                "path": str(directory / "stdout.txt"),
                "question": "Summarize this command output, retaining exact evidence for errors and the requested result.",
            },
        )
    else:
        result.update(
            stdout=stdout.decode(errors="replace"), stderr=stderr.decode(errors="replace")
        )
    return result
