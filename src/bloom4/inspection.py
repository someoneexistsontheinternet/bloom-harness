"""Human inspection views over original call files; these views never drive execution."""

import json
from pathlib import Path

from .store import atomic, encoded


def export_call(store, event_id, destination):
    """Materialize an exact wire request, available response and honest receipt in a new folder."""
    event = store.get(event_id)
    if event["kind"] == "model.completed": event = store.get(event["data"]["call"])
    if event["kind"] != "model.started": raise ValueError("Select a model.started or model.completed event")
    destination = Path(destination).expanduser().resolve()
    if destination.exists(): raise FileExistsError("Use a new export directory")
    request = store.read(event["data"]["request"])
    complete = next((e for e in store.events("model.completed") if e["data"]["call"] == event["id"]), None)
    response = store.read(complete["data"]["response"]) if complete else None
    destination.mkdir(parents=True, mode=0o700)
    atomic(destination / "request.json", request)
    if response is not None: atomic(destination / "response.json", response)
    row = store.db.execute("SELECT * FROM call_index WHERE id=?", (event["id"],)).fetchone()
    receipt = {"call": dict(row), "request_source": event["data"]["request"],
               "response_event": complete["id"] if complete else None,
               "usage": complete["data"].get("usage") if complete else None}
    atomic(destination / "receipt.json", encoded(receipt))
    return {"directory": str(destination), "response_available": response is not None, "call": event["id"]}
