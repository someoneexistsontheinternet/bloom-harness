import shutil
from pathlib import Path

import pytest
import httpx

from bloom.config import Config
from bloom.store import Store


@pytest.fixture(autouse=True)
def no_unmocked_http(monkeypatch):
    original = httpx.AsyncClient.__init__

    def blocked(request):
        raise AssertionError("Unit tests cannot make unmocked HTTP requests")

    def initialize(self, *args, **kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = httpx.MockTransport(blocked)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", initialize)


@pytest.fixture
def store(tmp_path):
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(
        source / "config", tmp_path / "config", ignore=shutil.ignore_patterns("local.toml")
    )
    shutil.copytree(source / "prompts", tmp_path / "prompts")
    (tmp_path / ".venv").symlink_to(source / ".venv", target_is_directory=True)
    # Ordinary unit fixtures use deterministic models without an autonomous
    # retrieval worker. Dedicated recall integration tests enable this explicitly.
    # Unit tests select deterministic model identities and synthetic Discord IDs;
    # distributed defaults intentionally contain no account or model selection.
    overrides = {
        "memory": {"retrieval_enabled": False, "semantic_upkeep_enabled": False},
        "profiles": {
            role: {"provider": "codex", "model": model}
            for role, model in (
                ("astra", "gpt-6-astra"),
                ("sol", "gpt-6.1-sol"),
                ("luna", "gpt-6-luna"),
            )
        },
        "discord": {"owner_id": "101", "channel_id": "102", "guild_id": "103", "bot_id": "104"},
    }
    s = Store(Config(tmp_path, overrides))
    yield s
    s.close()
