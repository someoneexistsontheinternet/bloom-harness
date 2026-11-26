async def run(tool, job_id: str):
    """Cancel an obsolete job owned by this execution identity. A receipt records what actually stopped; an uncertain process is never signalled blindly."""
    return await tool.request("cancel", job_id=job_id)
