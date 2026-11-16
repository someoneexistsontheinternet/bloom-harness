# Initial experiment validation — 2026-11-16

This is a CLI experiment, not production acceptance. No Discord gateway,
website, v3 state import or service deployment was performed.

## Live checks

An isolated synthetic workspace had a durable allowance of twelve requests.
All twelve were used; the allowance was not extended. All returned input/output
usage, and no live task or process was left running.

| Check | Calls | Verified outcome |
|---|---:|---|
| Luna file retrieval | 2 | Native read_file returned the exact fixture marker |
| WebSocket steering | 2 | An urgent input was applied through a native continuation and the final reply matched it |
| Astra → persistent Sol | 6 | Sol inspected existing work, wrote add.py, ran a real assertion, reported; Astra replied |
| Terra attention | 1 | An irrelevant late note was staged without another Astra invocation |
| Owning working compaction | 1 | Astra approved a timeline memo; prior native tool calls left active context, originals remained retained |

The first three cases used frozen commit `980d53f`. Controller/compaction checks
used `2b043cc`, including the later recovery and memory-policy fixes. Later CLI,
formatting and packaging changes are covered offline, not by another paid replay.
The reproducible entry point is `scripts/evaluate_live.py`; live execution is
explicit and is never part of CI. Local original receipts are under
`logs/codex-misc/live/local-20261116_180309/`, outside Git.

| Reported model | Calls | Cached input | Uncached input | Output |
|---|---:|---:|---:|---:|
| gpt-6-luna | 4 | 0 | 2,158 | 154 |
| gpt-6-astra | 3 | 0 | 3,076 | 921 |
| gpt-6.1-sol | 4 | 0 | 6,601 | 437 |
| gpt-6.1-terra | 1 | 0 | 627 | 61 |

These are provider-reported tokens, not a credit invoice. The probes reported
zero cached tokens; they do not demonstrate cache-hit improvements. Stable
prefixes and fork prefix sharing are tested separately with exact comparisons.
Neither the 1M window nor long-running memory quality was stress-tested.

## Offline boundaries

Final local gate: **53 tests passed**, explicit changed-file compilation,
Ruff lint/format checks, source and wheel/sdist publication scans, and both
package builds succeeded. The final local run is
`logs/codex-misc/publication/local-20261116_195850_923562/` (ignored).

The suite exercises file-before-index recovery, idempotent inbox inputs,
per-recipient consumption, exact memory approval, revision conflicts, private
fork divergence and grants, immutable Markdown, stable contexts, real local
processes, helper write denial, universal large-result preservation, late
appendices, budget exhaustion, uncertain inference, publication crashes,
urgent-effect cancellation, compaction races, idle thresholds/preemption,
curator failure, relational rebuilds, disconnected cold storage, backups and
non-executing restores. The actual CLI demo/export path is also exercised.
CLI observations additionally verify Terra-before-owner routing, idempotent
review and preserving paused/blocked tasks. These final inbox additions were
verified offline; the earlier live checks did not exercise this CLI entry point.

Five initially failing fault tests and their fixes are described in
[the teething log](teething.md). No claims of production reliability or general
autonomous coding quality follow from a small arithmetic example.

## Deliberately outside this first cut

- Discord, external MCP clients, Safari/CUA/image gateways and a development site.
- Importing private v3 history or replacing the stopped v3 installation.
- Automatic adoption of a winning branch as the main conversation.
- Automatically promoting repeated tasks into a maintained reusable tool library.
  Owners can already write scripts and record their exact references in memory.
- A broad semantic memory-quality evaluation, learned stagnation detection,
  or verified account-credit accounting.
- Continuous partial-process monitoring, autonomous project scheduling and
  mandatory Terra quality review of every Sol implementation. Terminal process
  receipts and required Sol reports currently resume the waiting owner directly;
  optional retrieval and explicit observations go through Terra.
- Automatic cold archival of arbitrary repository logs; configured archival
  currently handles workspace payloads while the CLI runner is alive.
