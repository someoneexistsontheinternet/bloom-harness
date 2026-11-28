# Bloom v4.1 experimental

A fresh CLI implementation on `cli-v4.1-experimental`, from public main.
Filesystem inbox → recipient Terra → persistent agent → filesystem outbound.
The design and acceptance criteria are in [docs/design.md](docs/design.md).
The object map and filesystem tree are in [docs/layout.md](docs/layout.md).

The common loop, native provider adapter, durable messages and isolated tool
runner are implemented. Persistent Astra/Sol identities share the same loop;
Terra gathers context without taking over their work. Tools are ordinary Python
files in each agent's `tools/` directory, with native definitions discovered from
typed entry points. New messages and actual results wake agents; waiting and
routine send acknowledgements spend no inference.

Completed tool results return directly to the waiting owner. They do not commission
another Terra pass. A new message still receives recipient-side context, and an
unread instruction takes precedence over handling an older tool result. Results
that arrive during context preparation can join the same owner turn.

Terra receives activity facts and retrievable original call records, and batches
pending routine activity for one work scope. Required context and tool
continuations take precedence. Its working view keeps at most eight prior notes
of 512 tokens each, plus an older approved memo if present and the current
request's exact native history. That prefix stays frozen across asynchronous
tools. Full packets and responses remain in the audit history. Attachments are
references in Terra's input; their original owner-facing package is preserved.

`send_message` takes the exact final-field text, freezes selected file revisions,
waits on named existing jobs or new contextual tools, and publishes one complete
package. Delivery does not end the agent's assignment. Only continuous Astra can
create persistent Sols. Self-forks pin the exact parent request and retain the
owner's identity, with separate history and writable files.

Use `additional_content.question` when preparing evidence before final text exists.
Large additions without that purpose remain references, with an explicit sender
receipt; no empty-question reader call is bought. `additional_content.references`
retains selected original files without another extraction. Existing selected
evidence can be sent through `files` or `results`. Extraction failures accompany
the message as limitations alongside the retained original. New profiles guide
Astra toward one lead owner per coherent deliverable and early focused self-forks;
this is planning guidance, not a hard-coded specialist topology.

```sh
uv sync --group dev
.venv/bin/bloom41 --root logs/demo-first demo
```

The demo uses explicitly scripted models, real sandboxed shell/file execution and
real message delivery. It checks the returned arithmetic evidence and verifies
that idling buys no new decisions. Use a fresh root; scripted workspaces cannot
subsequently be used for live inference.

For real use, create a separate workspace and grant a deliberate call allowance:

```sh
.venv/bin/bloom41 --root workspace-runs/first init
.venv/bin/bloom41 --root workspace-runs/first budget --calls 20
.venv/bin/bloom41 --root workspace-runs/first post 'Your task here'
.venv/bin/bloom41 --root workspace-runs/first run
.venv/bin/bloom41 --root workspace-runs/first status
```

The default provider uses an existing Codex login; `--provider openai` uses
`OPENAI_API_KEY`. Credentials never enter tool environments. Installation and
initialization start no inference. The default allowance is zero and every
owner/helper request counts. All profiles retain a configured 1M capacity; the
local admission check counts text and reserves output space, not exact image cost.
Provider requests have a 180-second network inactivity timeout and a separate
900-second overall deadline. Active streaming can exceed the idle timeout. The
CLI exposes these as `--idle-timeout` and `--request-deadline`; timeouts retain the
partial stream and an uncertain terminal receipt instead of retrying a purchase.

`chat` accepts input continuously; `/urgent`, `/status` and `/quit` are available.
`run --watch` waits for new input. `inbox`, `usage`, `tools` and `reindex` inspect
the durable workspace. After inspecting a failure, `resume ACTOR --reason TEXT`
explicitly permits recovery: completed responses are reused, and failed purchases
remain recorded rather than silently retried. Budget-blocked requests that have
not started resume automatically after an allowance is added.

Files, native requests/responses and event receipts are authoritative; SQLite is
a rebuildable index. Prepared effects are checked again before execution when
new user input arrives. Accepted but unsettled effects remain uncertain after
restart. Runtime data, credentials, transcripts and private notes are excluded
from Git and packages. V3/v4 workspaces are not migrated.

