[![PyPI version](https://badge.fury.io/py/eggpool.svg)](https://pypi.org/project/eggpool/)
[![Crates.io version](https://img.shields.io/crates/v/eggpool.svg)](https://crates.io/crates/eggpool)
[![Rust runtime](https://img.shields.io/badge/runtime-Rust-orange.svg)](https://www.rust-lang.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/eggstack/eggpool/blob/main/LICENSE)
[![CI](https://github.com/eggstack/eggpool/actions/workflows/ci.yml/badge.svg)](https://github.com/eggstack/eggpool/actions/workflows/ci.yml)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/eggpool?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/eggpool)
[![Crates.io Downloads](https://img.shields.io/crates/d/eggpool.svg)](https://crates.io/crates/eggpool)

# EggPool

EggPool is a self-hosted proxy that combines AI provider accounts behind
familiar OpenAI and Anthropic API endpoints. Run it on your computer or LAN,
then point coding agents and other clients at one local service.

## What EggPool does

- Routes requests across configured providers and accounts using health, quota, and priority information.
- Translates supported OpenAI and Anthropic request formats. Gemini wire formats are also supported.
- Provides a dashboard, model discovery, usage tracking, and optional local user accounts with personal API tokens.
- Supports per-account outbound proxies, including SSH, and runs on Linux x86_64, Linux aarch64, and macOS arm64.

Windows and other unqualified targets are unsupported for the EggPool server.

## Install and start

The recommended binary-first installer fetches the verified native executable
from GitHub raw releases and installs it as `standalone-rust`. It verifies the
SHA-256 checksum before execution and never invokes uv, pipx, pip, Python, Cargo,
or a source build.

```bash
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/eggstack/eggpool/main/scripts/install.sh | bash' && \
  export PATH="$HOME/.local/bin:$PATH"
eggpool onboard
```

`eggpool onboard` creates a config if needed, guides you through provider
setup, creates the first administrator on a new database, and starts the
server. Save the personal API token it displays once. If output is redirected,
use `eggpool onboard --show-secret` only on a secure console. For unattended
password input, use `--password-stdin`.

Package-manager alternatives are `uv tool install eggpool`, `pipx install eggpool`,
and `cargo install eggpool` (Rust 1.89 or newer). These alternatives use their
package manager for updates. The native installer supports Linux x86_64, Linux
aarch64, and macOS arm64.

For a systemd service or production deployment, see
[Deployment](docs/deployment.md). For installation ownership, exact upgrades,
and rollback, see [Upgrading](docs/upgrading.md).

## Make your first request

Set the personal token from onboarding in the environment used by your client:

```bash
export EGGPOOL_API_KEY='<personal-token-shown-once>'

# Liveness is public. Model discovery and inference require a personal token.
curl http://127.0.0.1:11300/v1/healthz
curl -H "Authorization: Bearer $EGGPOOL_API_KEY" http://127.0.0.1:11300/v1/models
```

Use a model ID returned by `/v1/models`:

```bash
curl -H "Authorization: Bearer $EGGPOOL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"MODEL_ID","messages":[{"role":"user","content":"Hello!"}]}' \
  http://127.0.0.1:11300/v1/chat/completions
```

The API supports OpenAI Chat Completions, stateless OpenAI Responses, Anthropic
Messages, and embeddings when an operator configures an eligible provider. See
the [API reference](docs/api-reference.md).

## Coding agents

Generate a local client configuration or export a remote profile for another
machine:

```bash
eggpool configsetup opencode --apply
eggpool configsetup codex --model <eggpool-model-or-alias>
eggpool configremote codex
```

Generated configurations reference `EGGPOOL_API_KEY`; they do not contain the
resolved key. Use a personal token for each managed user. See
[Agent Configuration](docs/agent-configuration.md) for supported clients,
managed setup, and desktop installation.

## LAN access and security

EggPool listens on `0.0.0.0:11300` by default. Keep the port on a trusted LAN
or VPN and do not expose it directly to the public internet. Browser password
sign-in requires loopback or trusted HTTPS proxy transport. For a headless
device, use SSH port forwarding. For a team, use an operator-managed HTTPS
proxy or VPN. See [Firewall](docs/firewall.md).

The personal token authenticates inference, model discovery, and integration profiles. It does not grant dashboard or administrator access. The shared `[server].api_key` is for legacy installations and is retired after local user access is initialized.

## Configuration and updates

Configuration is read from `--config`, then `$EGGPOOL_CONFIG`, then
`$XDG_CONFIG_HOME/eggpool/config.toml` (or `~/.config/eggpool/config.toml`
when unset, if present), then `./config.toml`. Provider credentials belong in
environment variables or the config-adjacent `.env` file, not in `config.toml`.
Prefer `eggpool connect` to add an account; see [Configuration](docs/configuration.md)
and [Providers](docs/providers.md).

```bash
eggpool check-config
eggpool update
eggpool update 0.8.0
eggpool install-provenance
```

`eggpool update` installs the latest catalogued release. A version argument
selects that exact catalogued release. `eggpool install-provenance` shows which
package manager or standalone updater owns the installation.

## Documentation

| Guide | Use it for |
|---|---|
| [Deployment](docs/deployment.md) | Personal, systemd, and production setup |
| [Providers](docs/providers.md) | Provider setup, account routing, and model discovery |
| [Agent Configuration](docs/agent-configuration.md) | Coding-agent setup and remote profiles |
| [CLI Reference](docs/cli-reference.md) | Command options and local administration |
| [API Reference](docs/api-reference.md) | Endpoints, authentication, and request limits |
| [Configuration](docs/configuration.md) | Model-router settings |
| [Backups and Restore](docs/backup-restore.md) | Backup, restore, and recovery |
| [Upgrading](docs/upgrading.md) | Updates, rollback, and installation ownership |
| [Raspberry Pi](docs/raspberry-pi.md) | Single-board computer setup |
| [Proxy Configuration](docs/proxy.md) | Per-account outbound proxies |

Additional references cover [protocol transcoding](docs/transcoding.md),
[stateless Responses](docs/stateless-responses.md), [model routing](docs/model-routing.md),
[context limits and budgets](docs/model-limits.md), [reasoning](docs/thinking.md),
and [database recovery](docs/runbooks/database-recovery.md).

## Development

The runtime is Rust. See [AGENTS.md](AGENTS.md) for current formatting, lint,
test, and tooling commands.

## License

MIT
