from pathlib import Path
import tomllib

import pytest

from bloom.models import Models, ProviderError


def test_distributed_defaults_do_not_select_an_account_or_purchase_models():
    root = Path(__file__).resolve().parents[1]
    defaults = tomllib.loads((root / "config/default.toml").read_text())
    assert not defaults["discord"]["enabled"] and not defaults["storage"]["enabled"]
    assert all(
        defaults["discord"][key] == "" for key in ("owner_id", "guild_id", "channel_id", "bot_id")
    )
    for profile in defaults["profiles"].values():
        assert profile["provider"] == "openai"
        assert profile["model"].startswith("configure-")
        assert profile["context_limit"] == 1_000_000


@pytest.mark.parametrize("model", ["", "configure-astra-model", None])
async def test_unconfigured_model_fails_before_any_request_or_receipt(store, model):
    task = store.new_task("Do not purchase an unconfigured model")
    with pytest.raises(ProviderError, match="config/local.toml"):
        await Models(store).infer("astra", task, "test", [], [], profile_override={"model": model})
    assert not store.rows("SELECT id FROM model_calls")
