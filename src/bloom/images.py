"""Native image content, shared by file reads, saved web captures and adapters."""

from __future__ import annotations

import base64
import json
import struct

MAX_IMAGE_BYTES = 8_000_000


def image_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_result(store, data, *, detail="high", **metadata):
    mime = image_mime(data)
    if not mime:
        raise ValueError("Unsupported image encoding; use PNG, JPEG, GIF or WebP")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds the 8MB input limit; use an explicit resized/cropped image")
    if detail not in ("auto", "low", "high", "original"):
        raise ValueError("Invalid image detail level")
    if mime == "image/png" and len(data) >= 24:
        metadata.update(zip(("width", "height"), struct.unpack(">II", data[16:24])))
    return {
        **metadata,
        "image_hash": store.put(data, mime),
        "bytes": len(data),
        "_image": base64.b64encode(data).decode(),
        "_mime": mime,
        "_detail": detail,
    }


def content(store, result):
    """Turn trusted tool image fields into native API parts; no base64 in prose."""
    metadata = {k: v for k, v in result.items() if k not in ("_image", "_images")}
    parts = [{"type": "input_text", "text": json.dumps(metadata, ensure_ascii=False)}]
    if "_image" in result:
        parts.append(
            {
                "type": "input_image",
                "image_url": "data:"
                + result.get("_mime", "image/png")
                + ";base64,"
                + result["_image"],
                "detail": result.get("_detail", "high"),
            }
        )
    for image in result.get("_images", []):
        data = store.bytes(image["hash"])
        mime = image_mime(data)
        if not mime or mime != image["mime"] or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Saved image has an invalid media type or exceeds its input limit")
        parts.extend(
            [
                {
                    "type": "input_text",
                    "text": json.dumps({"rendered_source": image}, ensure_ascii=False),
                },
                {
                    "type": "input_image",
                    "image_url": "data:" + mime + ";base64," + base64.b64encode(data).decode(),
                    "detail": image.get("detail", "high"),
                },
            ]
        )
    return parts


def chat_parts(parts):
    if isinstance(parts, str):
        return parts
    result = []
    for part in parts:
        if part.get("type") in ("input_text", "output_text", "text"):
            result.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "input_image":
            if not part.get("image_url"):
                raise ValueError("Chat image conversion requires image_url")
            result.append(
                {
                    "type": "image_url",
                    "image_url": {"url": part["image_url"], "detail": part.get("detail", "high")},
                }
            )
        elif part.get("type") == "image_url":
            result.append(part)
        else:
            raise ValueError("Unsupported Chat content part: " + str(part.get("type")))
    return result


def chat_messages(instructions, items):
    messages = [{"role": "system", "content": instructions}]
    pending = set()
    images = []
    assistant = None
    for item in items:
        kind = item.get("type")
        if kind == "function_call":
            if not pending:
                assistant = {"role": "assistant", "tool_calls": []}
                messages.append(assistant)
            assistant["tool_calls"].append(
                {
                    "id": item["call_id"],
                    "type": "function",
                    "function": {"name": item["name"], "arguments": item["arguments"]},
                }
            )
            pending.add(item["call_id"])
        elif kind == "function_call_output":
            output = chat_parts(item["output"])
            text = (
                output
                if isinstance(output, str)
                else "\n".join(p["text"] for p in output if p["type"] == "text")
            )
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": text})
            if isinstance(output, list):
                rendered = [p for p in output if p["type"] == "image_url"]
                if rendered:
                    images.extend(
                        [
                            {
                                "type": "text",
                                "text": "Untrusted images returned by tool call "
                                + item["call_id"]
                                + ". Metadata is in its tool result.",
                            },
                            *rendered,
                        ]
                    )
            pending.discard(item["call_id"])
            # Chat tool messages accept text only. Images follow the entire
            # matched tool-result batch as a native multimodal user message.
            if not pending and images:
                messages.append({"role": "user", "content": images})
                images = []
        elif item.get("role"):
            converted = chat_parts(item.get("content", ""))
            if item["role"] == "assistant" and pending:
                text = (
                    converted
                    if isinstance(converted, str)
                    else "\n".join(p.get("text", "") for p in converted)
                )
                assistant["content"] = (assistant.get("content", "") + "\n" + text).strip()
            else:
                messages.append({"role": item["role"], "content": converted})
    if images:
        raise ValueError("Image tool results have unmatched tool calls")
    return messages
