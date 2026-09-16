# Plan 215: Plan 213 closure pass (Codex/OpenCode portable adapters)

> **Status:** complete
>
> **Completed:** 2026-09-16 — all 14 acceptance criteria implemented, documented, and qualified locally; serial workspace suite + no-default guards + tooling validators green (see evidence).
>
> **Closes:** `plans/213-codex-opencode-portable-client-adapters.md` (original left untouched per append-only rule)
>
> **Baseline:** EggPool `main` at `4441fa6e` (post-212) through this commit
>
> **Scope:** record what Plan 213 implemented, where, and with what evidence. No new behavior in this file.

## Implementation-time client baselines (WS1)

Re-checked 2026-09-16 against live docs (pinned in code comments, not vendored):

- Codex: `$CODEX_HOME/config.toml` user-level config; `wire_api = "responses"` is the only supported value (chat removed Feb 2026); `model_provider` + `model_catalog_json` top-level keys; provider fields (`name`, `base_url`, `env_key`, `wire_api`, `supports_websockets`, …); validation via `codex debug models` (+ `--bundled`) and `codex doctor --json` (developer docs + CLI reference).
- OpenCode V1: `provider` / `npm` / `options` shape, `@ai-sdk/openai` for Responses, `{env:VAR}` interpolation, `opencode models` discovery (`opencode.ai/docs`).
- OpenCode V2: `providers` / `package` / `settings` shape, `env: [...]` credential lists, per-model `limit` / `capabilities` / `variants`, Responses packages including `@opencode/ai/providers/openai-compatible/responses` (`opencode.ai/v2/docs/providers/`, `/models/`). The older cached `@opencode-ai/ai/...` prefix was seen but NOT used; live V2 docs are authoritative (noted at `OPENCODE_V2_RESPONSES_PACKAGE`).
- Qualified client versions remain Codex CLI 0.154.0 / OpenCode 1.18.30 (Plan 206); V2 shape support is docs-qualified with native-verification backstop.

## What landed, by workstream

- WS2 (Codex editing): `toml_edit` evaluated, NOT adopted — the qualified narrow mutator covers the fixture matrix with zero new audit surface/binary impact/MSRV pressure. Hardened instead: table-head/trailer split preserves file footers appended after the managed table (found + fixed during this plan via a failing test).
- WS3 (Codex inspection/drift/removal): `codex_owned_matches()` semantic owned-field comparison; drift gate accepts last-written or already-converged owned state (unrelated edits converge); pre-existing `[model_providers.eggpool]` tables captured (head text) and restored exactly; applied root `model` recorded in the manifest (`applied_model`, additive field).
- WS4 (Codex validation): local layer now asserts `base_url`, `wire_api = "responses"`, `supports_websockets = false`, `env_key`, strict catalog parsing, model-count match, and secret hygiene; native layer unchanged (`codex debug models` + `codex doctor --json`, key forwarded to child only, no inference).
- WS5 (OpenCode variants): explicit V1/V2 renderers from one projection; shape-first selection (`select_opencode_variant`), both-keys ambiguous refuses, empty defaults V1 (2.x major selects V2); never writes both families into one file.
- WS6 (JSONC preservation): `jsonc-parser` evaluated, NOT adopted — new `rust/crates/eggpool-client-config/src/jsonc.rs` token-offset scanner/editor (comments, block comments, trailing commas preserved; bounded line/column errors; no content echo).
- WS7 (OpenCode ownership): exact raw previous-entry capture (`provider.eggpool` / `providers.eggpool`), byte-for-byte restore, safe empty-parent cleanup (comment-bearing parents stay), drift-aware remove.
- WS8 (projection): V1 entries unchanged; V2 entries carry `name`/`limit`/`capabilities` (tools only when exactly known, unknown omitted), no `modelID`/`transport`/cost metadata.
- WS9 (auth/runtime): V1 `{env:EGGPOOL_API_KEY}` + `@ai-sdk/openai`; V2 `env: ["EGGPOOL_API_KEY"]` + `@opencode/ai/providers/openai-compatible/responses` (no chat downgrade). V2 reasoning variants deliberately omitted (no proven mapping; default reasoning unaffected).
- WS10 (paths): same-host `opencode_config_path()` now honors `%APPDATA%` on Windows (pure tested resolver mirroring `eggpool-connect`); `CODEX_HOME`/`OPENCODE_CONFIG`/XDG unchanged; helper state/catalog roots unchanged.
- WS11 (fixtures/matrix): 49 crate unit tests + lifecycle/transaction coverage for empty, comments before/inside/after, line+block comments, trailing commas, unrelated providers/settings, pre-existing entries, external owned/unrelated edits, malformed JSONC (with locations), V1/V2 parity, idempotent re-install, and remove-restore.

