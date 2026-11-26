from pathlib import Path
import shutil

import pytest

from bloom41.agents import Actor, Agents
from bloom41.messages import Messages, addressed
from bloom41.store import Store


def test_file_journal_rebuild_and_idempotent_atomic_message(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    agents.create("markets", "sol", "Research public companies", principal=Actor("astra"))
    messages = Messages(store)
    source = store.put("Exact supporting passage\r\n", "text/plain")
    text = "---\ntarget: markets\n---\nPlease inspect this.\n"
    first = messages.deliver(
        "astra",
        "markets",
        text,
        message_id="m1",
        attachments=[{"name": "source.txt", "source": source}],
    )
    assert first == messages.deliver(
        "astra",
        "markets",
        text,
        message_id="m1",
        attachments=[{"name": "source.txt", "source": source}],
    )
    assert len(messages.pending("markets")) == 1
    package = messages.pending("markets")[0]
    assert store.read(package["body"]).decode() == text
    assert (
        agents.path("markets") / "inbox/m1/attachments/000-source.txt"
    ).read_bytes() == b"Exact supporting passage\r\n"
    messages.consume([package], "turn1")
    events = store.events()
    store.close()
    for path in tmp_path.glob("index.sqlite3*"):
        path.unlink()
    reopened = Store(tmp_path)
    assert [(e["kind"], e["data"]) for e in reopened.events()] == [
        (e["kind"], e["data"]) for e in events
    ]
    assert not Messages(reopened).pending("markets")
    reopened.close()


def test_delivery_recovers_after_package_rename_before_receipt(tmp_path, monkeypatch):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    messages = Messages(store)
    original = store.event

    def crash(kind, *args, **kwargs):
        if kind == "message.delivered":
            raise RuntimeError("simulated process exit")
        return original(kind, *args, **kwargs)

    monkeypatch.setattr(store, "event", crash)
    with pytest.raises(RuntimeError):
        messages.deliver("user", "astra", "original", message_id="m1")
    monkeypatch.setattr(store, "event", original)
    messages.recover()
    assert len(messages.pending("astra")) == 1
    assert len(store.events("message.delivered")) == 1
    store.close()


def test_identity_authority_and_branch_ownership(tmp_path):
    store = Store(tmp_path)
    agents = Agents(store)
    agents.bootstrap()
    agents.create("markets", "sol", "Market research", principal=Actor("astra"))
    with pytest.raises(PermissionError):
        agents.create("other", "sol", "Other work", principal=Actor("markets"))
    branch = agents.fork(Actor("astra"), "b1", {"input": []}, "Inspect an alternative")
    with pytest.raises(PermissionError):
        agents.create("other", "sol", "Other work", principal=branch)
    with pytest.raises(PermissionError):
        agents.get(Actor("markets", "b1"))
    assert agents.get(branch)["id"] == "astra"
    assert addressed("---\ntarget: astra\n---\nhello")[0] == "astra"
    store.close()
