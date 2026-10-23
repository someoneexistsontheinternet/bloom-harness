# Worker presets

Astra explicitly launches scoped Luna work. Prefer one `gather_evidence` with
`assignments=[{"path":"config/agent-presets/inspect-files.toml","bindings":{...}}, ...]`.
It captures every template/binding before publishing any children, runs them in
parallel and returns their validated reports in one native tool result. Restart
reuses the captured batch. No separate launch/join inference is needed. Set
`background=true` only when identified independent work needs early handles.
Individual `apply_request` launches and `await_workers` remain available.
No recall worker starts merely because a new owner message arrived. Skip workers
when they add no value.

| File under `config/agent-presets/` | Required bindings | Optional bindings |
| --- | --- | --- |
| `recall.toml` | `question` | none |
| `inspect-files.toml` | `question`, `cwd` | `sources`, `criteria` |
| `compare-evidence.toml` | `question`, `cwd`, `sources` | `criteria` |
| `check-result.toml` | `question`, `cwd`, `sources`, `criteria` | none |

`sources` is a list of exact file/evidence references within the assigned scope.
The owner question and recent public tool summaries are supplied automatically
after Astra chooses the assignment. Recall can search/read visible history; file
workers can inspect their assigned directory with bounded read-only tools. Neither
decides the final answer, changes memory or receives implementation authority.

Example `apply_request` arguments in the strict Astra wire format:

```json
{
  "path": "config/agent-presets/inspect-files.toml",
  "expected_sha256": null,
  "bindings": {
    "question": "Find the project's documented archive location and verify the referenced manifest.",
    "cwd": "/path/to/bloom-v3",
    "sources": ["README.md", "docs"],
    "criteria": null
  },
  "progress": {
    "observed": "The archive location still needs verification.",
    "next": "Have Luna inspect the project references while I check independent evidence.",
    "why": "Collect source-backed facts in parallel before drawing a conclusion."
  }
}
```

Several such calls can share one response. `await_workers` with `tasks=null`
snapshots the children accepted for the current owner turn, including workers
launched earlier in that batch. Specific returned task IDs can select a subset.
Results include failures/limitations; a failed worker is not proof of absence.
Validated reports append once at a settled tool boundary, without rewriting the
frozen memory prefix or splitting native tool exchanges.

Preset calls default to `required=true`: their results are dependencies of the
current reply. Set `required=false` only for supplementary evidence. Those workers
may finish after the reply; validated reports remain durably staged and append
once before the next naturally required Astra inference. They do not wake Astra,
invalidate a finished answer, or become owner steering. An explicit `await_workers`
can still collect a supplementary worker. Existing/legacy assignments remain
required. Owner cancellation or superseding instructions retain their control.

Reviewed relative preset paths resolve against the active frozen source. They
need no preliminary read/write. Custom preset TOML uses the same version/kind/
instructions/required/optional structure and the normal exact-hash/write-receipt
rule. Bindings are JSON data after parsing, never textual TOML/shell interpolation.
Every invocation retains its template, binding and expanded-assignment artifacts
in SQLite. Restart reuses the accepted invocation even if its authoring file later
changes. Fresh assignments use fresh native operation identities.

For ordinary controls, existing `write_file` → `apply_request` batching remains
available. Full Sol delegation keeps network enabled unless the task actually
requires isolation. The native foreground `gather_evidence` batch remains available
for ad hoc inspections; it already joins its workers automatically.

Accepted persona/preferences use `memory_get(topic)` and
`memory_update(topic, expected_revision, summary, detail, rationale, sources)`.
Use topic `global` for core preferences; a missing topic has revision 0. Sources
must be observed item references, received tool/report artifacts, or `owner` for
the attributed owner message. The accepted revision and idempotent receipt commit
together; conflicts preserve the newer content. The returned content is the
readback. SQLite is authoritative, with a recoverable Markdown mirror. These
tools append receipts to the current history; they never rewrite its frozen
memory snapshot. A source-code/SQL investigation or plan TOML is unnecessary for
an ordinary accepted-memory change.

Inspectors stop when their named criteria are met or report a concrete missing
dependency. Before the exploration limit they enter a persistent reporting phase:
discovery is withheld, the 120-second report clock starts once, and at most two
calls cover synthesis and one exact-source/format repair. Restart does not renew
this clock. Expiration preserves partial receipts and unknown usage; it does not
buy an automatic retry. Explicit recall on a genuinely empty prior corpus returns
a durable `not_needed` receipt with zero Luna calls. A lexical search miss alone
never enables that shortcut.
