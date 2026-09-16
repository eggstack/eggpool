# Deep Dive: Agent Integrations

Back to [Architecture](README.md)

`eggpool configsetup` output for supported coding agents is split across a
portable boundary. `rust/crates/eggpool-client-config/` owns the
provider-neutral projection, `ConnectionProfileV1`/`epc1` codecs,
`AgentIntegrationProfileV1`, Codex/OpenCode V1/V2 renderers, the narrow
TOML mutator (`text.rs`/`codex.rs`), the trivia-preserving JSONC scanner and
structural editor (`jsonc.rs`/`opencode.rs`), variant selection, ownership
types, hashing, and validation without EggPool runtime state.
`rust/src/operations/integrations.rs` is the EggPool adapter: it converts
`Config`/catalog/database facts into those portable types, resolves server
keys/endpoints, owns local lifecycle paths, delivery, and server-only
projection loading. It generates provider-neutral endpoint, model, and secret
references for each target without performing provider calls or persisting
credentials.

Integration generation is a CLI operation over the resolved configuration.
Generated files are written only when the operator requests an output path;
stdout modes remain suitable for review and shell piping. The native runtime
continues to serve all generated endpoints.

## Boundary and ownership

Portable policy (crate, no Axum/Tokio/SQLite/Eggress/provider transport):
projection (`AgentModelProjection`, conservative aggregation), Codex TOML +
catalog + strict validation + semantic owned-field matching, OpenCode V1/V2
Responses rendering with shape-first variant selection, `ConnectionProfileV1`
(`eggpool.connection/v1`, closed `codex`/`opencode` targets, absolute
HTTP(S) base URL, `responses` wire fact, `bearer_env` auth reference only,
`/api/integrations/v1/profile` reference, optional issuer version),
`epc1.<base64url(canonical JSON)>` tokens (no compression; `epc1`
unambiguously fixes the algorithm), `AgentIntegrationProfileV1` with
deterministic revision, closed `ClientTarget` + `ClientAdapter`
(render/inspect/plan/verify/remove without subprocesses), the narrow TOML
mutator plus the token-offset JSONC editor (comment/trailing-comma
preserving splices with bounded parse locations), V1/V2 sync/remove policy
helpers, ownership manifests (including applied-model and previous-entry
captures), and hashing.

Application-owned (EggPool adapter): `Config`/catalog/database reads,
conservative projection from authoritative server state, server key policy,
advertised endpoint choice, `CODEX_HOME`/`OPENCODE_CONFIG`/XDG/EggPool
state-dir resolution, clipboard/process delivery, HTTP serving, transcoder
mutation, and runtime paths. Receiving-machine paths/commands never come
from a profile; the desktop decides local paths from its adapter.

## Shared generation and delivery contract

`resolve_model()` uses an explicit `--model` first, fills a model only when the
catalog contains exactly one model, and otherwise returns no model unless the
target requires one in write mode. It never invents a preference from a
multi-model catalog. Renderers own format escaping and must not turn a missing
model into an empty or fabricated selection.

`Target::contains_secret()` describes the rendered artifact, not whether the
operation had to resolve the server key. Targets that embed the resolved key
remain hidden from stdout unless `--print-secret` is supplied. Codex and
OpenCode are the exceptions: Codex TOML contains only
`env_key = "EGGPOOL_API_KEY"` and OpenCode JSON uses
`{env:EGGPOOL_API_KEY}` interpolation, so both snippets are non-secret, print
by default, and `--print-secret` does not alter them. The delivery hints tell
operators to set that environment variable and use `eggpool getkey` to
retrieve the current value. Configsetup never executes a shell or writes a
shell profile.

## Provider-neutral agent projection

`AgentModelCapabilities`/`AgentModelProjection` is the single integration
projection derived from existing catalog/model-info/routing facts
(`IntegrationModel`, `ModelLimits`, validated capability JSON). Rules:

- derive only from validated facts; unknown stays `None`, not optimistic true;
- never infer capability from model-ID substrings;
- `websockets` remains false until a real Responses WebSocket path exists;
- remote compaction is not advertised here;
- provider-private source metadata never leaks into the projection.

`aggregate_projections()` collapses heterogeneous alias targets
conservatively: context/output are minimum known guaranteed values, boolean
features and input modalities intersect, reasoning efforts intersect, and
unknown on any required candidate remains unknown. It never publishes the
union of possible features.

