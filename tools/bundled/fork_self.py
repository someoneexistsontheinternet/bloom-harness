INTERACTIVE = True


async def run(tool, question: str):
    """Fork your exact current decision input into a temporary investigation under your own identity. Creation returns its branch handle so you can launch other forks and continue your plan; its later report also reaches your inbox. Independent forks may be called together. This cannot create another persistent Sol."""
    return await tool.request("fork_self", question=question)
