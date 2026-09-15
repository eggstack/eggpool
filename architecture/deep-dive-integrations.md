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
remain hidden from stdout unless `--print-secret` is supplied. Codex is the
exception because its TOML contains only `env_key = "EGGPOOL_API_KEY"`; its
snippet is non-secret, prints by default, and `--print-secret` does not alter
it. The Codex delivery hint tells operators to set that environment variable
and use `eggpool getkey` to retrieve the current value. Configsetup never
executes a shell or writes a shell profile.

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

The top-level `model` line is omitted when no model was selected. The
configuration is deliberately not a Codex-private catalog or WebSocket
integration, and standard EggPool `/v1/models` discovery remains unchanged.
