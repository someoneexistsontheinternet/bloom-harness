# Bloom v4.1 experimental

A fresh CLI implementation on `cli-v4.1-experimental`, based on the public
repository's main branch. The core routes filesystem messages, prepares context,
calls models and runs asynchronous standalone Python tools.

Development is incremental: commit each coherent feature after focused checks.
The architecture and acceptance criteria are in [docs/design.md](docs/design.md).
The durable foundation is implemented: immutable file events, rebuildable SQLite,
agent/branch identities, exact text/attachment packages and idempotent delivery.
The standalone tool runner is implemented: native definitions are discovered
without importing tool code in the host, each execution has an authenticated
capability, and macOS workers run under an explicit filesystem sandbox. File read,
revision-checked edit, local find, bash and cancellation are available. Nested tool
calls retain separate receipts. Eight tests cover recovery, real execution,
cancellation and cross-agent/control-file denial. The CLI loop and model/search
tools are still under development. No production service or live inference is
started by installation or tests.

The broker binds only loopback and derives identity from execution credentials,
never tool arguments. Arbitrary tools are refused without the macOS sandbox unless
the operator explicitly selects trusted-tools mode. This is tested filesystem/API
isolation, not a claim of comprehensive containment against every OS exploit.

The Safari capture source is retained separately under `lib/bloom_safari` for
adaptation. It is not yet connected to the new tool runner. Its native helper
must have matching source and explicit macOS permissions before live capture.

```sh
uv sync --group dev
```

Files and receipts are authoritative; SQLite is only a disposable search index.
Runtime workspaces, credentials, notes, logs and evidence are excluded from Git
and distribution artifacts. The separate v3 and v4 runtimes are not migrated.
