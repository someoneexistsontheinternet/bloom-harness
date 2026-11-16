import json

import pytest

from bloom4.agents import Agents
from bloom4.context import Context
from bloom4.memory import Memory
from bloom4.store import Store
from bloom4.store import encoded


def setup(tmp_path):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    return store, Memory(store), Context(store)


def test_memory_approval_revision_and_index_rebuild(tmp_path):
    store, memory, context = setup(tmp_path)
    first = memory.publish("owner", "astra", "L1", "work", "Finish this obligation")
    assert (tmp_path / first["file"]).read_text() == "Finish this obligation"
    with pytest.raises(ValueError, match="revision conflict"):
        memory.publish("owner", "astra", "L1", "work", "lost", expected=0)
    Agents(store).create("reader", "luna", "Retrieve evidence")
    with pytest.raises(PermissionError):
        memory.publish("reader", "reader", "L0", "self", "a belief")
    store.sync(rebuild=True)
    assert memory.get("astra", "L1", "work") == first


def test_memory_freezes_until_compaction_and_preserves_l1(tmp_path):
    store, memory, context = setup(tmp_path)
    first = context.request("astra")
    memory.publish("owner", "astra", "L1", "work", "Do not forget the migration")
    assert context.request("astra") == first
    context.append("astra", [{"role": "user", "content": "new input"}])
    assert context.request("astra")["input"][:-1] == first["input"]
    context.compact("astra", "A short summary that omitted the obligation", actor="owner")
    assert "Do not forget the migration" in json.dumps(context.request("astra"))
    assert len(store.events("timeline.entry")) == 1


def test_fork_keeps_prefix_then_private_revisions_diverge(tmp_path):
    store, memory, context = setup(tmp_path)
    memory.publish("owner", "astra", "L0", "style", "Be concise")
    context.append("astra", [{"role": "user", "content": "history"}])
    before = context.request("astra")
    context.fork("astra", "alternate", "Explore another approach")
    assert context.request("alternate")["input"][:-1] == before["input"]
    memory.publish("owner", "alternate", "L0", "style", "Be detailed", expected=1)
    assert memory.get("astra", "L0", "style")["text"] == "Be concise"
    assert memory.get("alternate", "L0", "style")["text"] == "Be detailed"
    assert context.request("astra") == before


def test_partial_native_tools_cannot_be_compacted_or_forked(tmp_path):
    store, memory, context = setup(tmp_path)
    context.append(
        "astra",
        [{"type": "function_call", "call_id": "open", "name": "read_file", "arguments": "{}"}],
    )
    with pytest.raises(ValueError, match="Settle"):
        context.compact("astra", "summary", actor="owner")
    with pytest.raises(ValueError, match="settled"):
        context.fork("astra", "branch", "Read")


def test_model_approval_binds_exact_memory_content(tmp_path):
    store, memory, context = setup(tmp_path)
    proposal = {
        "owner": "astra",
        "layer": "L0",
        "key": "style",
        "text": "Concise",
        "expected": 0,
        "sources": [],
    }
    response = {
        "output": [
            {
                "type": "function_call",
                "name": "memory_publish",
                "call_id": "memory1",
                "arguments": json.dumps(proposal),
            }
        ]
    }
    approval = store.append(
        "model.completed", {"response": store.put(encoded(response))}, target="astra"
    )
    with pytest.raises(ValueError, match="exact"):
        memory.publish("astra", "astra", "L0", "style", "Fabricated", approval=approval["id"])
    assert (
        memory.publish("astra", "astra", "L0", "style", "Concise", approval=approval["id"])["text"]
        == "Concise"
    )


def test_l2_history_does_not_become_permanent_prompt_ballast(tmp_path):
    store, memory, context = setup(tmp_path)
    memory.publish("owner", "astra", "L2", "old", "HISTORICAL_MARKER " * 1000)
    request = context.request("astra")
    assert "HISTORICAL_MARKER" not in json.dumps(request)
    assert memory.search("astra", "HISTORICAL_MARKER")
    assert memory.get("astra", "L2", "old")["revision"] == 1


def test_known_reasoning_usage_counts_without_ciphertext_length(tmp_path):
    store, memory, context = setup(tmp_path)
    context.snapshot("astra")
    response = {"output": [{"type": "reasoning", "id": "reason", "encrypted_content": "x" * 90000}]}
    store.append(
        "model.completed",
        {
            "response": store.put(encoded(response)),
            "purpose": "turn",
            "usage": {"output_tokens_details": {"reasoning_tokens": 100}},
        },
        target="astra",
    )
    body = context.request("astra")
    from bloom4.context import estimate

    assert context.tokens("astra", body) == estimate(body) + 100


def test_forks_reference_shared_history_and_cannot_read_future_parent_memory(tmp_path):
    store, memory, context = setup(tmp_path)
    first = memory.publish("owner", "astra", "L2", "past", "Inherited historical marker")
    context.append("astra", [{"role": "user", "content": "Shared history"}])
    context.fork("astra", "branch_a", "Alternative A")
    context.fork("astra", "branch_b", "Alternative B")
    agents = Agents(store)
    assert agents.get("branch_a").seed["history"] == agents.get("branch_b").seed["history"]
    assert "items" not in agents.get("branch_a").seed
    assert memory.search("branch_a", "Inherited")[0]["id"] == first["event"]
    future = memory.publish("owner", "astra", "L2", "future", "Private future marker")
    from bloom4.tools import Tools

    assert not Tools(store).can_read_event("branch_a", store.get(future["event"]), None)
    assert not memory.search("branch_a", "future")


def test_markdown_version_is_the_verified_memory_source(tmp_path):
    store, memory, context = setup(tmp_path)
    result = memory.publish("owner", "astra", "L0", "source", "Exact approved Markdown")
    event = store.get(result["event"])
    assert "text" not in event["data"]
    assert result["source"]["path"] == result["file"]
    (tmp_path / result["file"]).write_text("Unapproved edit")
    with pytest.raises(ValueError, match="checksum"):
        memory.get("astra", "L0", "source")
