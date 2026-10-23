"""Local prompt estimates, separate from reported usage and immutable wire data.

Astra image rules: https://developers.openai.com/api/docs/guides/images-vision
Checked 2026-10-22. Unknown models/dimensions retain an explicit fallback.
"""

from __future__ import annotations

import base64
import json
import math
import struct

import tiktoken


def dimensions(url):
    if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url:
        return None
    try:
        # Headers suffice; never tokenize or duplicate an entire base64 image.
        data = base64.b64decode(url.split(",", 1)[1][:262144], validate=True)
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if data.startswith(b"RIFF") and data[8:16] == b"WEBPVP8X" and len(data) >= 30:
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(
                data[27:30], "little"
            )
        if data.startswith(b"\xff\xd8"):
            pos = 2
            while pos + 9 <= len(data):
                if data[pos] != 255:
                    pos += 1
                    continue
                marker = data[pos + 1]
                pos += 2
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    continue
                length = int.from_bytes(data[pos : pos + 2], "big")
                if marker in (
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                ):
                    height, width = struct.unpack(">HH", data[pos + 3 : pos + 7])
                    return width, height
                if length < 2:
                    break
                pos += length
    except (ValueError, struct.error):
        pass
    return None


def image_tokens(image, model, fallback=8192):
    payload = image.get("image_url")
    url = payload.get("url") if isinstance(payload, dict) else payload
    detail = (payload.get("detail") if isinstance(payload, dict) else image.get("detail")) or "auto"
    size = dimensions(url)
    if not size or not all(size) or not model.startswith("gpt-6-astra"):
        return fallback, "unknown_model_or_dimensions"
    width, height = size
    limit = 512 if detail == "low" else 65535
    scale = min(1, limit / max(width, height))
    width, height = max(1, math.floor(width * scale)), max(1, math.floor(height * scale))
    patches = math.ceil(width / 32) * math.ceil(height / 32)
    if detail == "high" and patches > 2500:
        shrink = math.sqrt(1024 * 2500 / (width * height))
        shrink *= min(
            math.floor(width * shrink / 32) / (width * shrink / 32),
            math.floor(height * shrink / 32) / (height * shrink / 32),
        )
        width, height = max(1, math.floor(width * shrink)), max(1, math.floor(height * shrink))
        patches = math.ceil(width / 32) * math.ceil(height / 32)
    return math.ceil(patches * 1.2), "astra_patch_estimate"


class Estimator:
    def __init__(self, store):
        self.store = store
        self.tokenizer = tiktoken.get_encoding("o200k_base")

    def measure(self, value, model="gpt-6-astra"):
        result = {
            "text": 0,
            "images": 0,
            "reasoning": 0,
            "unknown_images": 0,
            "unknown_reasoning": 0,
        }
        seen_reasoning = set()

        def view(item):
            if isinstance(item, dict):
                if item.get("type") in ("input_image", "image_url"):
                    amount, method = image_tokens(item, model)
                    result["images"] += amount
                    result["unknown_images"] += method.startswith("unknown")
                    return {"type": item["type"], "image": "[native image]"}
                if item.get("encrypted_content"):
                    # The provider's decoded reasoning usage is relevant; its
                    # ciphertext length is not. Count each generating call once.
                    known = self.store.one(
                        "SELECT c.id,c.reasoning_tokens,c.output_tokens FROM items i JOIN model_calls c ON c.id=i.model_call_id WHERE i.provider_id=? AND i.kind='reasoning' AND c.status='completed' LIMIT 1",
                        (item.get("id"),),
                    )
                    if known:
                        if known["id"] not in seen_reasoning:
                            amount = (
                                known["reasoning_tokens"]
                                if known["reasoning_tokens"] is not None
                                else known["output_tokens"]
                            )
                            if amount is None:
                                result["unknown_reasoning"] += 1
                                amount = self.store.config.profile("astra")["output_reserve"]
                            result["reasoning"] += amount
                            seen_reasoning.add(known["id"])
                    else:
                        result["unknown_reasoning"] += 1
                        result["reasoning"] += self.store.config.profile("astra")["output_reserve"]
                return {k: view(v) for k, v in item.items() if k != "encrypted_content"}
            if isinstance(item, list):
                return [view(v) for v in item]
            return item

        visible = view(value)
        result["text"] = len(
            self.tokenizer.encode(
                visible if isinstance(visible, str) else json.dumps(visible, ensure_ascii=False),
                disallowed_special=(),
            )
        )
        result["tokens"] = result["text"] + result["images"] + result["reasoning"]
        return result

    def request(self, agent, instructions, tools, items, model):
        estimate = self.measure([instructions, tools, items], model)
        previous = self.store.one(
            "SELECT request_hash,input_tokens FROM model_calls WHERE agent_id=? AND model_requested=? AND purpose='turn' AND status='completed' AND input_tokens IS NOT NULL ORDER BY rowid DESC LIMIT 1",
            (agent, model),
        )
        if previous:
            try:
                body = json.loads(
                    self.store.bytes(previous["request_hash"], touch=False, cache=False)
                )
            except FileNotFoundError:
                body = {}  # Calibration is optional when an old request is cold.
            prefix = body.get("input", [])
            if (
                body.get("instructions") == instructions
                and body.get("tools") == tools
                and items[: len(prefix)] == prefix
            ):
                suffix = self.measure(items[len(prefix) :], model)
                estimate.update(
                    tokens=previous["input_tokens"] + suffix["tokens"],
                    method="reported_prefix_plus_new_items",
                    reported_prefix=previous["input_tokens"],
                )
                return estimate
        estimate["method"] = "local_structured_estimate"
        return estimate
