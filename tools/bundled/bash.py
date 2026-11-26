import asyncio


async def run(tool, command: str, timeout: int = 60):
    """Run bash in your work directory. Independent commands can be separate calls. Waits happen locally; stdout, stderr and exit status are retained. Do not poll jobs with model turns."""
    if timeout < 1 or timeout > 86400:
        raise ValueError("Choose a timeout between 1 and 86400 seconds")
    directory = tool.workspace / "command-output" / tool.job
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "stdout.txt").open("wb") as out, (directory / "stderr.txt").open("wb") as err:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", command, cwd=tool.workspace, stdout=out, stderr=err
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            print(
                "Command deadline expired; partial stdout/stderr retained in " + str(directory),
                flush=True,
            )
            # Stop the verified whole executor group, including shell descendants.
            await tool.request(
                "cancel",
                job_id=tool.job,
                reason="Command timeout after "
                + str(timeout)
                + " seconds. Partial output: "
                + str(directory),
            )
            raise
    stdout = (directory / "stdout.txt").read_bytes()
    stderr = (directory / "stderr.txt").read_bytes()
    text = stdout.decode(errors="replace") + "\n" + stderr.decode(errors="replace")
    result = {
        "exit_code": proc.returncode,
        "stdout_path": str(directory / "stdout.txt"),
        "stderr_path": str(directory / "stderr.txt"),
    }
    if len(text) > 50_000:
        for name, raw in (("stdout", stdout), ("stderr", stderr)):
            if len(raw.decode(errors="replace")) > 25_000:
                result[name + "_evidence"] = await tool.invoke(
                    "extract",
                    {
                        "path": str(directory / (name + ".txt")),
                        "question": "Summarize this command "
                        + name
                        + ", retaining exact evidence for errors and the requested result.",
                    },
                )
            else:
                result[name] = raw.decode(errors="replace")
    else:
        result.update(
            stdout=stdout.decode(errors="replace"), stderr=stderr.decode(errors="replace")
        )
    return result
