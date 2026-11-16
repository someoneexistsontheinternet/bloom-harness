# Teething log

This records reproducible defects and actual integration limits of the v4
experiment. Tests and mock providers are not live model acceptance.

Initial scope: CLI, local files, persistent identities, memory/history,
delegation and attention scheduling. No Discord, Safari integration, website
or production migration. Those interfaces will use the core contracts later.

## Reproduced and fixed during the first implementation

Four explicit fault-injection tests failed on the initial implementation:

| Failure | Cause | Fix / regression |
|---|---|---|
| Crash between completion and reply publication lost the reply | Task became terminal before its answer/handoff receipt existed | Publish idempotent receipts first, mark completion last; restart reuses the recorded model output without inference |
| Urgent HTTP update still allowed an obsolete write | New input was checked only after dispatching returned tools | Ingest/check urgent inputs before effects; preserve native calls with explicit not-executed results and then reconsider |
| Null usage crashed budget accounting | Missing keys were handled, present JSON nulls were not | Unknown usage keeps the conservative reservation and stays visibly unknown |
| Evidence arriving during compaction disappeared from the next context | The compaction cutoff was taken at completion, including material the summarizer never saw | Freeze exact input membership and cutoff before inference; append later evidence after the new summary |

The regressions live in `tests/test_failure_boundaries.py`. These were local
synthetic fault tests, not failures in a production deployment.

## Current practical limits

- Codex's compatibility transport has no configured server output-token cap.
  Local admission reservations and call limits are not a hard credit ceiling.
- HTTP input steering occurs at a response boundary. Native WebSocket steering
  was separately exercised successfully on the configured route.
- Commands are opt-in and not an OS sandbox. File-tool scope checks do not
  restrict arbitrary commands launched by a trusted persistent owner.
- The first live implementation used six calls: Astra delegated, Sol performed
  an environment check, wrote the file, ran the assertion, reported, then Astra
  replied. No paid polling occurred, but the model still serialized prepared
  work instead of batching every possible operation.
- A small arithmetic example does not establish long-running autonomous quality,
  memory quality after repeated compactions, or production readiness.
