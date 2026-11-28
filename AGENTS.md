# Working on Bloom v4.1

Read README.md, docs/design.md and the ignored notes/current.md before substantial work.
The user-approved architecture is inbox → agent → outbound, with Terra preparing
supplemental context concurrently instead of blocking assignments. Preserve the
latest user corrections over inherited v3/v4 assumptions.

- One integrator. Do not change the separate v3/v4 checkouts, their environments,
  runtime state or active native helper. Never push private repository history.
- Commit coherent features after focused verification; update README with actual
  behavior and limits. Use this project's .venv, uv and explicit changed-file
  compilation plus runtime smoke checks. Offline tests make no paid requests.
- Files are authoritative; indexes are rebuildable. Preserve original messages,
  model requests, responses, tool versions, outputs, receipts and uncertain effects.
  No blind replay of accepted model purchases, commands or message delivery.
- Tools are standalone Python files in each agent's directory. Tool() supplies
  narrow authenticated capabilities, not Runtime. Native schemas come from tools.
- Terra gathers context and monitors; it never owns substantive work. Sol owns its
  work and the context accompanying its outgoing messages. Only continuous Astra
  creates persistent Sol identities. Self-forks retain their owner's identity.
- Keep Terra's working notes small and separate from full audit history. Supply
  activity facts directly, batch routine monitoring, and expose bulk evidence to
  Terra by reference. Preserve native tool continuations within each request.
- Capture outgoing message content from the model's final field. send_message
  prepares sender-selected files, existing outputs and new tool results before
  atomic delivery. Delivery is independent of assignment completion.
- Wait locally. Wake only for material input/results or explicitly continuing
  independent work. Do not buy model calls to poll jobs or announce receipts.
- A completed empty response parks cleanly; never manufacture wake-ups with shell
  sleeps. Each identity has one active turn, with no global actor cap. Remote
  model/browser waits release the local tool launch budget, including nested calls.
- Guide substantial work with work/plan.md: task hierarchy, dependencies, owners,
  expected returns and results. Launch ready independent forks together and keep
  integration with the lead. Plans are files, not submission contracts. Each fork
  has private work/scratch and read-only access to its owner's filesystem memory.
- Model API calls have no harness concurrency-slot limit. Preserve per-request
  deduplication, durable purchase intents and the explicit total call allowance.
- Return terminal tool results directly to their owner; do not buy a Terra pass
  for each result. New instructions must not be skipped while processing results.
- Keep native histories and 1M configured capacity. Persistent owners approve
  their private memory/compaction; Astra approves shared memory. Original history
  remains available through retrieval and forks.
- Safari search uses the preserved ordinary-session native capture implementation,
  whole document text, native images and explicit coverage. Never replace it with
  generic HTTP search as a hidden shortcut. No service-generated permission prompt.
- Keep credentials, runtime data, transcripts and personal notes out of Git and
  packages. Do not expose tokens in output or logs. Tool isolation and API identity
  checks must be tested; directory names alone are not a security boundary.
- Keep supporting runs under logs/codex-misc and development checkpoints under
  notes. Live inference is explicitly budgeted; no gateway/service deployment is
  included in this CLI implementation.
