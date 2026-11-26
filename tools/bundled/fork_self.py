NOTIFY_ON_SUCCESS = False


async def run(tool, question: str):
    """Fork your exact current decision input into a temporary investigation under your own identity. The branch sends its result to your inbox. Start independent work in this same response; its initial handle does not buy a polling turn. This cannot create another persistent Sol."""
    return await tool.request("fork_self", question=question)
