# Configuration

`config/default.toml` contains shared policy. Ignored `config/local.toml`
recursively overrides it. `BLOOM_ROOT` selects the data/workspace root;
`BLOOM_SOURCE_ROOT` selects frozen source/config/prompts for a deployed release.
Normal checkout use needs neither variable.

## Models and context

Role names do not select models automatically. Public defaults contain model
placeholders and use an environment-authenticated Responses provider. Copy
[the local example](../config/local.example.toml) and configure all three roles.

| Role | Configured window | Compaction trigger | Rebuild budget |
| --- | ---: | ---: | ---: |
| Astra | 1,000,000 | 400,000 | 100,000 |
| Sol | 1,000,000 | 272,000 | 96,000 |
| Luna | 1,000,000 | 512,000 | 256,000 |

Each profile reserves 32,768 output tokens. Luna input/compaction policies have
an explicit 256k floor. These are configured policies, not provider capability
discovery or minimum actual request sizes. Verify model availability and capacity.

Astra's snapshot and native history stay append-only between compactions.
Idle maintenance requires 20 minutes without owner activity and more than 100k
uncompressed estimated tokens. Memory publication requires Astra's exact
approval and remains separate from working-context compression.

## Evidence and concurrency

| Setting | Default |
| --- | ---: |
| Active agents | 4 |
| Local supervised processes | 2 |
| Parallel tools per agent | 16 |
| Web candidates / selected sources | 20 / 3–7 |
| Useful visual sources | 2 |
| Large-result curation threshold | 50,000 characters |
| Individual research working result | 6,000 text tokens |
| Astra / Sol task research budget | 18,000 / 64,000 text tokens |
| Sol handoff | 4,000 tokens |

Research budgets support explicit expansion and exact saved-range reads; they
do not delete original evidence. Luna avoids generic result curation, while
still using the same nonduplicated web-document representation.

Worker templates are in `config/agent-presets/`; see [worker presets](worker-presets.md).
Foreground joins wait locally without paid model polling. Supplementary workers
can be optional dependencies and report at a later natural inference boundary.

## Credentials and integrations

Provider configuration stores credential **names/paths**, not credential values.
Keep tokens in the selected environment/credential store. Discord and storage
are disabled by default and all account/channel IDs are empty.

Cold storage requires an existing mounted volume identity, mount path and cold
root. A missing drive leaves local data in place; it must never create a fake
mount directory on the SSD. Configure only deliberate SSH targets with explicit
project roots, interpreters and known-host files.

See [setup](setup.md), [operations](operations.md) and [security](../SECURITY.md).
