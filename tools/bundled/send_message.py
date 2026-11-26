import asyncio

HANDLES_MESSAGE = True
NOTIFY_ON_SUCCESS = False


async def run(tool, recipient: str, additional_content: dict | None = None):
    """Send your final-field message with sender-prepared context. additional_content may contain files (paths), results (existing job IDs), and tools ({name,args} invocations). Requested additions settle before atomic delivery. This does not wait for a recipient reply. If your current response has no final text, preparation is retained for your next addressed final response."""
    content = additional_content or {}
    if set(content) - {"files", "results", "tools"}:
        raise ValueError("Additional content supports files, results and tools")
    for key in ("files", "results", "tools"):
        if not isinstance(content.get(key, []), list):
            raise ValueError(key + " must be a list")
    attachments = []
    body = await tool.request("message_body", recipient=recipient)
    # Freeze file revisions before awaiting other work.
    for path in content.get("files", []):
        if not isinstance(path, str):
            raise ValueError("File attachments must be paths")
        record = await tool.request("attach_file", path=path)
        attachments.append(record["id"])
        if record.get("needs_extraction"):
            extracted = await tool.invoke(
                "extract",
                {
                    "path": record["snapshot_path"],
                    "question": "Extract the evidence relevant to this outgoing message: "
                    + body["text"],
                },
            )
            item = await tool.request("attach_value", name="extracted-file.json", value=extracted)
            attachments.append(item["id"])

    async def existing(handle):
        result = await tool.wait_result(handle)
        return await tool.request("attach_value", name=handle + ".json", value=result)

    async def prepare(spec, number):
        if (
            not isinstance(spec, dict)
            or set(spec) - {"name", "args"}
            or not isinstance(spec.get("name"), str)
            or not isinstance(spec.get("args", {}), dict)
        ):
            raise ValueError("Tool additions need name and args")
        if spec["name"] == "send_message":
            raise ValueError("Message preparation cannot recursively send another message")
        try:
            value = await tool.invoke(spec["name"], spec.get("args", {}))
        except Exception as exc:
            value = {"status": "unavailable", "tool": spec["name"], "error": str(exc)}
        return await tool.request(
            "attach_value", name=f"{number:03d}-{spec['name']}.json", value=value
        )

    additions = await asyncio.gather(
        *[existing(h) for h in content.get("results", [])],
        *[prepare(s, n) for n, s in enumerate(content.get("tools", []))],
    )
    for item in additions:
        attachments.extend([item["id"], *item.get("related", [])])
        if item.get("needs_extraction"):
            extracted = await tool.invoke(
                "extract",
                {
                    "path": item["snapshot_path"],
                    "question": "Extract the evidence relevant to this outgoing message: "
                    + body["text"],
                },
            )
            selected = await tool.request(
                "attach_value", name="selected-result.json", value=extracted
            )
            attachments.append(selected["id"])
    return await tool.request("publish_message", recipient=recipient, attachments=attachments)
