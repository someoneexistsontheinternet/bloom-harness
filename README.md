# Bloom v3

A persistent personal agent for macOS, with one continuous conversation,
delegated workers, source-backed memory and durable execution records.

**First public draft / experimental.** The local runtime has a regression suite,
but this is not a hosted service or a hardened multi-user agent platform.
The supported environment for this draft is **Apple Silicon macOS with Python
3.12**. Setup starts with Discord and cold storage disabled and no model selected.

## What it does

- **Astra** owns the main conversation, consequential reasoning and final decisions.
- **Sol** takes substantial research and implementation assignments. **Luna**
  supplies scoped evidence and small disposable helpers.
- SQLite records tasks, native model/tool messages, effects and delivery receipts.
  Restart reuses acknowledged work and exposes uncertain effects for reconciliation.
- Memory uses a local timeline/persona hierarchy and full-text search. Every
  semantic write requires Astra's approval; the prompt snapshot stays frozen
  until compaction.
- Web research captures up to 20 pages in signed-in Safari. Luna reviews their
  complete extracted documents and native images, selecting 3–7 sources. Each
  page appears once; DOM table/link inventories stay out of model input.
- Optional Discord delivery includes public progress, streaming previews,
  per-model usage cards, worker threads and an activity feed.
- Optional cold storage verifies copies before removing inactive local payloads.
  A local index keeps archived text searchable when the drive is disconnected.

These role names are independent of provider/model IDs. Configure models you
can access; the repository does not automatically select or purchase one.

## Start locally

Install Git, [uv](https://docs.astral.sh/uv/getting-started/installation/) and
Apple's Command Line Tools. From a checkout of this repository:

```sh
uv sync --locked --dev --python 3.12
cp config/local.example.toml config/local.toml
.venv/bin/python -m bloom.cli init
.venv/bin/python -m bloom.cli status
```

These commands initialize local state without model inference or Discord posts.
Edit the three profile model IDs and provider settings in `config/local.toml`.
Supply credentials through the configured environment variable or supported
credential store; do not put credential values in TOML or Git.

Once configured, a foreground request is:

```sh
.venv/bin/python -m bloom.cli ask 'Explain the files in this project without changing them.'
```

`ask` can invoke paid models and tools. The main and Sol agents default to full
developer access under your account. Read the [setup guide](docs/setup.md) and
[security model](SECURITY.md) before connecting accounts or running the service.

## Documentation

| Guide | Contents |
| --- | --- |
| [Setup](docs/setup.md) | Providers, Discord, Safari and first-run checks |
| [Configuration](docs/configuration.md) | Context policy, credentials, worker and evidence settings |
| [Architecture](docs/architecture.md) | Roles, execution, persistence, memory and module map |
| [Operations](docs/operations.md) | Service lifecycle, archives, backups and recovery |
| [Web research](docs/web-search.md) | Extraction, images, curation and verification |
| [Worker presets](docs/worker-presets.md) | Explicit delegation and reusable assignments |
| [Runtime tools](docs/runtime-tools.md) | Pinned ripgrep and process environments |
| [Known limitations](docs/limitations.md) | What this draft does not establish |

## Develop and verify

```sh
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_publication.py
```

The unit suite blocks unmocked HTTP calls. Real browser/model evaluation scripts
are opt-in; see their docstrings before running them. CI uses an Apple Silicon
macOS runner and does not start Discord, Safari or paid model evaluations.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow.

## Source and local data

```text
src/bloom/     Runtime, adapters, state and native browser helpers
config/        Shared defaults and worker templates; local.toml is ignored
prompts/       Astra, Sol, Luna and common tool policies
scripts/       Maintained service and evaluation workflows
tests/         Regression tests
docs/          Setup, design and operational documentation
```

`workspace/`, `memories/`, `artifacts/`, `logs/`, `worktrees/` and local notes
contain private or generated data and are excluded from Git. Do not upload a
running workspace or conversation backup as a source release.

## License

Bloom is [MIT-licensed](LICENSE). Vendored Mozilla Readability remains under
Apache-2.0; see [third-party notices](THIRD_PARTY_NOTICES.md).
