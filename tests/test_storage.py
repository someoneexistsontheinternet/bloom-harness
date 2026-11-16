import os
from pathlib import Path
import time

import pytest

from bloom4.agents import Agents
from bloom4.memory import Memory
from bloom4.storage import Archive, backup, restore
from bloom4.store import Store


def test_cold_search_offline_and_exact_recovery(tmp_path):
    store = Store(tmp_path / "workspace")
    cold = tmp_path / "cold"; cold.mkdir()
    source = store.put("rare original evidence")
    ev = store.append("source.saved", {"source": source, "search_source": source}, target="astra")
    os.utime(store.root / source["path"], (1, 1))
    Archive(store).configure(cold)
    assert Archive(store).sweep()["moved"] == 1
    assert store.read(source) == b"rare original evidence"
    disconnected = tmp_path / "disconnected"; cold.rename(disconnected)
    assert store.search("rare")[0]["id"] == ev["id"]
    with pytest.raises(FileNotFoundError, match="reconnect"):
        store.read(source)
    assert Archive(store).sweep()["status"] == "deferred"
    assert not cold.exists()
    with pytest.raises(FileNotFoundError): store.sync(rebuild=True)
    assert store.search("rare")[0]["id"] == ev["id"]
    disconnected.rename(cold)
    store.sync(rebuild=True)
    assert store.read(source) == b"rare original evidence"


def test_active_memory_is_pinned(tmp_path):
    store = Store(tmp_path / "workspace"); Agents(store).bootstrap()
    item = Memory(store).publish("owner", "astra", "L1", "open", "An unresolved obligation")
    os.utime(store.root / item["source"]["path"], (1, 1))
    cold = tmp_path / "cold"; cold.mkdir(); Archive(store).configure(cold)
    assert Archive(store).sweep()["moved"] == 0


def test_backup_restores_files_index_and_history_without_execution(tmp_path):
    store = Store(tmp_path / "workspace"); Agents(store).bootstrap()
    Memory(store).publish("owner", "astra", "L0", "style", "A retained preference")
    source = store.put("cold original")
    store.append("source.saved", {"source": source, "search_source": source})
    os.utime(store.root / source["path"], (1, 1))
    cold = tmp_path / "cold"; cold.mkdir(); Archive(store).configure(cold); Archive(store).sweep()
    dest = tmp_path / "backup.zip"
    result = backup(store, dest)
    assert result["files"] > 0
    cold.rename(tmp_path / "removed-volume")
    restored = restore(dest, tmp_path / "restored")
    assert restored["executed"] is False
    recovered = Store(tmp_path / "restored")
    assert recovered.read(source) == b"cold original"
    assert Memory(recovered).get("astra", "L0", "style")["text"] == "A retained preference"
    with pytest.raises(FileExistsError): restore(dest, tmp_path / "restored")
