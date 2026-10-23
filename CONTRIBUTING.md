# Contributing

This is an experimental first draft. Keep changes focused and explain the
trigger, resulting behavior, evidence and verification limits.

```sh
uv sync --locked --dev --python 3.12
.venv/bin/ruff format src tests scripts
.venv/bin/ruff check src tests scripts
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_publication.py
uv build
.venv/bin/python scripts/check_publication.py --dist dist
```

Use a new isolated workspace for integration checks. Do not run a new checkout
against somebody else's live database, model account, Discord channel or native
helper. Unit tests block unmocked HTTP; the opt-in evaluation scripts can have
real effects. Read their docstrings and get appropriate authorization first.

Add regression tests at the actual failure boundary: native request construction,
restart/reuse, source validation, effect receipts or delivery. Avoid tests that
only repeat the implementation. Compile changed Python files with the project
interpreter. Browser serializer changes also need the actual Safari fixtures
when a configured GUI environment is available.

Preserve native messages and original evidence. Do not weaken validation, turn
unknown usage into zero, silently replay uncertain work or trim obligations to
make a test pass. Describe unverified integration gates explicitly.

Keep private data out of patches: no `config/local.toml`, credentials, runtime
databases, memory files, call payloads, screenshots, private notes or logs.
The publication checker is a useful guard, not a complete secret detector.
Review the staged diff and final history before sharing it.

Contributions to Bloom's original code are made under the [MIT license](LICENSE).
Preserve vendored third-party notices and record provenance for new vendored code.