On macOS, workers use an explicit filesystem sandbox plus actor-bound loopback
API credentials. Cross-agent/control-file denial is tested with real subprocesses.
Elsewhere, arbitrary scripts require explicit `--trusted-tools`; there is no
claim of OS isolation in that mode. This is tested filesystem/API isolation,
not comprehensive protection against every OS exploit.

Safari search is connected through a scoped browser service. `search.py` owns the
20-candidate whole-document/native-image review and subsequent numbered extraction
of selected text over 50k characters. Saved originals and capture failures remain
available. The separate **Bloom v4.1 Browser Helper** does not replace v3's helper.

```sh
.venv/bin/bloom41 --root YOUR_WORKSPACE browser prepare
.venv/bin/bloom41 --root YOUR_WORKSPACE browser setup
.venv/bin/bloom41 --root YOUR_WORKSPACE browser check
```

Setup opens the explicit macOS permission UI. Service requests never request a
grant automatically. `browser probe URL` captures a single page without model
inference. Source updates can require rebuilding and a new OS grant; active helper
bundles are never replaced. `scripts/check_web.py` supports retained capture replay
and an opt-in live local HTML regression.

Owners publish private L0/L1/L2 memory through exact native edits; only continuous
Astra publishes shared memory. Helpers and self-forks cannot approve those writes.
Compaction is performed by the same owning model, preserves exact owner
instructions and immutable native originals, and refreshes accepted memory only at that
boundary. L2 remains a retrieval index instead of growing the permanent prefix.
Tool results and peer reports do not count as new owner directions or become
protected bulk inputs. Instruction attachments retain their exact source references.
Branches preserve only directions within their pinned parent seed and subsequent
branch instructions. Working defaults are 20k retained tokens for Terra, 250k
after three substantive inputs for owners
(450k pressure boundary), and 500k for temporary branches. These are compaction
thresholds, not lower model capacities; all configured windows remain 1M.
An empty, oversized or non-shrinking optional compaction leaves the original view
active. It is deferred until three new owner inputs or substantial additional
pressure (32k more tokens); unchanged protected material does not trigger a loop.
An interrupted snapshot adoption reuses its completed compaction receipt even
after operator resume. Force requests remain explicit deliberate boundaries.
Terra continuations retain their request identity even when unrelated activity
arrives for the same owner.

Current verification: 76 offline tests cover executable CLI exchange,
exact fork seeds, cancellation, auth checks, sender preparation, superseding
queued effects, crash recovery and explicit failed-request recovery. Live
provider transport, native tools and sequential conversation reuse were verified
on an earlier revision, with both requested answers delivered from the same workspace. Retained real Safari
discussion/table/link captures pass replay with full text and native images
preserved. The separate helper builds and its signature verifies. After its
explicit Automation grant, live v4.1 Safari fixtures also passed: whole discussion,
tables, useful links and native screenshots. No production service or gateway
deployment is included.
The subsequent Terra changes were exercised in a fresh sequential live pair on
frozen revision `1b6de69`: FRMI answered in 46m49s / 125 requests and AVGO in
47m23s / 119 requests. Including background work, the recording has 264 terminal
model receipts and zero compactions. This required operator recovery: an empty
post-answer response blocked Astra, and 2m11s of AVGO's elapsed time belongs to
that recovery. The earlier FRMI run also included about ten minutes of operator
downtime. These runs demonstrate reduced model traffic, not a clean uninterrupted
pass or an established latency improvement. Long reader/tool queues and shell-based
polling remain. Background work was stopped after both answers; unstarted jobs
remain retained without replay.

A user-inbox answer does not terminate other agents' outstanding work. `run` may
therefore reach its allowance and exit with status 2 after delivering an answer;
inspect the inbox and run status separately. Unstarted budget-blocked work remains
available for an explicit later continuation.

Tool-result redaction takes one credential snapshot per value. Sender preparation
is durable and reused while extraction is pending; repeated scheduler ticks do not
rebuild the same result or change its frozen supporting-file revisions. Missing or
edited derived result snapshots are restored from their immutable originals.