## Codex provider contract

`build_codex_toml_snippet()` emits the generic HTTP/SSE Responses provider
shape used by the qualified Codex path:

```toml
model_provider = "eggpool"
model = "<optional explicit model-or-alias>"

[model_providers.eggpool]
name = "EggPool"
base_url = "http://<host>:<port>/v1"
wire_api = "responses"
supports_websockets = false
env_key = "EGGPOOL_API_KEY"
```

The top-level `model` line is omitted when no model was selected. With a
managed catalog installed, no top-level model is required: the user picks
from the Codex model UI/CLI. The configuration references `EGGPOOL_API_KEY`
and never embeds the resolved key.

`build_codex_toml_snippet_with_catalog()` adds root-level
`model_catalog_json` pointing at the EggPool-owned generated artifact (by
default under the EggPool state directory,
`~/.local/state/eggpool/integrations/codex/eggpool-codex-models.json`).
`CODEX_HOME` is respected for locating `config.toml`.

## Codex model catalog

`build_codex_catalog_json()` renders a deterministic, bounded
(`MAX_CODEX_CATALOG_MODELS`/`MAX_CODEX_CATALOG_BYTES`), sanitized Codex
catalog from the projection. Each entry carries the EggPool public ID as
`slug`, display name, conservative context/output facts, reasoning efforts
where guaranteed, `shell_type = "shell_command"`, `visibility = "list"`,
`supported_in_api = true`, `prefer_websockets = false`, and a 90%
`auto_compact_token_limit` derived from the guaranteed context window.
Every entry always emits `supported_reasoning_levels` (empty when no
reasoning is guaranteed) and `base_instructions = ""` (no EggPool override);
both are required by current Codex strict parsing (qualified against Codex
CLI 0.154.0 via `codex debug models` 2026-09-16) and must not be used to
fabricate provider instructions. `validate_codex_catalog_json()` enforces
the strict-parser contract (slug and display name present, reasoning-levels
array present, base-instructions or template instructions present, no
WebSocket advertisement, no embedded secret).

The standard EggPool `/v1/models` endpoint remains the OpenAI-compatible
model-list contract and is never overloaded as a Codex-private schema. Rich
remote facts use the separately versioned authenticated
`GET /api/integrations/v1/profile` endpoint (see below); local `configsetup`
renders from local config/database.

## Advertised integration endpoint

`[server].host`/`port` describe the local listen socket. Remote clients need a
separate advertised fact (DNS, reverse proxy, Tailscale/WireGuard, LAN
interface). `rust/src/config.rs::IntegrationsConfig::advertise_base_url`
(`[integrations].advertise_base_url`) owns that contract:

```toml
[integrations]
advertise_base_url = "https://pool.example.internal/v1"
```

Validation (`normalize_advertise_base_url`): absolute `http://`/`https://`,
authority/host required, no userinfo/fragment/query, no whitespace/control,
bounded length, trailing slash stripped, normalized to the EggPool API root
(`.../v1`). A bare host normalizes to `.../v1`; arbitrary paths are rejected
rather than silently rewritten. The transition
`integrations.advertise_base_url` is `Live` in
`rust/src/config_reload_policy.rs` (profile output only; no socket or runtime
change).

Resolution (`resolve_advertised_base_url`): explicit `--base-url` wins,
configured advertisement is next, detected LAN is offered only when
structurally compatible with the listen config (wildcard binds via LAN
detection, explicit non-loopback binds via their own host), and loopback-only
without an explicit advertisement fails with guidance rather than emitting a
misleading shareable command.

## Remote export (`eggpool configremote`)

`rust/src/cli.rs::ConfigremoteArgs` (`eggpool configremote codex|opencode
[--base-url URL] [--format command|token|json] [--shell auto|posix|powershell|all]
[--no-bootstrap]`) is a read-only exporter. `build_remote_context()` reads
validated config, reads persisted catalog facts best-effort (missing DB yields
static models only and never creates the database file), merges
static/overrides through the authoritative projection path, resolves the
advertised URL, and reports `EGGPOOL_API_KEY` plus `auth_configured` without
creating/rotating keys, mutating config/transcoding, refreshing catalogs, or
sending upstream requests. `remote_connection_token()` builds the portable
`ConnectionProfileV1` + `epc1` token; `--format json` emits the stable bounded
`eggpool.configremote/v1` object (now with a `bootstrap` section carrying the
pinned version/tag, per-shell asset URLs, helper asset names, and both shell
commands); default human output shows target, endpoint, auth reference, token,
and the `--shell`-selected bootstrap block. `runtime.rs` remains
the presentation adapter; reusable construction lives in
`operations/integrations.rs`.

