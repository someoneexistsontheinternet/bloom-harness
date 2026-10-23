# Local runtime command tools

Bloom provisions standalone ripgrep 15.2.0 from its official Apple Silicon
release. Archive and executable SHA256 values are pinned in `bloom.toolchain`;
the executable and license/provenance files stay in local `workspace/tools/`.
It does not depend on Codex's private bundled binary or modify the project venv.

```sh
.venv/bin/python -m bloom.toolchain prepare --root /path/to/bloom-v3
.venv/bin/python -m bloom.toolchain verify --root /path/to/bloom-v3
```

Installation is explicit and idempotent. Existing version directories are
verified, never overwritten. A new version gets its own directory; retain any
version used by an active release. `service.py prepare` verifies the dependency,
and launch checks its recorded identity again before replacing the service.

Local commands use `/bin/zsh --emulate sh -f -o pipefail -c`: POSIX-compatible
command semantics, no user startup files, and failed pipeline stages propagate
failure. A failed optional search should explicitly handle its exit status.
Each child receives TMPDIR/TMP/TEMP and zsh's TMPPREFIX inside its existing
per-process scratch grant. Here-documents work without broadening filesystem
access. Existing remote SSH command behavior is unchanged.

The managed executable is included in child/service PATH and granted read-only
access to scoped workers. Target fields enumerate only configured locations
that the agent may use; omitted targets select local execution. Target is not
a description field.

Evidence/recall summary lengths are guidance. Valid longer summaries retain
their full artifact bytes and do not trigger a cosmetic rewrite. Exact evidence
quotes, receipt ownership, schema validity and model context budgets remain
checked.

Upstream source: https://github.com/BurntSushi/ripgrep/releases/tag/15.2.0
