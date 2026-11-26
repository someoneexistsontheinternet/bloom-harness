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
tools are still under development. The shared HTTP/SSE provider adapter and context
layer now retain exact requests, native phases/images, stable history and fork
seeds. Model admission is zero until an explicit allowance is granted. Twelve
offline tests pass, including concurrent receipt reuse and uncertain-stream
retention; live provider compatibility has not been rechecked for v4.1. No
production service or live inference is started by installation or tests.

The broker binds only loopback and derives identity from execution credentials,
never tool arguments. Arbitrary tools are refused without the macOS sandbox unless
the operator explicitly selects trusted-tools mode. This is tested filesystem/API
isolation, not a claim of comprehensive containment against every OS exploit.

Sender preparation now runs in `send_message.py`: selected file revisions,
existing job results and new contextual tools settle before one atomic package is
delivered. A tool-only response can prepare context for the next final-field
message. `extract.py` reads numbered 128k-token passes and resolves selected
passages against the original bytes, preserving CRLF and Unicode. Sixteen tests
pass, including real sender preparation while an unrelated process continues.
The common interactive loop and live Safari integration are the next steps.

The Safari capture source is retained separately under `lib/bloom_safari` for
adaptation. It is not yet connected to the new tool runner. Its native helper
must have matching source and explicit macOS permissions before live capture.

```sh
uv sync --group dev
```

Files and receipts are authoritative; SQLite is only a disposable search index.
Runtime workspaces, credentials, notes, logs and evidence are excluded from Git
and distribution artifacts. The separate v3 and v4 runtimes are not migrated.
