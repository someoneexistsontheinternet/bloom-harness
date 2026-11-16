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
Default efforts are Astra max, Sol xhigh, Terra medium and Luna max. Sent
contracts remain pinned; updated tools/profiles enter at compaction.

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
Each request record now contains the exact JSON wire payload, excluding auth
headers. A native steering successor reserves another call before submission;
its actual steering payload and separate reported usage are retained.

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
The frozen working packet contains L0/L1 and an L2 retrieval index, not every
historical lesson in full. Explicit memory reads can append newer accepted
revisions. Token estimates include known reasoning usage without counting
encrypted serialization length; provider usage remains authoritative.

Owners can also invoke `fork_agent` to consult a persistent branch. The seed is
the model-decision input, before its newly proposed effects, plus applied owner
updates. Multiple branches reference the same immutable history object rather
than duplicating native history in their registry files. Private memory then
diverges; inherited source grants stop at the fork. Reports join the parent;
automatic adoption of a winning branch as the main history is not implemented.

Owners can compact after 20 minutes without owner input only when their
uncompressed tail exceeds 100k. Incoming input preempts idle maintenance.
Failed optional compaction keeps the old context and is deferred for that
snapshot; uncertain compaction purchases need explicit reconciliation before
another attempt. A failed Luna curator returns an honest failure appendix and
the original reference rather than permanently blocking the owner.

SQLite also provides relational `agent_index`, `task_index`, `call_index` and
`memory_index` tables for joins and monitoring. They are projections of the
event files, rebuilt by `reindex`. A failed rebuild rolls back without destroying
the previous searchable index. Status snapshots contain short objectives and
references rather than repeated full helper inputs.

## Cold storage and backups

```sh
# Destination must already exist on its intended mounted volume.
.venv/bin/bloom4 archive --destination /path/to/existing/cold-directory
.venv/bin/bloom4 backup /path/outside/workspace/conversation.zip
.venv/bin/bloom4 restore /path/to/conversation.zip /path/to/new-workspace
```

Configured cold storage checks hourly while `run --watch`/`chat` is running.
Payloads inactive for seven days receive verified gzip copies before local
removal. Current owner contexts, accepted memory and unfinished work are pinned.
Event files and SQLite/search remain local. Text search works with a disconnected
volume; uncached source bytes need reconnection. A missing mount is never replaced
with a new SSD directory. This prototype archives workspace payloads, not arbitrary
project logs. No standalone service is installed.

Large assignment bodies, native history items and tool outputs are object
references in control-plane records. Their exact text is reconstructed for the
model, so helper payloads do not get copied into every task/status record.
Accepted memory points directly to its named Markdown revision. The approval
event records its hash and sources; modifying that version without approval is
detected. Draft a new file and publish a revision rather than editing an accepted
version in place. SQLite indexes those source files; it does not own their text.

Backups require the runtime and detached processes to be settled. The ZIP includes
registered cold originals and checks every member; restore requires a new directory,
rebuilds indexes and never starts work. V3 data is not imported or reset.
