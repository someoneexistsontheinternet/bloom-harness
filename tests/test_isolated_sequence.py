import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from bloom.runtime import Runtime
from bloom.toolchain import directory
from test_runtime_delivery import message, saved


async def test_exact_sequence_keeps_memory_and_delivery_in_isolated_store(store, monkeypatch):
    source = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "isolated_sequence", source / "scripts/evaluate_isolated_sequence.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("BLOOM_SOURCE_ROOT", str(source))
    directory(store.config.root).mkdir(parents=True)
    (directory(store.config.root) / "marker").write_text("synthetic toolchain fixture")
    monkeypatch.setattr(module, "verify", lambda root: {"fixture": True})
    prompts = []
    for i, question in enumerate(
        ("First exact prompt", "Second exact prompt", "Remember this preference"), 1
    ):
        path = store.config.root / f"question-{i}.txt"
        path.write_text(question)
        prompts.append(
            {
                "sequence": i,
                "path": str(path),
                "sha256": hashlib.sha256(question.encode()).hexdigest(),
            }
        )
    manifest = store.config.root / "manifest.json"
    manifest.write_text(json.dumps({"prompts": prompts}))
    run = store.config.root / "logs/replay"
    config, intent = module.prepare(store.config.root, run, manifest)
    assert config.root != store.config.root and not config.data["discord"]["enabled"]

    class FixtureRuntime(Runtime):
        def __init__(self, inner):
            super().__init__(inner)
            count = 0

            async def infer(agent, task, instructions, items, tools, **kwargs):
                nonlocal count
                count += 1
                if task.endswith("-3") and count == 1:
                    output = [
                        {
                            "type": "function_call",
                            "name": "memory_update",
                            "call_id": "save",
                            "arguments": json.dumps(
                                {
                                    "topic": "global",
                                    "expected_revision": 0,
                                    "summary": "Fixture preference",
                                    "rationale": "Owner asked",
                                    "sources": ["owner"],
                                }
                            ),
                        }
                    ]
                else:
                    output = [message("Fixture result")]
                return saved(inner, task, output, kwargs["item_ids"]), {"output": output}

            self.models.infer = infer

    monkeypatch.setattr(module, "Runtime", FixtureRuntime)
    await module.execute(config, intent, run, timeout=10)
    result = json.loads((run / "result.json").read_text())
    assert [q["astra_calls"] for q in result["completed"]] == [1, 1, 2]
    assert result["external_deliveries"] == 0 and result["memories"][0]["revision"] == 1
    assert store.rows("SELECT * FROM tasks") == [] and store.rows("SELECT * FROM memories") == []
    await module.execute(config, intent, run, timeout=10)
    assert json.loads((run / "result.json").read_text())["completed"] == result["completed"]
    (store.config.root / "question-1.txt").write_text("Changed prompt")
    with pytest.raises(ValueError, match="prompt changed"):
        module.prepare(store.config.root, run, manifest)
