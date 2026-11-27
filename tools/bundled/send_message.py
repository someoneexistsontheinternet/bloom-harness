import asyncio

HANDLES_MESSAGE = True
NOTIFY_ON_SUCCESS = False
INTERACTIVE = True


async def run(tool, recipient: str, additional_content: dict | None = None):
    """Send your exact final-field message. additional_content supports files (paths), references (paths to retain without reading), results (existing job IDs), tools ({name,args}), and question (the evidence purpose). Reuse prepared evidence. Large additions are extracted only with a question or existing final text; otherwise they remain references. Preparation without final text is retained for your next addressed final response. Delivery does not wait for a reply."""
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
    reference_only = []
    body = await tool.request("message_body", recipient=recipient)
    question = content.get("question", "").strip() or body["text"].strip()

    async def select(record, name):
        if not record.get("needs_extraction"):
            return
        if not question:
            reference_only.append(record["snapshot_path"])
            return
        try:
            value = await tool.invoke(
                "extract",
                {
                    "path": record["snapshot_path"],
                    "question": "Extract the evidence relevant to this outgoing message or explicit purpose:\n"
                    + question,
                },
            )
        except Exception as exc:
            value = {
                "status": "unavailable",
                "error": str(exc),
                "source_path": record["snapshot_path"],
                "notice": "Extraction failed. The exact original remains attached by reference.",
            }
        selected = await tool.request("attach_value", name=name, value=value)
        attachments.append(selected["id"])

    # Freeze file revisions before awaiting other work.
    files = []
    for path in content.get("files", []):
        if not isinstance(path, str):
            raise ValueError("File attachments must be paths")
        record = await tool.request("attach_file", path=path)
        attachments.append(record["id"])
        files.append(record)
    for path in content.get("references", []):
        if not isinstance(path, str):
            raise ValueError("Reference attachments must be paths")
        record = await tool.request("attach_file", path=path, reference=True)
        attachments.append(record["id"])
    for record in files:
        await select(record, "extracted-file.json")

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
        await select(item, "selected-result.json")
    result = await tool.request("publish_message", recipient=recipient, attachments=attachments)
    if reference_only:
        result = {
            **result,
            "reference_only": reference_only,
            "preparation_note": "No extraction was purchased without an evidence purpose. Supply additional_content.question or attach existing selected evidence if passages are needed.",
        }
    return result
