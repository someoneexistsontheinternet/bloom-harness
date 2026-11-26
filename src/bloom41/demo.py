"""Scripted decisions exercise real tools and message delivery; no inference service."""

import json
import re


def message(text):
    return {
        "type": "message",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": text}],
    }


def invocation(name, arguments, ident):
    return {
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(arguments),
        "call_id": ident,
    }


def evidence(request):
    for item in request["input"]:
        for part in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if part.get("type") == "input_text":
                try:
                    yield json.loads(part["text"])
                except (ValueError, TypeError):
                    pass


class DemoProvider:
    simulated = True

    def __init__(self):
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        raw = json.dumps(request["input"], ensure_ascii=False)
        model = request["model"]
        if model.endswith("terra"):
            latest = request["input"][-1]["content"][0]["text"]
            ordinary = re.search(r"Gather only context useful to ([\w-]+)", latest)
            activity = re.search(r"Monitor the latest turn of ([\w-]+)", latest)
            target = (ordinary or activity).group(1)
            disposition = "stage" if activity else "deliver"
            output = [
                message(
                    f"---\ntarget: {target}\ndisposition: {disposition}\n---\nUse the supplied originals. Preserve the original request and check the real command output."
                )
            ]
        elif model.endswith("astra"):
            if "The checked result is 4" in raw:
                assert any(
                    isinstance(v, dict) and v.get("output", {}).get("stdout") == "4"
                    for v in evidence(request)
                ), "Astra must receive the real supporting result"
                output = [
                    message(
                        "---\ntarget: user\n---\nThe checked result is 4. The specialist's real shell calculation and its output reached this review."
                    )
                ]
            else:
                output = [
                    invocation(
                        "create_agent",
                        {
                            "name": "calculator",
                            "responsibility": "Check arithmetic with executable evidence",
                        },
                        "create",
                    ),
                    message(
                        "---\ntarget: calculator\n---\nCalculate 2+2 with a real command and send me the result with its evidence."
                    ),
                ]
        else:
            outputs = [
                i
                for i in request["input"]
                if i.get("type") == "function_call_output" and i.get("call_id") == "calculate"
            ]
            if not outputs:
                output = [invocation("bash", {"command": "printf '%s' \"$((2+2))\""}, "calculate")]
            else:
                assert any(
                    isinstance(v, dict) and v.get("stdout") == "4" for v in evidence(request)
                ), "Sol must see the actual result before reporting"
                handle = json.loads(outputs[0]["output"])["job"]
                output = [
                    invocation(
                        "send_message",
                        {"recipient": "astra", "additional_content": {"results": [handle]}},
                        "send",
                    ),
                    message(
                        "---\ntarget: astra\n---\nThe checked result is 4. The command output is attached."
                    ),
                ]
        return {"status": "completed", "model": model, "output": output}, []
