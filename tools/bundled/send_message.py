import asyncio

HANDLES_MESSAGE = True
NOTIFY_ON_SUCCESS = False
INTERACTIVE = True


async def run(tool, recipient: str, additional_content: dict | None = None):
    """Send your exact final-field message. additional_content supports files (paths), references (paths to retain without reading), results (existing job IDs), tools ({name,args}), and question (the evidence purpose). Plain files/results are copied without model calls. To read evidence, explicitly include extract in tools with its actual question. Published source IDs cited in final text are forwarded automatically. Preparation without final text is retained for your next addressed final response. Delivery does not wait for a reply."""
    content = additional_content or {}
    if set(content) - {"files", "references", "results", "tools", "question"}:
        raise ValueError(
            "Additional content supports files, references, results, tools and question"
        )
    for key in ("files", "references", "results", "tools"):
        if not isinstance(content.get(key, []), list):
            raise ValueError(key + " must be a list")
    if not isinstance(content.get("question", ""), str):
        raise ValueError("The evidence question must be text")
    attachments = []
    # Freeze file revisions before awaiting other work.
    for path in content.get("files", []):
        if not isinstance(path, str):
            raise ValueError("File attachments must be paths")
        record = await tool.request("attach_file", path=path)
        attachments.extend([record["id"], *record.get("related", [])])
    for path in content.get("references", []):
        if not isinstance(path, str):
            raise ValueError("Reference attachments must be paths")
        record = await tool.request("attach_file", path=path, reference=True)
        attachments.extend([record["id"], *record.get("related", [])])

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
    return await tool.request(
        "publish_message", recipient=recipient, attachments=list(dict.fromkeys(attachments))
    )