## Same-host/helper unification (criterion 12)

`configsetup` (`integrations.rs`) and `eggpool-connect` (`install`/`detect`/`verify`/`main verify`) now call the same portable entry points (`select_opencode_variant`, `expected_opencode_provider_for`, `apply/remove_opencode_document`, `capture_owned_raw`, `current_owned_entry`, `owned_entry_allows_sync`, `looks_like_eggpool_entry`, `codex_owned_matches`, `apply/remove_codex_text_mutation`). `ClientAdapter` plan/remove methods are real implementations (Codex stub removed; `codex doctor --json` fixed).

## Docs updated

`docs/agent-configuration.md` (V1/V2 contracts, JSONC preservation, semantic drift, capture/restore, Windows paths), `architecture/deep-dive-integrations.md` (boundary, V1/V2, lifecycle, dependency-gate section), `architecture/overview.md` (§11, §13), `README.md` (V1/V2 runtimes, portable crate), `AGENTS.md` (layout line), `.opencode/skills/architecture/SKILL.md`, `.opencode/skills/documentation/SKILL.md`. Unreferenced `config-examples/opencode.jsonc` left untouched (out of scope).

## Evidence (all local, 2026-09-16)

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1   # zero failures
cargo test --manifest-path rust/Cargo.toml --no-default-features                            # zero failures
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1      # 14 passed
cargo test --manifest-path rust/Cargo.toml --lib operations::integrations                   # 20 passed
cargo test --manifest-path rust/crates/eggpool-client-config/Cargo.toml                    # 49 passed
cargo test --manifest-path rust/Cargo.toml -p eggpool-connect --test connect_transaction -- --test-threads=1  # 16 passed
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/                                                                     # 0 errors
uv run pytest tests/tooling/ -q --tb=short --maxfail=1                                      # 75 passed, 1 skipped
uv run python scripts/validate_release_docs.py                                              # pass
uv run python scripts/validate_runtime_package_boundary.py                                  # pass
```

Dependency/size verification: no new dependencies (`Cargo.toml`/`Cargo.lock` untouched); `cargo deny` bans/licenses/sources ok — advisories reports one PRE-EXISTING rustls finding (RUSTSEC-2026-0285, locked graph predates this plan; deny is not in the CI `check` job — weekly audit workflow owns it); `cargo tree --duplicates` shows only the pre-existing dev/test duality. No release-binary impact (nothing added).

## Acceptance mapping

1. Schema code lives in `eggpool-client-config`/`integrations.rs`/`eggpool-connect` only — yes.
2. Codex Responses semantics + strict catalog retained — yes (tests).
3. Codex preserves TOML, restores previous values — yes (incl. table + footer).
4. OpenCode JSONC/trailing-comma preservation — yes.
5. Explicit V1/V2 variants, no universal shape — yes.
6. Unknown/unsupported fails closed — yes (ambiguous/invalid/legacy-model paths).
7. Pre-existing entries captured + restored — yes (both clients).
8. V1/V2 Responses-capable packages — yes.
9. Env references only, never embedded keys — yes (asserted).
10. Conservative shared projection — yes.
11. Platform-aware paths + overrides — yes.
12. Shared renderer/mutation policy — yes (same portable entry points).
13. Dependency review + footprint — yes (none added, documented gate).
14. Isolated apply/check/remove smoke — yes (temp-home deterministic suites; live-client matrix belongs to Plan 214).

## Handoff

Upstream schemas remain fast-moving renderer contracts: evidence is pinned in `jsonc.rs`/`opencode.rs` header comments and tests. If Codex/OpenCode change, adapt the client layer only. Live-client qualification (real `codex`/`opencode` binaries, Plan 214) is still the remaining step before workgroup rollout.
