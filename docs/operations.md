# Operations

Use the checkout's `.venv/bin/python`. Commands default to the checkout root;
pass `--root` when operating on an explicit data root. Do not point a test
checkout at an active production workspace.

## Runtime and service

```sh
.venv/bin/python -m bloom.cli status
.venv/bin/python scripts/service.py prepare
.venv/bin/python scripts/service.py status
```

Preparation requires committed source and the pinned runtime tool. It creates
an immutable release under `artifacts/releases/`. It does not start the runtime.
After checking credentials and enabled integrations in the GUI service context:

```sh
.venv/bin/python scripts/service.py start
.venv/bin/python scripts/service.py stop
.venv/bin/python scripts/service.py rollback
```

Starting can process tasks, purchase inference and send Discord replies.
The default LaunchAgent is `dev.bloom.agent`; the archive job is
`dev.bloom.agent.archive`. These labels support one installation per user. Do not run
two coordinators against one database. GUI services do not automatically inherit
terminal credentials, and this draft has no interactive provider-key installer.

Restart at a settled boundary when possible. Check the current PID/start time,
heartbeat, task/model/operation status and pending deliveries. Preserve the
database and logs before an upgrade; do not edit frozen source or a live venv.
The controller records failed launches and restores a previous release when
available. A source rollback does not roll back external effects or conversation
state.

## Inspect history

```sh
.venv/bin/python -m bloom.cli sql 'SELECT id,agent_id,status,turns FROM tasks ORDER BY created_at DESC LIMIT 20'
.venv/bin/python -m bloom.cli export-calls
```

The SQL command is read-only and bounded. `artifacts/runtime/calls/` contains
derived recent-call views: 500 main calls and 500 worker/helper calls by default.
Those limits do not delete underlying history. Immutable originals are in
`artifacts/runtime/objects/`; SQLite links requests, responses, tool results,
owner turns and delivery receipts. Exporting can retrieve cold data.

Runtime state and backups can contain private conversations and tool output.
Keep them out of issues, Git and source ZIPs.

## Cold storage and backups

Configure an existing archive volume in local TOML before enabling storage.
Keep code, venv, SQLite/search index, working context and recovery state local.

```sh
.venv/bin/python -m bloom.storage status
.venv/bin/python -m bloom.storage install
.venv/bin/python -m bloom.storage search 'keywords from an archived source'
.venv/bin/python -m bloom.storage restore 123
```

The hourly job checks seven-day inactivity and active-work pins. It copies,
fsyncs and verifies bytes, records local search/catalog receipts, then removes
the unchanged local source. A missing volume leaves local data in place.
An interrupted archive pass resumes through receipts; `more_pending` means
the bounded pass left work for another invocation.

Archived text is searchable offline through the local index. Uncached source
bytes require the archive volume. Keep the index and receipts: they are durable
data. Weekly conversation bundles include consistent SQLite and every registered
original payload; they are not backups of arbitrary project files or credentials.
Refresh the archive job from the newly prepared release after a code upgrade.

## Uncertain operations and explicit resets

An uncertain external effect must be investigated before deciding whether it
happened. Preserve its request, stream and effect receipts; do not solve an
uncertain purchase or send by repeating it. The CLI includes explicit
`resolve` and `reconcile-model` operations for owner-directed recovery.

An explicit reset removes active session/task/memory state after verifying a
complete backup. It is not a deployment step or a model tool:

```sh
.venv/bin/python -m bloom.reset plan
# Only when an intentional reset is wanted and the plan is understood:
.venv/bin/python scripts/service.py reset
```

Reset refuses unresolved work/effects and preserves original archives and
credentials. Backups and retired state live under `artifacts/session-resets/`;
local receipts remain under `workspace/`. Restore a bundle into a new isolated
directory for inspection. Do not replace a running database with an older copy.
External Discord messages are not deleted by a reset.

## MCP inbox

The optional stdio interface can be launched with an explicit workspace root:

```sh
.venv/bin/python -m bloom.mcp_server /absolute/path/to/bloom-v3
```

It reads status and queues owner requests for the existing runtime. It does not
automatically attach itself to an external client or start another coordinator.
Client selection and permissions are deployment decisions.
