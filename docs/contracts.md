# Minimal contracts

1. An Event is immutable, sequenced, checksummed and stored as a file. Its
   actor, target, task, causal parent and timestamp are queryable in SQLite.
   Losing the index must not lose history or accepted state.
2. A Mailbox accepts identified JSON/TOML inputs. Reusing an ID with different
   content fails. Receipt and consumption are separate, per-recipient events.
3. An Agent owns identity, responsibilities and a history. Persistent owners
   can write scoped work; disposable Luna helpers cannot. Terra judges readiness.
4. A Context is a frozen contract plus appended native history. Memory changes
   enter the prefix at compaction; retrieved evidence can append immediately.
5. An Effect has durable intent before execution and a durable receipt afterward.
   Unknown completion is not permission to execute it again.
6. Selection is a judgment. A late appendix preserves references and reasons;
   it does not automatically buy another main-agent call. Explicit feedback can
   approve a scoped preference; silence is not feedback.
7. A model-call budget includes helper calls, compaction and failures. It is
   durable across restarts, and never silently replenished.

Storage starts local. Paths are navigational views; IDs and hashes identify
originals. Accepted memory is versioned; edits are proposed and explicitly
accepted. Compaction never deletes source events or unresolved obligations.