Tool source can declare `INTERACTIVE = True` for short control or file operations.
The scheduler reserves one of its four worker slots for these jobs and admits them
first, preserving FIFO order within each class. Bundled message, read/edit, cancel,
fork and agent-creation tools use this declaration; search, extraction and shell
work share the other three slots. This is scheduling metadata, not API authority
or a latency guarantee: message preparation can still await selected evidence.
Existing frozen tool versions retain their original declarations.

Actor scheduling prioritizes direct owner input, then runnable owners, forks and
context, then routine Terra activity. All ready identities may start; each identity
has at most one active turn. A completed empty response parks without an outgoing
message. Later real input wakes it; no shell timer or polling purchase is needed.

Model/browser API waits release the executor's local tool launch budget, including
waits inside nested tools. This lets extraction and calculation launch while other
tools await remote work. Local execution still has a four-worker launch budget
with one reserved interactive place. External completions may overlap that budget;
it is not a model concurrency limit or a hard total-process cap.

Independent model API requests start without a global or per-profile concurrency
slot limit. A lock applies only to duplicate callers using the same request ID.
The allowance check and durable purchase intent occur together before awaiting
the provider, so concurrent requests still respect the total call allowance.
Request deadlines, terminal receipts and protection against uncertain replay remain.

To inspect a completed run without starting the harness:

```sh
.venv/bin/python scripts/profile_run.py \
  --workspace logs/TASK/RUN/workspace \
  --run-directory logs/TASK/RUN \
  --out artifacts/profiler/NEW_RUN \
  --details
```

The exporter checks the read-only index against every journal original and writes
an interactive operation timeline, a Chrome trace, an overlap-aware timing summary
and explicit causal links. Optional detail files retain visible requests, responses
and results while omitting native private reasoning and image bytes. These outputs
can still contain task content; they are local diagnostics, not a public export.
Original receipts and payloads are unchanged. Outputs must be outside the runtime
workspace; use a fresh destination for each source run.

Timings are elapsed receipt intervals, not sampled CPU time. Browser lease waiting
is included in the service interval because it was not separately timed. Inbox
waiting ends at the consuming turn's start. Supervisor `*-process.json` start/end
receipts distinguish runner downtime when available; their absence does not imply
an operator pause. Actor rows in the viewer are collapsible display groups and
include idle gaps. The complete Chrome trace remains available independently of
the inline view; the latter loads a pinned visualization component from its CDN.
An operator-declared recovery delay is shown separately when a journal recovery
record supplies its start and a context-resolution receipt explicitly links back
to it. Unresolved recovery records do not invent a completed duration. The timing
summary keeps recovery time distinct from model activity, including overlaps.
Jobs still prepared at the last receipt appear as unfinished queue intervals,
without a fabricated admission or completion. Per-question queue totals are
clipped to the question's answer boundary.

For the full browser viewer, use the lane-based timeline, event ledger and side
inspector adapted from the earlier Bloom explorer:

```sh
.venv/bin/python scripts/build_profiler_site.py \
  --workspace logs/TASK/RUN/workspace \
  --run-directory logs/TASK/RUN \
  --out artifacts/profiler-site/NEW_RUN
```

Serve that directory through an appropriate authenticated static origin and open
`v41.html`. Model and tool payloads load on selection. The complete receipt list,
explicit dependency links, exact model inputs/outputs and time/event bookmarks
remain available. FRMI/AVGO views end at their answers; Full run includes later
activity. Turn and service envelopes are optional, so idle time is not presented
as a solid agent-activity bar. Derived supervisor boundaries are labeled separately
from original journal receipts. Generated data remains private and outside Git.

Run `.venv/bin/python scripts/check.py` for the offline checks. CI runs them on
macOS (real sandbox) and Linux (explicit trusted-tools fixtures), then builds the
distribution. Neither CI nor the demo needs live provider credentials or browser
permission grants.

Use `--browser-root EXISTING_PROJECT` to reuse its already prepared and granted
Safari helper with a fresh `--root`. Only the native helper/lease lives there;
captured sources, agent history and receipts stay in the new runtime root.