## Desktop bootstrap rendering

`render_connect_posix()` / `render_connect_powershell()` (selected via
`render_connect_bootstrap()` for `--shell auto|posix|powershell|all`) emit
copy/paste blocks pinned to the running EggPool version
(`RELEASE_REPOSITORY = "eggstack/eggpool"`, immutable
`/releases/download/vX.Y.Z/` URLs, no `latest`). Each block downloads the
reviewed bootstrap plus the release SHA256SUMS over HTTPS, verifies the
bootstrap hash, then runs the bootstrap with the token single-quoted as a
data argument (`posix_shell_quote()` / `powershell_quote()`, hostile-token
tested; no `eval`/`Invoke-Expression`, no credential assignment). Blocks stay
within `MAX_BOOTSTRAP_COMMAND_LEN`. The bootstraps themselves
(`packaging/connect/eggpool-connect.sh`, `eggpool-connect.ps1`) only
select/download/verify/execute the matching helper binary and own no client
mutation logic; helper binaries are the `eggpool-connect-*` release assets
with `SHA256SUMS` as the integrity contract (see the deployment deep dive).

## Integration-profile API

`GET /api/integrations/v1/profile` (`rust/src/server/health.rs::
integration_profile`, route in `rust/src/server/mod.rs`) serves the portable
`AgentIntegrationProfileV1` from the same conservative projection as local
setup. It is authenticated through the existing middleware contract
(`requires_auth()` returns true for `/api/integrations/*` even when the
dashboard is public) and performs no catalog refresh, upstream request, or
health mutation. Response: deterministic schema version, normalized advertised
`base_url`, deterministic `revision` (canonical sanitized-content SHA-256, not
timestamps/row IDs), models in deterministic public-ID order, conservative
capabilities/limits only, bounded bytes/model count, no keys, no
provider-private source metadata, no paths/prompts/user data. Headers:
`Content-Type: application/json`, `Cache-Control: private, max-age=0,
must-revalidate`, `ETag: "<revision>"` with `If-None-Match` → `304`.
Errors are bounded and generic (`401`/`403` auth, `503` unavailable with no
internal body).

## OpenCode provider contracts

Both variants render from the same conservative projection and never claim
image/tool capability across a heterogeneous alias route:

- V1 (`provider` / `npm` / `options`, qualified against OpenCode 1.18.30):
  the Responses-capable `@ai-sdk/openai` runtime, `baseURL` ending at `/v1`,
  `apiKey` as `{env:EGGPOOL_API_KEY}`, per-model `limit.context`/
  `limit.output` where known, `modalities`, and `reasoning`/`variants` only
  where guaranteed.
- V2 (`providers` / `package` / `settings`, current V2 docs): the
  Responses-capable `@opencode/ai/providers/openai-compatible/responses`
  package, `settings.baseURL`, `env: ["EGGPOOL_API_KEY"]`, per-model `limit`
  and `capabilities` (tool support only when exactly known), no WebSocket
  transport, and no reasoning-effort variants until a proven package mapping
  exists.

Variant selection is shape-first (`providers` selects V2, `provider` selects
V1, both present refuses, empty defaults to V1 with newer-major versions
selecting V2); both key families are never written into one file. All
mutations go through the token-offset JSONC editor: only the owned `eggpool`
entry (plus its parent key when EggPool creates it) is spliced, so comments,
trailing commas, indentation, ordering, and unrelated providers survive
`--apply`/`--sync`/`--remove`. A pre-existing `eggpool` entry is captured
exactly on first ownership and restored byte-for-byte on remove; an emptied
parent is removed only when comment-free. Invalid JSONC and ambiguous shapes
fail closed with bounded line/column locations.

## Managed lifecycle

