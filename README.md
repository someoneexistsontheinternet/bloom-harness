# Bloom v4 experimental

A fresh Python, CLI-first experiment. The `experimental/v4-cli` branch replaces
the application tree; public v3 history remains on `main`. No v3 runtime data
is imported. This is not a deployed service.

**Commit every coherent feature after testing it.** Update this README with
what works and what remains unverified. Record defects in `docs/teething.md`.
Do not hide a large rewrite behind one final commit.

```sh
uv sync --group dev
.venv/bin/bloom4 --workspace .bloom init
.venv/bin/bloom4 --workspace .bloom post 'Inspect the project'
.venv/bin/bloom4 --workspace .bloom timeline
.venv/bin/bloom4 --workspace .bloom search 'project'
```

Files are authoritative: immutable event envelopes, content-addressed originals,
versioned memory and explicit receipts. SQLite is rebuilt from those files.
Inbox files are authoring inputs, not executable scripts. See
[the contracts](docs/contracts.md) and [teething issues](docs/teething.md).

Runtime state, local credentials, logs and development checkpoints are ignored.
The experiment has no Discord or HTTP gateway. Model execution is added behind
an explicit budget; ordinary tests use synthetic providers.

Persistent identities now have explicit responsibilities and isolated work
directories. Accepted L0–L2 memory revisions are real Markdown files with
immutable approval/source records; L3 is the original event history. Frozen
contexts adopt new accepted memory only at compaction. Settled history can fork
without modifying its parent. Terra targets 10k working tokens and compacts at
20k; Astra/Sol use 100k/400k, Luna 256k/512k. All model windows remain configured
to 1M; these working policies are separate from model capacity.

```sh
.venv/bin/bloom4 agent create builder --role sol --responsibility 'Maintain the demo project'
.venv/bin/bloom4 agent list
.venv/bin/bloom4 memory show --agent astra
# Owner approval of exact Markdown; subsequent edits require --expected REVISION.
.venv/bin/bloom4 memory publish --agent astra --layer L0 --key style --file style.md
.venv/bin/bloom4 agent fork alternate --from-agent astra --responsibility 'Explore another approach'
```
