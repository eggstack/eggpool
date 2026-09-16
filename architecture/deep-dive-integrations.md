# Deep Dive: Agent Integrations

Back to [Architecture](README.md)

`rust/src/operations/integrations.rs` owns `eggpool configsetup` output for
supported coding agents. It generates provider-neutral endpoint, model, and
secret references for each target without performing provider calls or
persisting credentials.

Integration generation is a CLI operation over the resolved configuration.
Generated files are written only when the operator requests an output path;
stdout modes remain suitable for review and shell piping. The native runtime
continues to serve all generated endpoints.

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
`validate_codex_catalog_json()` enforces the strict-parser contract (slug and
display name present, no WebSocket advertisement, no embedded secret).

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
