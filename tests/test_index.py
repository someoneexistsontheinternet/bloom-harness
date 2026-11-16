from bloom4.agents import Agents
from bloom4.budget import Budget
from bloom4.context import Context
from bloom4.memory import Memory
from bloom4.runtime import Runtime
from bloom4.store import Store, encoded


def test_relational_projection_rebuilds_from_files(tmp_path):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    Budget(store).grant(3)
    runtime = Runtime(store, None)
    task = runtime.open("astra", "Indexed work")
    memory = Memory(store).publish("owner", "astra", "L1", "work", "Complete the indexed work")
    call = Budget(store).reserve("astra", task, Context(store).request("astra"))
    response = {"output": [], "model": "reported-model"}
    store.append(
        "model.completed",
        {
            "call": call["id"],
            "response": store.put(encoded(response)),
            "model_reported": "reported-model",
            "usage": {
                "input_tokens": 20,
                "input_tokens_details": {"cached_tokens": 10},
                "output_tokens": 5,
            },
        },
        target="astra",
        task=task,
    )
    store.append("task.completed", {}, target="astra", task=task)
    before = [
        tuple(r)
        for r in store.db.execute(
            "SELECT t.status,c.model_reported,c.input_tokens FROM task_index t JOIN call_index c ON c.task=t.id"
        )
    ]
    store.sync(rebuild=True)
    after = [
        tuple(r)
        for r in store.db.execute(
            "SELECT t.status,c.model_reported,c.input_tokens FROM task_index t JOIN call_index c ON c.task=t.id"
        )
    ]
    assert before == after == [("completed", "reported-model", 20)]
    assert (
        store.db.execute("SELECT source_sha FROM memory_index").fetchone()[0]
        == memory["source"]["sha256"]
    )


def test_failed_rebuild_leaves_previous_index_intact(tmp_path):
    store = Store(tmp_path)
    Agents(store).bootstrap()
    first = store.append("source.saved", {"search_source": store.put("needle")})
    ref = first["data"]["search_source"]
    (tmp_path / ref["path"]).unlink()
    try:
        store.sync(rebuild=True)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("Missing original should block rebuilding search")
    assert store.search("needle")[0]["id"] == first["id"]
    assert store.db.execute("SELECT count(*) FROM agent_index").fetchone()[0] == 2
