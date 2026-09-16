# Agent Configuration

`eggpool configsetup` generates configuration snippets for popular coding agents. Each target produces format-appropriate output (JSON, TOML, YAML, or shell exports) that references your running EggPool instance.

Codex and OpenCode additionally support a managed lifecycle that installs a generated model catalog/provider block with ownership, drift detection, and safe removal. All other targets keep the snippet/clipboard/output workflow.

`eggpool configremote` is the headless companion: on the EggPool host it exports a small secret-free `epc1` connection profile for Codex/OpenCode running on other machines. Local `configsetup` writes the local filesystem; `configremote` never touches a desktop filesystem and never mutates EggPool config to manufacture a setup.

## Supported Targets

| Target | Command | Output Format | `--write` Default | Model |
|--------|---------|---------------|-------------------|-------|
| OpenCode | `eggpool configsetup opencode` | JSON provider config (Responses runtime) | N/A (clipboard) | auto (all models) |
| Claude Code | `eggpool configsetup claude-code` | JSON snippet | N/A (clipboard) | N/A |
| Aider | `eggpool configsetup aider` | Shell env exports | `.env.eggpool` | recommended |
| Codex | `eggpool configsetup codex` | TOML `[model_providers.eggpool]` block (Responses wire API) + generated `model_catalog_json` | N/A (printed) | optional with catalog |
| Qwen Code | `eggpool configsetup qwen-code` | JSON provider block | N/A (printed) | optional |
| Kilo | `eggpool configsetup kilo` | JSON provider block | N/A (printed) | optional |
| Continue | `eggpool configsetup continue` | YAML model block | `~/.continue/eggpool.yaml` | usually yes |
| Cline | `eggpool configsetup cline` | JSON profile | `cline-eggpool.json` | recommended |
| Roo Code | `eggpool configsetup roo-code` | JSON profile | `roo-eggpool.json` | recommended |
| Goose | `eggpool configsetup goose` | Shell env exports | N/A (printed) | recommended |
| OpenHands | `eggpool configsetup openhands` | Shell env exports | N/A (printed) | recommended |

## Shared Options

| Option | Description |
|--------|-------------|
| `--host HOST` | Override the EggPool host (default: `localhost`) |
| `--base-url URL` | Override the full base URL |
| `--model MODEL` | Override the default model (Codex: optional root `model`; OpenCode exposes all models) |
| `--write` | Write output to the default file for the target (snippet mode only) |
| `--output PATH` | Write output to a specific file (snippet mode only) |
| `--force` | Overwrite existing output file; in lifecycle mode, converge despite detected drift |
| `--no-clipboard` | Skip copying to clipboard |
| `--print-secret` | Print the resolved API key for targets whose generated artifact embeds it; it does not change Codex/OpenCode output (both reference `EGGPOOL_API_KEY`) |

## Managed Lifecycle (Codex and OpenCode)

```sh
eggpool configsetup codex --apply
eggpool configsetup codex --sync
eggpool configsetup codex --check
eggpool configsetup codex --remove
eggpool configsetup codex --dry-run

eggpool configsetup opencode --apply
eggpool configsetup opencode --sync
eggpool configsetup opencode --check
eggpool configsetup opencode --remove
eggpool configsetup opencode --dry-run
```

Lifecycle semantics:

- `--apply`: create/update EggPool-owned generated artifacts and make the minimum safe client-config changes.
- `--sync`: recompute model catalog/provider facts and converge only EggPool-owned state. Refuses unsafe drift.
- `--check`: read-only validation. Reports whether client config, generated catalog, base URL, environment-key reference, and hashes are current. No mutations; exits non-zero on drift.
- `--remove`: remove only EggPool-owned generated files/fields when current state still matches ownership evidence; restores previously captured values where safely possible.
- `--dry-run`: render the exact proposed diff/actions without writing. Can be combined with `--apply`/`--sync`/`--remove`/`--check` or used alone.

Repeated `--apply`/`--sync` is idempotent. Drift in a user-edited client config causes a safe refusal rather than silent clobbering; re-run with `--force` to converge deliberately. `--output`/`--write` cannot be combined with lifecycle flags; the snippet workflow remains for users who do not want automatic changes.

Ownership manifests live under the EggPool state directory (`~/.local/state/eggpool/integrations/<target>/manifest.json`) and record the client config path, pre/post hashes, owned fields, previous values, catalog path/hash, and schema version. No secrets are stored.

## Remote Setup (`configremote`)

