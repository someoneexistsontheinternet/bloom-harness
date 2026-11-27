HANDLES_MESSAGE = True
INTERACTIVE = True


async def run(tool, name: str, responsibility: str, additional_content: dict | None = None):
    """Continuous Astra only: create a persistent Sol with an enduring domain responsibility. If this response has final text, send it as the initial assignment with the requested additional content. Otherwise address an assignment to the new agent in your next final response. Reuse existing specialist identities."""
    result = await tool.request("create_agent", name=name, responsibility=responsibility)
    body = await tool.request("message_body", recipient=name)
    if body["text"].strip():
        delivery = await tool.invoke(
            "send_message", {"recipient": name, "additional_content": additional_content}
        )
        return {**result, "delivery": delivery, "wake": False}
    return {
        **result,
        "instruction": "Put the initial assignment in your next final response addressed to "
        + name,
    }
