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

Native tools include Python AST outlines (docstrings/signatures/ranges plus
top-level effects), exact file ranges, revision-checked writes and detached
process receipts. Each substantive write/command records a short intent in the
same tool call. Helpers cannot write maintained files or launch processes.
Process execution is opt-in and **not an OS sandbox**; use a trusted workspace.
Large reads retain full originals and request Luna curation before exposing
their text to expensive owners. Small exact ranges remain available.

## Run an agent

```sh
.venv/bin/bloom4 init
# This explicit allowance includes owners, helpers, failures and compaction.
.venv/bin/bloom4 budget --calls 12 --output-tokens 131072
.venv/bin/bloom4 ask 'Create a persistent Sol named builder to implement a small example.'
.venv/bin/bloom4 chat --allow-processes
.venv/bin/bloom4 status
.venv/bin/bloom4 usage
```

The default provider reuses an existing Codex login without copying credentials.
Use `--provider openai` with `OPENAI_API_KEY` for the public Responses API.
An expired Codex login is an explicit blocker; the harness never writes its
credential file. `--output-reserve` is a local admission reservation. The public
API also receives it as `max_output_tokens`; the Codex compatibility adapter
does not, so it is **not a hard output or credit ceiling** there. No dollar or
credit estimate is presented as an invoice. Budget grants survive restarts.

`chat` accepts `/status`, `/usage`, `/urgent TEXT` and `/quit`. Input can also
arrive through another CLI's `post` or a complete JSON/TOML inbox file while
`run --watch` is active. HTTP steering is delivered at the next completed
response boundary; `--transport websocket` enables native steering where the
endpoint supports it. Public API support does not prove Codex-route support.

Required Sol assignments and processes park the parent automatically. Luna
retrieval is supplementary: Terra can stage a reference appendix or request a
material follow-up. Empty history skips automatic recall. No model calls are
bought for unchanged polling. `--no-auto-recall` disables the optional initial
lookup. Tool-produced text over 50k is retained and curated; Luna is exempt.

```sh
.venv/bin/bloom4 pause TASK_ID
.venv/bin/bloom4 resume TASK_ID
.venv/bin/bloom4 reconcile MODEL_STARTED_EVENT --reason 'Inspected this effect-free failed request'
.venv/bin/bloom4 cancel-process PROCESS_HANDLE
.venv/bin/bloom4 compact --to terra
```

Pause prevents new effects; already running commands need explicit cancellation.
Reconciliation records abandonment of a model purchase, not fabricated success.
Compaction uses the owning model and preserves originals and exact active L1.