Bind (`[server].host`/`port`) is where EggPool listens; advertisement (`[integrations].advertise_base_url`) is the URL desktop clients should use. Configure the latter once on a headless host:

```toml
[integrations]
advertise_base_url = "https://pool.example.internal/v1"
```

The value must be an absolute `http://`/`https://` URL ending in `/v1` (a bare host normalizes to `.../v1`), with no credentials, fragment, or query. It is live-reloadable via `eggpool rehash` and never changes the listen socket.

```sh
eggpool configremote codex
eggpool configremote opencode --format token
eggpool configremote codex --format json
eggpool configremote opencode --base-url https://pool.example.internal/v1
```

Precedence: explicit `--base-url` wins, configured advertisement is next, and a detected LAN address is offered only when compatible with the listen config (wildcard binds via LAN detection; explicit non-loopback binds via their own host). Loopback-only without an advertisement fails with guidance rather than emitting a misleading command.

`configremote` is read-only: it never creates/rotates server keys, mutates config/transcoding, refreshes catalogs, or requires the service to be running. If the server key is missing it reports the prerequisite instead of manufacturing one. Output is secret-free (`EGGPOOL_API_KEY` reference only); `--format token` prints the raw `epc1` token and `--format json` prints the stable `eggpool.configremote/v1` object. Default human output notes that version-pinned bootstrap installers arrive in a later release; use token/JSON with `eggpool-connect` today.

The token references `GET /api/integrations/v1/profile` (authenticated, revisioned) rather than embedding a model inventory, so it stays reusable as models change.

## Desktop Helper (`eggpool-connect`)

The narrow `eggpool-connect` binary receives a secret-free `epc1` token and
configures Codex/OpenCode transactionally on Linux, macOS, and Windows. It is
not an agent harness or proxy: it only detects the local client, fetches the
current integration projection, backs up byte-exact, mutates EggPool-owned
fields atomically, validates, and rolls back automatically on failure.

```sh
eggpool-connect plan --profile 'epc1.…'
eggpool-connect install --profile 'epc1.…'
eggpool-connect verify --client codex
eggpool-connect backups --client codex
eggpool-connect restore <backup-id>
eggpool-connect remove --client codex
```

Behavior:

- `plan` never mutates the filesystem and suits workgroup troubleshooting.
- `install` shows the plan and requires confirmation by default; `--yes`
  approves non-interactively after all safety checks.
- Credentials come from `EGGPOOL_API_KEY`, a secure TTY prompt, or
  `--api-key-stdin` (never argv). The token carries no credential.
- Every mutation commits a byte-exact backup first under
  `<user-state>/eggpool-connect/backups/<id>/` (see
  [Filesystem layout](filesystem-layout.md)); post-write validation failure
  restores automatically, with a distinct rollback-failure error pinning the
  backup ID/path when recovery itself fails.
- `restore` takes a pre-restore backup first, so restore is reversible.
- `remove` deletes only EggPool-owned fields/artifacts and refuses on drift
  without `--force`.
- Repeated installs are idempotent no-ops after validation; remote revision
  changes update only owned artifacts.
- OpenCode files with JSONC comments and OpenCode V2 (`providers` plural)
  shapes fail closed with manual guidance until the preserving V2 adapter
  lands; Codex preserves comments/unrelated TOML today.
- Default setup never modifies shell profiles or persistent environment
  variables; each desktop still needs `EGGPOOL_API_KEY` in the environment
  that launches the client.

## Examples

```sh
# OpenCode — print JSON config to stdout
eggpool configsetup opencode

# OpenCode — managed install with drift detection
eggpool configsetup opencode --apply
eggpool configsetup opencode --check

# Aider — write .env.eggpool with a specific model
eggpool configsetup aider --model openai/gpt-4 --write

# Continue — write YAML to a custom path
eggpool configsetup continue --model claude-sonnet-4 --output ~/.continue/eggpool.yaml

# Cline — skip clipboard
eggpool configsetup cline --no-clipboard

# Codex — print the non-secret Responses provider block
eggpool configsetup codex --model <eggpool-model-or-alias> --no-clipboard

# Codex — managed install with generated model catalog
eggpool configsetup codex --apply
eggpool configsetup codex --check

# Roo Code — write JSON profile
eggpool configsetup roo-code --write
```

## Output Behavior

