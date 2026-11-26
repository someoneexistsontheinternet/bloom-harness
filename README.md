# Bloom v4.1 experimental

A fresh CLI implementation on `cli-v4.1-experimental`, based on the public
repository's main branch. The core routes filesystem messages, prepares context,
calls models and runs asynchronous standalone Python tools.

Development is incremental: commit each coherent feature after focused checks.
The architecture and acceptance criteria are in [docs/design.md](docs/design.md).
The durable foundation is implemented: immutable file events, rebuildable SQLite,
agent/branch identities, exact text/attachment packages and idempotent delivery.
Three focused tests cover file-only recovery, interrupted delivery and identity
authority. The CLI loop and tool runner are still under development. No production
service or live inference is started by installation or tests.

The Safari capture source is retained separately under `lib/bloom_safari` for
adaptation. It is not yet connected to the new tool runner. Its native helper
must have matching source and explicit macOS permissions before live capture.

```sh
uv sync --group dev
```

Files and receipts are authoritative; SQLite is only a disposable search index.
Runtime workspaces, credentials, notes, logs and evidence are excluded from Git
and distribution artifacts. The separate v3 and v4 runtimes are not migrated.
