---
name: documentation
description: Documentation maintenance for the native Rust EggPool runtime and its tooling boundary.
---

# Documentation Maintenance

## Doc map

| Location | Audience | Content |
|---|---|---|
| `README.md` | New users | Current install, CLI, and development flow |
| `docs/` | Operators | Deployment, providers, API, runbooks, release/rollback |
| `architecture/` | Contributors | Current Rust design and ownership |
| `plans/` | Maintainers | Historical plans and current maintenance records |
| `AGENTS.md`, `.opencode/skills/` | Agents | Repository workflow and task guidance |

## Rules

- Architecture and operational docs describe the shipped Rust runtime, not a
  duplicated historical application.
- Explain that `Requires-Python >=3.11` is package-manager compatibility for
  explicit historical targets, not a production interpreter dependency.
- Historical Python source is recoverable from immutable Git history; it is not
  an active source package.
- Verify every command, path, route, environment variable, and relative link
  against the current tree before documenting it.
- Treat `rust/Cargo.toml` as the authority for native dependency and feature
  claims. Do not document proxy, TLS, SQLite, or archive capabilities that are
  not present in the resolved Cargo feature graph.
- When documenting an optional native capability, describe both its enabled
  behavior and its disabled-feature behavior. Eggress 1.0.7 SSH is enabled by
  the root `ssh` capability through the stable facade, while no-default builds
  reject SSH configuration before dialing and retain non-SSH proxy support.
- Keep provider transport documentation aligned with the exact Cargo profile:
  Eggfetch 0.1.7 uses `native-http1,tls-rustls`, while the high-level `http1`
  alias and `standard-http1` are intentionally not selected. Preserve the
  historical 0.1.5 measurement as history and record newer footprint evidence
  in a dated architecture subsection.
- Describe configuration transitions through the typed policy in
  `rust/src/config_reload_policy.rs`: mutation paths classify before atomic
  replacement, and server-side rehash reclassifies before generation
  publication. Do not preserve stale caller-specific restart/reload lists.
- Keep historical plans append-only. Git history is the archive for retired
  migration scaffolding; current docs must describe the shipped Rust runtime.
  New plans follow the `plans/` lifecycle in the `plan` skill; do not invent a
  second numbering or status scheme.
- Never document Python-era runtime details as current: no ASGI server, no
  `yield`/`json.dumps`/`_execute_streaming`/`_build_stream_generator` internals,
  no `self._`-style Python fields, no `tests/unit/*.py` paths. The streaming
  path is Rust/Hyper SSE under `rust/src/coordinator/streaming/` + `rust/src/wire/`,
  periodic tasks are `TaskSpec { interval_s, initial_delay_s }` in
  `rust/src/task_supervisor.rs`, and native tests live in `rust/tests/`.
- Document Responses as two bounded paths: canonical semantic adaptation and
  source-native same-surface preservation. State the stateless policy from
  admission (`store` omitted/false is accepted; stateful continuation and
  background features are rejected) and never imply native-only items/tools
  are translated losslessly. For streams, document native Responses
  observe-and-forward behavior separately from bounded cross-surface lifecycle
  synthesis, including authoritative completed output items and strict
  terminal evidence.
- For Codex integration docs, show the generated `[model_providers.eggpool]`
  Responses configuration with WebSockets disabled, the generated
  `model_catalog_json` picker path, an optional explicit model/alias, the
  actual server default port, and the `EGGPOOL_API_KEY` environment contract.
  The generated TOML references `EGGPOOL_API_KEY` and never embeds the
  resolved key; `--print-secret` does not change that Codex behavior. For
  OpenCode V1, document the Responses-capable `@ai-sdk/openai` runtime with
  `{env:EGGPOOL_API_KEY}` interpolation and per-model limits from the same
  projection; for V2, document
  `@opencode/ai/providers/openai-compatible/responses` with
  `env: ["EGGPOOL_API_KEY"]`, shape-first variant selection, JSONC
  preservation, and previous-entry restoration on remove.
  `/v1/models` remains the standard OpenAI schema.
- For remote setup docs, distinguish the listen socket (`[server].host`/`port`)
  from the advertised client URL (`[integrations].advertise_base_url`, `.../v1`,
  live-reloadable). Document `eggpool configremote codex|opencode` token/JSON
  output as secret-free and read-only, and `GET /api/integrations/v1/profile`
  as the authenticated versioned projection. Document `eggpool-connect`
  `plan`/`install`/`verify`/`backups`/`restore`/`remove` with byte-exact
  backups, atomic writes, automatic rollback, and the helper state layout in
  `docs/filesystem-layout.md`. Document the version-pinned desktop bootstrap
  (`eggpool configremote --shell posix|powershell`, `packaging/connect/`
  scripts, SHA-256 against the release SHA256SUMS, `docs/releasing.md`
  helper assets) and always state that a Windows helper does not imply
  Windows proxy support.

## Checks

```bash
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```
