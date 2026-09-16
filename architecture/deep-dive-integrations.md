# Deep Dive: Agent Integrations

Back to [Architecture](README.md)

`eggpool configsetup` output for supported coding agents is split across a
portable boundary. `rust/crates/eggpool-client-config/` owns the
provider-neutral projection, `ConnectionProfileV1`/`epc1` codecs,
`AgentIntegrationProfileV1`, Codex/OpenCode renderers, TOML/JSONC mutation
primitives, ownership types, hashing, and validation without EggPool runtime
state. `rust/src/operations/integrations.rs` is the EggPool adapter: it
converts `Config`/catalog/database facts into those portable types, resolves
server keys/endpoints, owns local lifecycle paths, delivery, and server-only
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
catalog + strict validation, OpenCode Responses rendering, `ConnectionProfileV1`
(`eggpool.connection/v1`, closed `codex`/`opencode` targets, absolute
HTTP(S) base URL, `responses` wire fact, `bearer_env` auth reference only,
`/api/integrations/v1/profile` reference, optional issuer version),
`epc1.<base64url(canonical JSON)>` tokens (no compression; `epc1`
unambiguously fixes the algorithm), `AgentIntegrationProfileV1` with
deterministic revision, closed `ClientTarget` + `ClientAdapter`
(render/inspect/plan/verify/remove without subprocesses), TOML/JSONC
primitives, ownership manifests, and hashing.

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
model-list contract and is never overloaded as a Codex-private schema. No
remote projection endpoint is served; local `configsetup` renders from local
config/database.

## OpenCode provider contract

`build_opencode_config_json()` renders from the same projection using the
Responses-capable `@ai-sdk/openai` runtime, `baseURL` ending at `/v1`,
`apiKey` as `{env:EGGPOOL_API_KEY}`, per-model `limit.context`/`limit.output`
where known, `modalities`, and `reasoning`/`variants` only where guaranteed.
It never claims image/tool capability across a heterogeneous alias route.

## Managed lifecycle

`codex_lifecycle()` and `opencode_lifecycle()` implement
`--apply`/`--sync`/`--check`/`--remove`/`--dry-run` with idempotent
ownership manifests under EggPool state. Codex mutation owns only root
`model_provider`, root `model_catalog_json`, optional root `model` (only
when explicitly requested), and `[model_providers.eggpool]`, preserving
comments and unrelated TOML with atomic writes. OpenCode merges only
`provider.eggpool` and refuses to rewrite JSONC-comment files silently.
Both refuse on drift without `--force` and remove only EggPool-owned state.
See `docs/agent-configuration.md` for the operator contract.
