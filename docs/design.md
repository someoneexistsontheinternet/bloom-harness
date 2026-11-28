# The v4.1 implementation contract

The common loop is filesystem inbox → persistent agent → filesystem outbound,
with recipient Terra preparing supplemental context concurrently. Astra directs and reviews; Sol implements; Terra gathers
context and surfaces material deviations; Luna performs scoped evidence reading.

Terra's current request is a temporary working view over the immutable audit
history. It selects at most eight previous notes of 512 tokens each and preserves
the exact native history of the current context request. The selected prefix is
frozen while that request waits for tools. Full packets and responses remain in
history/; they are not copied into every later monitoring decision. Evidence
attachments reach Terra as retrievable references while the owner retains its
original package. Routine activity for the same work scope can be inspected
together, with operation facts, progress and exact records supplied directly.

Each agent has tools/, inbox/, outbound/, history/, memory/, work/ and scratch/. Tool scripts
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
Preparation may carry an explicit evidence question before the final-field body
exists. Without either, large additions stay references and the receipt states
that no extraction ran. Explicit reference attachments do not buy another read.
A failed extraction retains the original and adds its limitation to the package.
The exact final-field body remains the only outgoing message content.

The scheduler launches independent tools concurrently, records one native result
per call and parks without inference. Later results/messages can wake an agent.
Terminal tool results return directly to their owner without purchasing another
Terra context pass. New messages reach their owner immediately, with available
results in the same turn; optional Terra work cannot hold delivery. Routine notes
are staged and concrete additions may wake the owner later. An empty completed
response consumes its inputs and parks until real input arrives, without a timer.
An empty Terra response settles its preparation without a recipient wake.
Context preparation accepted
by an older runner retains its original inputs and completes normally on recovery.
An accepted long job is a receipt, not a reason to buy another decision. Native
message phases, source lineage and tool-call correlation are preserved.
Actor scheduling prioritizes owner instructions and runnable owners/forks ahead
of optional monitoring, with periodic oldest-work service. Model API calls have
no global or per-profile concurrency-slot limit. Duplicate callers for one request
ID are serialized for receipt reuse; independent requests start immediately after
the call-allowance check and durable purchase intent. Every ready actor may start,
with one active turn per identity. Local tool launches retain a small budget;
model/browser waits release it even inside nested tools. This is not a model slot
gate or a hard process count: external completions may overlap. An accepted request is never silently
cancelled or repurchased to obtain priority.

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

Memory lives in owner Markdown files, with immutable approved revisions. A new
fork's memory directory is a read-only view of those files; its working context
still uses the pinned seed until a deliberate boundary. Legacy branch directories
are retained and their SDK memory reads use the owner. Every branch has independent
work/ and scratch/. Use work/plan.md for a short task hierarchy, dependencies,
owners, expected returns and current results. Launch ready independent forks
together, keep useful lead work, then integrate; this is guidance, not another
runtime contract. Plans, temporary drafts and durable memory have distinct roles.
New inputs label attachments with canonical paths. Legacy inbox references resolve
only within authorized read grants, with ambiguity rejected. History appends use
the exact prepared model input, including across restart and rendering changes.

Compaction protects actual owner directions and material assignments, with
references to their attachments. Native user-role wrapping alone does not make a
tool result or peer report an instruction. Owner rebuilds count substantive
inputs, and temporary branches preserve the parent cutoff. Nonproductive optional
compaction is deferred; a paid completed memo is reused across interrupted
adoption and operator recovery. The full old context remains an immutable source.

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
4. Shared persistent loop, supplemental Terra context for new messages, direct terminal
   tool-result delivery, Astra/Sol exchange, same-owner
   forks, final-field messages and zero model purchases during unchanged waits.
5. Provider adapter, exact multimodal requests, bounded admission and honest usage;
   scripted acceptance is distinguished from authorized real-model verification.
6. Safari tool integration and retained document/image replay. GUI permission or
   live verification limitations are explicit; no HTTP substitute.
7. Focused tests, packaged CLI demonstration, privacy checks and public branch
   commits as features are verified. No production deployment.