- Generated JSON, TOML, YAML, and shell snippets escape catalog/config values for the target format, including provider-suffixed model IDs.
- The `--model` flag sets an explicit model or EggPool alias. If exactly one model is available, the shared resolver can fill it automatically; when multiple models are available, EggPool does not invent a preference. With a Codex catalog installed, no top-level `model` is required: the user can choose from the Codex model UI/CLI.
- `--write` writes to a sensible default location for the target (see the table above). `--output` always takes precedence.
- Without `--write` or `--output`, the output is printed to stdout and copied to the clipboard (unless `--no-clipboard`).

## Codex Integration

Codex uses EggPool through the HTTP/SSE Responses path. The standard `/v1/models` endpoint intentionally keeps its OpenAI-compatible schema and is not a Codex remote-catalog endpoint. Rich picker discovery is provided locally through a generated `model_catalog_json` file owned by EggPool.

The managed catalog is derived from EggPool's existing catalog/model-info/routing facts as a provider-neutral projection: context/output limits are conservative minimums, boolean features and input modalities are intersections, reasoning efforts intersect, unknown stays unknown (never optimistic), no capability is inferred from model-ID substrings, and WebSockets/remote compaction stay disabled. The generated JSON is deterministic, bounded, and carries no credentials or provider-private source metadata.

The current generated block with a managed catalog is equivalent to:

```toml
model_provider = "eggpool"
model_catalog_json = "/home/user/.local/state/eggpool/integrations/codex/eggpool-codex-models.json"

[model_providers.eggpool]
name = "EggPool"
base_url = "http://127.0.0.1:11300/v1"
env_key = "EGGPOOL_API_KEY"
wire_api = "responses"
supports_websockets = false
```

Set `EGGPOOL_API_KEY` to EggPool's server key in the environment used to run Codex. Retrieve the current key without putting it in the TOML with:

```bash
export EGGPOOL_API_KEY="$(eggpool getkey)"
```

The default server port is `11300`; use `--base-url` when the server is configured differently. Generate a model-specific snippet with:

```bash
eggpool configsetup codex --model <eggpool-model-or-alias>
```

The generated TOML contains `env_key = "EGGPOOL_API_KEY"`, never the resolved server key, so Codex output is printed normally without `--print-secret`. Passing `--print-secret` does not embed the key in Codex TOML. When `CODEX_HOME` is set, managed commands respect it for locating `config.toml`; the generated catalog itself stays under EggPool state unless the Codex contract requires otherwise. Codex config mutation owns only root `model_provider`, root `model_catalog_json`, optional root `model` (only when explicitly requested), and the `[model_providers.eggpool]` table, preserving comments and unrelated content with atomic writes and drift refusal. For a real current CLI check, see [Codex compatibility smoke](codex-compatibility-smoke.md).

EggPool relies on Codex local compaction by default and does not advertise remote v2 compaction: the generated provider keeps the current custom-provider `Unsupported` behavior until the complete trigger request/result path is qualified end to end. The catalog advertises a conservative 90% auto-compaction threshold derived from each model's guaranteed context window. Operators whose upstream natively supports the historical compact contract may opt in per provider surface with `supports_remote_compaction_v1 = true` plus a `compact_path_template`; the bounded `POST /v1/responses/compact` operation then forwards natively with normal routing, accounting, and health ownership. See [Stateless Responses](stateless-responses.md).

## OpenCode Integration

`eggpool configsetup opencode` generates an OpenCode-compatible JSON configuration from the same provider-neutral projection. The provider uses the Responses-capable `@ai-sdk/openai` runtime with `baseURL` ending at `/v1`, per-model `limit.context`/`limit.output` where known, `modalities` (text plus image only when guaranteed), and `reasoning`/`variants` only where the projection guarantees reasoning. Image/tool capability is never claimed across a heterogeneous alias route.

The generated file references `EGGPOOL_API_KEY` through OpenCode's `{env:EGGPOOL_API_KEY}` interpolation and never embeds the resolved server key, so OpenCode output prints by default like Codex. Set the variable in the environment that launches OpenCode:

```bash
export EGGPOOL_API_KEY="$(eggpool getkey)"
```

Managed OpenCode installation merges only the `provider.eggpool` key, preserving all other config content. Existing files with JSONC comments are never silently rewritten: `--apply` refuses with guidance to use `--dry-run`/generated output until a safe preserving mutator is available. Global config lives at `~/.config/opencode/opencode.json` (`OPENCODE_CONFIG` overrides).

Provider-scoped model IDs are used when `models.collapse_models = false` (the default), so OpenCode can disambiguate providers serving the same upstream model.
