# The v4.1 implementation contract

The common loop is filesystem inbox → recipient Terra → persistent agent →
filesystem outbound. Astra directs and reviews; Sol implements; Terra gathers
context and surfaces material deviations; Luna performs scoped evidence reading.

Each agent has tools/, inbox/, outbound/, history/, memory/ and work/. Tool scripts
expose native definitions through typed Python entry points. The runner injects
a small Tool() client with actor-bound API authority, shared model calling and
scoped filesystem/source access. The core does not contain a research or report
acceptance state machine.

Outgoing prose is the exact model final field. send_message(recipient,
additional_content) can include file revisions, existing result handles and new
tool invocations. It resolves those selected dependencies under the sender's
authority and atomically publishes one complete package. Recipient Terra may
gather its own extra context; it does not assemble evidence on the sender's behalf.
Missing requested evidence is explicit. Sending does not terminate a persistent
agent, wait for the recipient's answer, or impose report-quality acceptance gates.

The scheduler launches independent tools concurrently, records one native result
per call and parks without inference. Later results/messages can wake an agent.
An accepted long job is a receipt, not a reason to buy another decision. Native
message phases, source lineage and tool-call correlation are preserved.

Only the continuous Astra identity may create persistent Sols. A Sol may fork its
own exact context into temporary branches. Authorization derives from the
authenticated execution identity, never a caller-supplied actor. Credentials stay
outside agent-visible files. Arbitrary tool code requires actual execution
isolation; a bearer token in a shared readable directory is insufficient.

Context construction uses stable history and approved memory until deliberate
compaction. All model capacities stay configured to 1M. Terra uses smaller working
memory; owners keep richer history. L0 identity, L1 commitments and L2 lessons are
owned revisions; L3 retains exact operations. Each owner approves private memory
and compaction, while Astra approves shared memory. A temporary fork pins its
origin, memory/tool versions and subsequent independent history.

Bundled tools: read_file, edit_file, bash, find, search, extract, fork_self,
send_message, create_agent (Astra only), cancel_job. Safari search preserves the
v3 native helper, ordinary signed-in task-owned window, full parsed document and
native image capture. Luna sees useful source context and extracts exact evidence.
Sources, coverage, omissions and failures remain retrievable. Search/extraction
and sender preparation are tool workflows, not hardcoded scheduler purposes.

## Acceptance

1. Durable file messages, atomic packages, exact call/tool receipts and file-only
   recovery; no inference or effect replay after acknowledged completion.
2. Native tool discovery from the actual agent directory, isolated real Python
   execution, authority and cross-agent denial checks, cancellation and real shell
   output. Unknown effects surface as uncertain rather than being retried.
3. Sender-owned additional context: files, existing completed/running results and
   new tool calls; idempotent whole-package publication and failure reporting.
4. Shared persistent loop, Terra context selection, Astra/Sol exchange, same-owner
   forks, final-field messages and zero model purchases during unchanged waits.
5. Provider adapter, exact multimodal requests, bounded admission and honest usage;
   scripted acceptance is distinguished from authorized real-model verification.
6. Safari tool integration and retained document/image replay. GUI permission or
   live verification limitations are explicit; no HTTP substitute.
7. Focused tests, packaged CLI demonstration, privacy checks and public branch
   commits as features are verified. No production deployment.
