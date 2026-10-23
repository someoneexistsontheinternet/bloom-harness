# Setup

## Supported environment

This draft targets Apple Silicon macOS and Python 3.12. Install Git, uv and
Apple Command Line Tools. Safari, its native bridge, Screen Recording,
Accessibility, launchd and sandbox-exec are macOS integrations. The pinned
ripgrep installer currently supports arm64 macOS only. No Linux/Windows support
is claimed by a successful source build.

Use a source checkout. Configuration and role prompts live beside the code;
the wheel is not a standalone application bundle. Do not move a running venv
or copy a live workspace into a different installation.

```sh
uv sync --locked --dev --python 3.12
cp config/local.example.toml config/local.toml
.venv/bin/python -m bloom.cli init
.venv/bin/python -m bloom.cli status
```

Initialization creates local SQLite/directories. It does not purchase inference,
connect Discord, install a LaunchAgent or import old conversations.

## Model provider

Set an accessible model ID for each role in `config/local.toml`. The default
provider is the Responses adapter named `openai`, configured with its API base
URL and the environment variable name `OPENAI_API_KEY`. Credentials are read
at inference time. Model placeholders fail before a request is sent.

The reference policy configures a 1M context window and native reasoning/tool
messages. Choose models and provider capabilities that match it; a configuration
value alone does not establish provider support. Change effort and request
options deliberately. The Chat Completions adapter is available through
`kind = "chat"`; adapter support is not a claim that arbitrary providers or
models have been verified.

An optional Codex-auth adapter remains in the implementation. It uses the local
Codex credential store and authentication helper. It is not needed for the
default setup and is not a stable public API compatibility promise. The optional
browser-inference example is experimental and requires explicit UI selection.
Neither adapter copies credentials into the repository.

Inspect configured profiles without buying inference:

```sh
.venv/bin/python -m bloom.cli doctor
```

`doctor` checks local desktop capabilities without requesting permission. Its
`--discord` flag additionally contacts Discord and requires a configured token.
When you intend to make a live request, run `bloom.cli ask` as shown in README.
Foreground processes inherit their shell environment; GUI services have a
separate launch context. A terminal credential variable is not automatically
available to launchd. Configure credentials in the actual runtime context.

## Discord (optional)

Create/install your bot through Discord's developer settings. Configure the
bot, owner, guild and main-channel IDs in local TOML, grant channel access and
the necessary message-content/history permissions, then enable Discord.
Intake currently serves the configured owner, not every member of a guild.

Supply the bot token through `BLOOM_DISCORD_TOKEN` or a login Keychain generic
password with the configured service/account (defaults: `bloom.discord.bot-token`
and `discord-bot`). Use Keychain Access or your credential manager to create it;
do not paste it into a shell command, issue, TOML file or model conversation.
Verify Keychain access in the actual GUI login/service context.

An activity channel is optional. It receives detailed model/tool JSON and can
contain private source material. Restrict access accordingly. Enabling the
runtime can process owner messages and send replies; it is not a read-only test.

## Safari and desktop tools (optional)

Enable Safari's JavaScript through Apple Events setting. Prepare the native
helper from the same checkout/release as the runtime:

```sh
bloom_setup_run="local-$(date +%Y%m%d_%H%M%S)"
.venv/bin/python -m bloom.safari_helper prepare --root "$PWD" --logs "$PWD/logs/browser-setup/$bloom_setup_run"
.venv/bin/python -m bloom.safari_helper setup --root "$PWD"
.venv/bin/python -m bloom.safari_helper check --root "$PWD"
```

Use the helper's explicit setup window to authorize Safari access. Screen
Recording is needed for capture, and Accessibility for desktop actions. Service
requests do not open permission dialogs. A settings toggle is not proof of a
working capture: run the [browser fixtures](web-search.md) in the GUI context.
Ad-hoc binary changes can require renewed grants; never replace a running helper.

Search uses task-owned windows in ordinary signed-in Safari. It does not export
cookies. Captured text and selected images are sent to your configured model
provider. It does not interact with arbitrary existing tabs.

## Background service

Prepare the pinned runtime command tool, then follow [operations](operations.md).
The source must be committed before a frozen release can be prepared.

```sh
.venv/bin/python -m bloom.toolchain prepare --root "$PWD"
.venv/bin/python scripts/service.py prepare
.venv/bin/python scripts/service.py status
```

Preparation alone does not start the agent. One installation per macOS user is
supported by the default launchd labels. Do not start another copy against a
running installation's data.
