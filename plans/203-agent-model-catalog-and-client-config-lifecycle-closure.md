# Plan 203: Agent model catalog and client config lifecycle closure

> **Status:** complete
>
> **Closes:** `plans/200-agent-model-catalog-and-client-config-lifecycle.md`
>
> **Scope:** local Codex catalog + OpenCode Responses provider from one conservative projection, with idempotent `--apply`/`--sync`/`--check`/`--remove`/`--dry-run` lifecycle. Remote projection endpoint deferred by design.

## What was built

- Provider-neutral `AgentModelCapabilities`/`AgentModelProjection` in `rust/src/operations/integrations.rs`, derived only from validated catalog/model-info facts. Unknown stays unknown, no model-ID inference, `websockets` false, no remote-compaction advertisement, no source-metadata leakage.
- Conservative `aggregate_projections()` for aliases: minimum guaranteed context/output, intersection for booleans/modalities/reasoning efforts.
- Deterministic bounded Codex catalog renderer (`build_codex_catalog_json`) plus `model_catalog_json` TOML support, with `validate_codex_catalog_json` strict-parser checks. Catalog lives under EggPool state (`integrations/codex/eggpool-codex-models.json`); `CODEX_HOME` respected for `config.toml`.
- OpenCode renderer upgraded to Responses-capable `@ai-sdk/openai` with `{env:EGGPOOL_API_KEY}` interpolation, per-model `limit.context`/`limit.output`, `modalities`, and `reasoning`/`variants` from the same projection. Both Codex and OpenCode are now non-secret printable artifacts.
- Managed lifecycle (`codex_lifecycle`, `opencode_lifecycle`) with ownership manifests, atomic writes, idempotent apply/sync, read-only check, safe remove/restore, drift refusal without `--force`, and `--dry-run` diffs. Codex mutation owns only root `model_provider`/`model_catalog_json`/`model` (when requested) plus `[model_providers.eggpool]`, preserving comments. OpenCode merges only `provider.eggpool` and refuses JSONC rewrites silently.
- CLI: `ConfigsetupLifecycleArgs` (`--apply`/`--sync`/`--check`/`--remove`/`--dry-run`) for `codex` and `opencode` only; other targets unchanged. Updated `tests/fixtures/cli/contract-matrix.json`.
- Standard `/v1/models` unchanged. No new server endpoint, no Codex/OpenCode runtime dependency, no `Cargo.toml`/`Cargo.lock` change.

## Evidence

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::integrations
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```

Focused targets all pass locally (`operations_o005` 13 tests, `operations::integrations` 15 tests, `cli_contract`, `codex_responses_compat`). Full serial workspace suite passes. No-default feature guard passes. Python tooling (`ruff`, `pyright`, `pytest`) passes. Release doc/boundary validators pass.

## Docs updated

- `docs/agent-configuration.md`: lifecycle contract, Codex catalog semantics, OpenCode Responses runtime + env key.
- `docs/thinking.md`: OpenCode example corrected to `@ai-sdk/openai`, `{env:EGGPOOL_API_KEY}`, `reasoning` shape.
- `docs/stateless-responses.md`: Codex catalog pointer.
- `README.md`: managed commands, catalog discovery note.
- `architecture/deep-dive-integrations.md` + `architecture/overview.md`: projection, catalog, lifecycle ownership.
- `.opencode/skills/architecture/SKILL.md` + `.opencode/skills/documentation/SKILL.md`: catalog/projection rules.

`AGENTS.md` unchanged (no new module boundary; `operations/integrations.rs` remains the owner).

## Acceptance mapping (Plan 200 criteria 1-16)

1. One provider-neutral projection — done. 2. Conservative alias guarantees — done with intersection tests. 3. Codex loads generated catalog — renderer + strict validation fixture. 4. Accurate/conservative limits/reasoning — done. 5. `wire_api responses`, no websockets, `env_key` — retained and tested. 6. No embedded key in TOML/catalog/manifests — asserted. 7. OpenCode Responses runtime + limits — done. 8-9. Managed lifecycle documented, OpenCode limited to safe merges — done. 10. Idempotent — tested. 11. Drift refusal — tested. 12. Remove restores only owned state — tested. 13. `/v1/models` unchanged — untouched. 14. No remote Codex-schema endpoint — deferred, documented. 15. No name inference — tested. 16. No new runtime dependency — `Cargo.toml` untouched.
