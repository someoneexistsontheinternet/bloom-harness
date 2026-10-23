import base64
import json
import pytest

from bloom.runtime import Runtime
from test_runtime_delivery import saved


async def invoke(runtime, store, task, name, arguments, identifier):
    output = [
        {
            "type": "function_call",
            "call_id": identifier,
            "name": name,
            "arguments": json.dumps(arguments),
        }
    ]
    call = saved(store, task, output)
    runtime.ingest_response(task, call, {"output": output})
    tool = store.one(
        "SELECT c.* FROM tool_calls c JOIN items i ON i.id=c.item_id WHERE i.model_call_id=?",
        (call,),
    )
    await runtime.execute(task, tool)
    return store.one("SELECT * FROM operations WHERE id=?", (tool["operation_id"],))


async def test_failed_rewrite_cannot_apply_old_control_file(store):
    runtime = Runtime(store)
    task = store.new_task("control provenance")
    await invoke(
        runtime,
        store,
        task,
        "write_file",
        {
            "path": "plan.toml",
            "text": 'operation="plan"\ntext="old plan"\n',
            "expected_sha256": "absent",
        },
        "first",
    )
    bad = await invoke(
        runtime,
        store,
        task,
        "write_file",
        {
            "path": "plan.toml",
            "text": 'operation="plan"\ntext="new plan"\n',
            "expected_sha256": "stale",
        },
        "failed-rewrite",
    )
    assert bad["status"] == "failed"
    applied = await invoke(runtime, store, task, "apply_request", {"path": "plan.toml"}, "apply")
    assert applied["status"] == "failed"
    assert store.one("SELECT plan FROM tasks WHERE id=?", (task,))["plan"] == ""


async def test_synthetic_credential_fixture_is_preserved_exactly(store):
    runtime = Runtime(store)
    task = store.new_task("synthetic fixture")
    # Generated expired, unsigned fixture; no credential literal in source.
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    token = encode({"alg": "none", "typ": "JWT"}) + "." + encode({"fixture": True, "exp": 0}) + "."
    text = f'FAKE_TOKEN = "{token}"\n'
    result = await invoke(
        runtime,
        store,
        task,
        "write_file",
        {"path": "fixture.py", "text": text, "expected_sha256": "absent"},
        "fixture",
    )
    assert result["status"] == "completed"
    assert (store.config.root / "fixture.py").read_text() == text


async def test_blocked_metadata_is_separate_from_final_prose(store):
    from test_runtime_delivery import message

    runtime = Runtime(store)
    task = store.new_task("needs owner input")
    await runtime.request(
        "astra",
        task,
        "outcome",
        {"operation": "outcome", "status": "blocked", "reason": "Need the target project"},
    )
    assert store.one("SELECT status FROM tasks")["status"] == "ready"
    output = [message("Please identify the target project.")]
    call = saved(store, task, output)
    runtime.ingest_response(task, call, {"output": output})
    await runtime.finish(task)
    assert store.one("SELECT status FROM tasks")["status"] == "blocked"
    assert store.one("SELECT body FROM deliveries")["body"] == "Please identify the target project."


async def test_unrecognized_control_fields_cannot_silently_change_scope(store):
    runtime = Runtime(store)
    task = store.new_task("contract")
    with pytest.raises(ValueError, match="Unsupported fields"):
        await runtime.request(
            "astra",
            task,
            "bad",
            {
                "operation": "delegate",
                "role": "sol",
                "objective": "Test",
                "invented_permission": "full",
            },
        )
    assert len(store.rows("SELECT * FROM agents")) == 1


@pytest.mark.parametrize("network", [None, False])
async def test_advertised_same_response_delegation_and_preparation(store, network):
    import jsonschema

    runtime = Runtime(store)
    task = store.new_task("Implement while checking deployment independently")
    schemas = {s["name"]: s for s in runtime.tools.schemas("astra")}
    progress = {
        "observed": "The implementation and inspection are independent.",
        "next": "Write and apply the assignment, then start inspection.",
        "why": "Start both workers without another coordination decision.",
    }
    path = str(store.config.root / "delegate.toml")
    text = (
        'operation="delegate"\nrole="sol"\nobjective="Inspect offline behavior in this fixture"\n'
    )
    if network is not None:
        text += "network=false\n"
    calls = [
        ("write_file", {"path": path, "text": text, "expected_sha256": "absent"}),
        ("apply_request", {"path": path, "expected_sha256": None}),
        (
            "gather_evidence",
            {
                "plan": "Independent preparation",
                "assignments": [{"question": "Inspect the fixture", "cwd": str(store.config.root)}],
            },
        ),
    ]
    output = []
    for index, (name, values) in enumerate(calls):
        args = {key: values.get(key) for key in schemas[name]["parameters"]["properties"]}
        if name == "gather_evidence":
            entry = schemas[name]["parameters"]["properties"]["assignments"]["items"]
            args["assignments"] = [
                {key: a.get(key) for key in entry["properties"]} for a in args["assignments"]
            ]
        args["progress"] = progress
        jsonschema.validate(args, schemas[name]["parameters"])
        output.append(
            {
                "type": "function_call",
                "call_id": "batched-" + str(index),
                "name": name,
                "arguments": json.dumps(args),
            }
        )
    call = saved(store, task, output)
    store.db.execute(
        "UPDATE model_calls SET request_hash=? WHERE id=?",
        (store.put({"tools": list(schemas.values())}), call),
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("Applying the write must not purchase another inference")

    runtime.models.infer = forbidden
    await runtime.step(task)
    operations = store.rows("SELECT name,status FROM operations ORDER BY rowid")
    assert operations[:2] == [
        {"name": "write_file", "status": "completed"},
        {"name": "apply_request", "status": "completed"},
    ]
    sol = store.one(
        "SELECT a.mode,a.network,t.status FROM agents a JOIN tasks t ON t.agent_id=a.id WHERE a.role='sol'"
    )
    assert sol == {"mode": "full", "network": 1 if network is None else 0, "status": "ready"}
    assert (
        store.one(
            "SELECT t.status FROM agents a JOIN tasks t ON t.agent_id=a.id WHERE a.role='luna'"
        )["status"]
        == "ready"
    )
    assert store.one("SELECT count(*) AS n FROM model_calls")["n"] == 1


def test_control_guidance_reaches_serialized_astra_request(store):
    import jsonschema

    runtime = Runtime(store)
    body = runtime.models.prepare(
        store.config.profile("astra"), "fixture", [], runtime.tools.schemas("astra")
    )
    tools = {tool["name"]: tool for tool in body["tools"]}
    for name in ("write_file", "patch_file", "apply_request", "gather_evidence"):
        assert "SAME" in tools[name]["description"] and "response" in tools[name]["description"]
    description = tools["apply_request"]["description"]
    assert (
        "network=true" in description
        and "network=false ONLY" in description
        and "sandbox-exec" in description
    )
    jsonschema.validate(None, tools["apply_request"]["parameters"]["properties"]["expected_sha256"])
