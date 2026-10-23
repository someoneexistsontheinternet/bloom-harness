from bloom import turns
from bloom.delivery import Delivery
from bloom.runtime import Runtime
from test_runtime_delivery import message, saved


def record_call(store, task, role, usage):
    call = saved(store, task, [message()])
    agent = store.one(
        "SELECT a.role FROM agents a JOIN tasks t ON t.agent_id=a.id WHERE t.id=?", (task,)
    )
    purpose = "turn" if agent["role"] == role else "tool_curation"
    store.db.execute(
        "UPDATE model_calls SET model_requested=?,input_tokens=?,cached_tokens=?,output_tokens=?,purpose=? WHERE id=?",
        (store.config.profile(role)["model"], *usage, purpose, call),
    )
    return call


def usage_embed(store, task):
    delivery = Delivery(store)
    delivery.usage_cards(store.one("SELECT * FROM tasks WHERE id=?", (task,)))
    row = store.one(
        "SELECT * FROM deliveries WHERE task_id=? AND kind='completion' ORDER BY rowid DESC",
        (task,),
    )
    return delivery.payload(row)["body"]["embeds"][0]


async def test_card_separates_actual_helper_model_and_worker_with_unknown_retry(store):
    task = store.new_task("Inspect the evidence", channel="discord", address="main")
    turn = turns.current(store, task)
    record_call(store, task, "astra", (100, 80, 10))
    # A helper's physical Luna request belongs to Astra's task/agent.
    record_call(store, task, "luna", (30, 20, 5))
    failed = record_call(store, task, "luna", (None, None, None))
    store.db.execute("UPDATE model_calls SET status='failed' WHERE id=?", (failed,))
    runtime = Runtime(store)
    worker = await runtime.request(
        "astra",
        task,
        "spawn",
        {"operation": "delegate", "role": "sol", "objective": "Inspect a source"},
    )
    record_call(store, worker["task_id"], "sol", (50, 40, 8))
    record_call(store, worker["task_id"], "luna", (20, 0, 3))
    # A subsequent owner message must not leak into this card.
    store.item("astra", {"role": "user", "content": "Separate question"}, task, origin="owner")
    following = record_call(store, task, "astra", (999, 0, 1))
    old_owner = store.one("SELECT source_item FROM owner_turns WHERE id=?", (turn,))
    store.db.execute(
        "UPDATE owner_turns SET reply_item=source_item,replied_at=created_at WHERE id!=?", (turn,)
    )
    store.db.execute("UPDATE tasks SET final_item=? WHERE id=?", (old_owner["source_item"], task))
    # The first turn remains open and therefore is the only selected owner.
    embed = usage_embed(store, task)
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert list(fields) == ["Astra", "Sol", "Luna", "Elapsed"]
    assert "**80** cached input tokens" in fields["Astra"]
    assert "**20** uncached input tokens" in fields["Astra"]
    assert "Context (last request): **100 / 1,000,000** tokens" in fields["Astra"]
    assert "**30** uncached input tokens" in fields["Luna"]
    assert "**20** cached input tokens" in fields["Luna"]
    assert "Context" not in fields["Luna"] and "Context" not in fields["Sol"]
    assert "**8** output tokens" in fields["Luna"]
    assert "**3** model calls" in fields["Luna"]
    assert "Incomplete usage: 1 calls; known totals only" in fields["Luna"]
    assert "**10** uncached input tokens" in fields["Sol"]
    assert "includes workers, helpers and retries" in embed["footer"]["text"]
    assert turns.call_turn(store, following) != turn
    before = store.one("SELECT count(*) AS n FROM deliveries")["n"]
    assert usage_embed(store, task) == embed  # Durable retry does not queue a second card.
    assert store.one("SELECT count(*) AS n FROM deliveries")["n"] == before


async def test_legacy_worker_subtotal_uses_same_model_split_and_excludes_siblings(store):
    runtime = Runtime(store)
    parent = store.new_task("Parent assignment")
    worker = await runtime.request(
        "astra",
        parent,
        "worker",
        {"operation": "delegate", "role": "sol", "objective": "Worker assignment"},
    )
    child = await runtime.request(
        worker["agent_id"],
        worker["task_id"],
        "child",
        {"operation": "delegate", "role": "luna", "objective": "Worker evidence"},
    )
    record_call(store, parent, "astra", (999, 888, 77))
    record_call(store, worker["task_id"], "sol", (40, 30, 5))
    record_call(store, child["task_id"], "luna", (10, 0, 2))
    embed = usage_embed(store, worker["task_id"])
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert list(fields) == ["Sol", "Luna", "Elapsed"]
    assert "**10** uncached input tokens" in fields["Sol"]
    assert "**30** cached input tokens" in fields["Sol"]
    assert "Context (last request): **40 / 1,000,000** tokens" in fields["Sol"]
    assert "**10** uncached input tokens" in fields["Luna"]
    assert "Worker assignment subtotal" in embed["footer"]["text"]


def test_zero_usage_and_historical_model_names_remain_explicit(store):
    empty = store.new_task("No calls", channel="discord", address="main")
    fields = usage_embed(store, empty)["fields"]
    assert [field["name"] for field in fields] == ["Usage", "Elapsed"]
    assert fields[0]["value"] == "No model calls"
    task = store.new_task("Historical model", channel="discord", address="main")
    call = record_call(store, task, "luna", (10, None, 3))
    store.db.execute(
        "UPDATE model_calls SET model_requested='historical-model' WHERE id=?", (call,)
    )
    fields = usage_embed(store, task)["fields"]
    assert fields[0]["name"] == "historical-model"
    assert "Unreported cached input tokens" in fields[0]["value"]
    assert "Unreported uncached input tokens" in fields[0]["value"]
    assert "known totals only" in fields[0]["value"]


def test_latest_context_is_not_cumulative_and_unknown_usage_is_not_uncached(store):
    task = store.new_task("Show cache misses and the current context")
    record_call(store, task, "astra", (100, 80, 10))
    record_call(store, task, "astra", (200, None, 20))
    record_call(store, task, "astra", (None, 50, 30))
    record_call(store, task, "astra", (300, 250, 40))
    # Later helper usage must not replace the answering agent's context.
    record_call(store, task, "luna", (900, 800, 90))
    fields = {f["name"]: f["value"] for f in usage_embed(store, task)["fields"]}
    assert "**380** cached input tokens" in fields["Astra"]
    assert "**70** uncached input tokens" in fields["Astra"]
    assert "**100** output tokens" in fields["Astra"]
    assert "Context (last request): **300 / 1,000,000** tokens" in fields["Astra"]
    assert "Incomplete usage: 2 calls; known totals only" in fields["Astra"]
    assert "Context" not in fields["Luna"]
    assert "Total" not in fields


def test_latest_context_missing_usage_does_not_reuse_an_older_size(store):
    task = store.new_task("Unreported last request")
    record_call(store, task, "astra", (100, 80, 10))
    record_call(store, task, "astra", (None, None, None))
    fields = {f["name"]: f["value"] for f in usage_embed(store, task)["fields"]}
    assert "Context (last request): Unreported" in fields["Astra"]
    assert "**20** uncached input tokens" in fields["Astra"]