`codex_lifecycle()` and `opencode_lifecycle()` implement
`--apply`/`--sync`/`--check`/`--remove`/`--dry-run` with idempotent
ownership manifests under EggPool state. Codex mutation owns only root
`model_provider`, root `model_catalog_json`, optional root `model` (only
when explicitly requested), and `[model_providers.eggpool]`, preserving
comments (including file footers appended after the managed table) and
unrelated TOML with atomic writes; a pre-existing provider table is captured
on first ownership and restored exactly on remove. Drift is decided on
EggPool-owned fields rather than whole-file hashes: unrelated edits are
preserved and converge, while owned-field changes (including model-list-only
vs identity changes distinguished by the revision-only sync policy) refuse
without `--force`. Both lifecycles remove only EggPool-owned state.
See `docs/agent-configuration.md` for the operator contract.

## Format-preserving dependency gate

Plan 213 evaluated `toml_edit` (TOML) and `jsonc-parser` (JSONC) at
implementation time and adopted neither. The qualified narrow line-oriented
TOML mutator plus the crate-local JSONC token scanner cover the full
fixture matrix (empty files, comments before/inside/after owned blocks,
line + block comments, trailing commas, unrelated providers/settings,
pre-existing `eggpool` entries, external edits, malformed input) with zero
new audit surface, zero release-binary impact, and no MSRV pressure on the
Rust 1.81-compatible portable crate. `cargo deny`, `cargo tree -e features`,
and `cargo tree --duplicates` therefore report no new third-party
dependencies for this plan. Revisit only if client schemas require
structural operations the narrow editors cannot express.

## Transactional desktop helper (`eggpool-connect`)

`rust/crates/eggpool-connect/` is the small desktop counterpart over the
same portable crate. It owns only receiving-machine concerns and never
duplicates renderers:

- CLI: `plan` (read-only, no filesystem mutation), `install` (plan +
  confirmation by default, `--yes` for non-interactive use after safety
  checks), `verify --client`, `backups`, `restore <id>`, `remove --client`
  with `--config` overrides, `--api-key-stdin`, `--no-verify-network`,
  `--json`, and `--force` for owned drift only.
- Sequence before any write: decode/validate `ConnectionProfileV1`, resolve
  the advertised URL, obtain the credential separately (`EGGPOOL_API_KEY` >
  TTY prompt > `--api-key-stdin`; never argv), fetch the authenticated
  `GET /api/integrations/v1/profile` over narrow Hyper/Rustls with TLS
  verification and bounded responses, validate schema/bounds/revision, detect
  the local client/version, and plan the mutation.
- State machine: `Decoded -> RemoteProfileValidated -> ClientDetected ->
  MutationPlanned -> BackupCommitted -> ConfigWritten -> LocalParseValidated
  -> ClientNativeValidated -> Committed`. Before `BackupCommitted` no target
  file changes; after it every failure attempts automatic restoration and
  reports both the original failure and the rollback result, with a distinct
  rollback-failure error pinning backup ID/path (never contents/secrets).
- Backups: byte-exact snapshots under `<user-state>/eggpool-connect/
  backups/<id>/` (`manifest.json` + `config.bin` + `generated-artifacts/…`),
  user-private (`0o700`/`0o600` on POSIX), secret-free manifests (profile
  fingerprint, never credentials), conservative retention (newest 10 per
  target, never the only recovery point). `restore` takes a pre-restore
  backup first, making restore itself reversible.
- Mutation: regular-file-or-absent check with symlink/device/FIFO/socket
  refusal, complete proposed bytes in memory, pre-write parse validation,
  same-directory temp file with restrictive permissions, fsync, atomic
  rename, and permission preservation. No in-place truncate writes.
- Validation: Layer 1 re-parses installed config/catalog through the shared
  adapter (EggPool-owned fields only); Layer 2 runs `codex debug models` +
  `codex doctor --json` or `opencode models` time-bound with bounded
  redacted diagnostics and no inference/quota use. Missing executables
  require explicit `--yes` consent instead of pretending success.
- `remove` is ownership-aware (restores captured previous values where
  valid, refuses on drift without `--force`); repeated installs are
  idempotent no-ops after validation; remote revision updates converge only
  owned artifacts without noisy rewrites. Credential persistence is deferred:
  default setup never touches shell profiles or persistent environment state.
- Dependency surface (verified via `cargo tree -p eggpool-connect`): Clap,
  Serde/JSON, SHA-2, TOML, narrow Hyper/Rustls/`webpki-roots`, Tokio, and the
  portable crate. No Axum, SQLite, Eggress, routing, provider codecs, or
  dashboard assets, so publishing a Windows helper never implies Windows
  proxy support.
