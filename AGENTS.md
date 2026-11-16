# Working on the v4 experiment

Read README.md and docs/contracts.md. Private execution state is in ignored
notes/current.md when present. V3 is a separate, stopped reference installation.

- Commit each coherent feature after focused verification. README must describe
  the feature and its real limits. Do not accumulate an unreviewable mega-commit.
- Use this checkout's .venv and uv. Compile changed Python files and exercise
  their behavior. No paid model calls in the ordinary test suite.
- Files are the durable source; SQLite is a disposable index. Preserve events,
  original results, pending work, source references and acknowledged effects.
- Keep native history stable until explicit compaction. Never execute partial
  tool arguments or silently retry an uncertain request or process.
- Persistent owners approve their private memory; Astra approves shared memory.
  Luna reads and reports. Terra's working compaction does not publish beliefs.
- Keep credentials, local notes, transcripts and runtime state out of Git.
  Never print authentication material or copy private v3 history here.
- No Discord, browser service, public web server or production deployment is
  part of this CLI experiment. Inspect before changing another process.
- Report actual failure boundaries. Synthetic provider tests do not establish
  live model compatibility. Keep live verification explicitly budgeted.
