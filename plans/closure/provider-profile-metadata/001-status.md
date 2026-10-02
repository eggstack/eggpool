# Provider Profile Metadata M001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/provider-profile-metadata/001-provider-template-endpoint-and-source-reconciliation.md`

Source subsystem roadmap:

- `plans/subsystems/provider-profile-metadata-corrective-roadmap.md#milestone-001--provider-template-endpoint-and-source-reconciliation`

Repository baseline reviewed: `58b1e24c8dce4fc099ac6cba17a0804e4f0cc8a5`

Implementation commits or pull requests:

- `805d6f70` — implement provider template endpoint/source reconciliation M001 (5 focused template regression tests + template-authority docs; zero `_templates.toml` diff)

## 1. Executive finding

The milestone is complete. Every bundled provider template in
`rust/assets/providers/_templates.toml` (23 entries) received an
evidence-backed disposition against current first-party documentation
(reviewed 2026-10-02). The resulting local contract is unchanged from the
baseline: no bundled base URL, path, auth, or discovery fact required a
correction, so the production template diff is zero. The value landed is the
regression surface that makes future path-composition drift visible
(`rust/tests/operations_o004.rs`, 5 new tests) plus the documented review
authority (`architecture/deep-dive-providers.md`,
`architecture/deep-dive-catalog.md`).

The plan's Together premise is superseded by repository evidence and is
explicitly not applied: the plan assumed EggPool's
`https://api.together.ai/v1` was stale and should move to
`https://api.together.xyz/v1`. Current canonical first-party documentation
(`docs.together.ai`, including the OpenAI-compatibility reference, the
quickstart, and the OpenAPI `servers` entries) uses
`https://api.together.ai/v1` throughout; the `.xyz` host appears only on the
legacy `togetherai-migration.mintlify.app` page. Per the planning rule that
repository evidence overrides interim plans, Together is retained on `.ai`
and the `.xyz` host is recorded as a legacy alias. CodeGG's matching drift
is the mirror image (CodeGG Together is on legacy `.xyz`; CodeGG OpenCode
Go is missing `/zen`) and is recorded as non-authoritative comparison
evidence only — nothing was imported.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Audit every bundled template against first-party docs | Review matrix in §3, sources + review date per provider | pass | 23/23 dispositioned: 15 confirmed, 6 operator-configurable/local, 2 uncertain-deferred (minimax-cn, alibaba models probing) |
| Together resolves to current first-party prefix | `together_template_retains_first_party_ai_endpoint`; canonical `docs.together.ai` fetch | pass | Retained `.ai`; plan's `.xyz` correction rejected with evidence (§1) |
| OpenCode Go retains `/zen/go/v1` + per-surface paths | `opencode_go_template_retains_zen_go_prefix_and_wire_surfaces` | pass | Base + 3 wire surfaces + discovery composition locked |
| Every changed endpoint has path-composition regression | `touched_provider_configs_construct_and_validate`, `representative_template_path_compositions_stay_exact` | pass | No endpoint changed, so regressions lock the confirmed compositions instead (Together, OpenCode Go, DeepSeek, MiniMax, Alibaba shapes) |
| Template parsing/validation | `bundled_templates_parse_with_stable_ids_and_well_formed_endpoints` (23 IDs, http(s) base, known auth, non-empty protocols) | pass | — |
| Provider config construction for touched providers | `touched_provider_configs_construct_and_validate` via `Config::from_toml_bytes` | pass | Together + OpenCode Go validate end to end |
| Model-discovery coherence without live secrets | Composed `/models` assertions for Together/OpenCode Go; `catalog_refresh` 4/4 | pass | No live provider traffic; fixtures only |
| Auth/wire-surface coverage where changed | Static auth-mode sweep over all templates; per-surface assertions for OpenCode Go; Together bearer | pass | Nothing changed, so coverage is confirmatory |
| Catalog/provider/wire suites relevant to templates | `catalog_refresh`, `wire_profiles`, `wire_runtime`, full workspace | pass | 815/815 workspace; see §4 |
| No-default parity | `cargo check` + `cargo clippy --no-default-features` | pass | Clean; see §4 |
| No runtime freshness checker / cross-repo dependency | Diff review: no new network code, no sibling imports | pass | Zero `_templates.toml` diff; tests parse the committed file only |
| Documentation authority | `deep-dive-providers.md` template-authority section; `deep-dive-catalog.md` bootstrap pointer | pass | States first-party authority + no auto-freshness |

## 3. Production implementation evidence

Landed ownership/storage/protocol/runtime/operator changes: none required
beyond guards and docs. Specifically:

- `rust/assets/providers/_templates.toml`: unchanged (deliberately — every
  fact verified current; see matrix). No provider-ID, key, routing,
  persistence, or transport change.
- `rust/tests/operations_o004.rs` (+313 lines, 5 tests):
  `bundled_templates_parse_with_stable_ids_and_well_formed_endpoints`,
  `together_template_retains_first_party_ai_endpoint`,
  `opencode_go_template_retains_zen_go_prefix_and_wire_surfaces`,
  `touched_provider_configs_construct_and_validate`,
  `representative_template_path_compositions_stay_exact`.
- `architecture/deep-dive-providers.md`: new "Bundled provider-template
  authority" section (owner, first-party authority, 2026-10-02 review
  outcome, composition rule, no freshness checker).
- `architecture/deep-dive-catalog.md`: new "Discovery bootstrap" pointer.

Provider review matrix (review date 2026-10-02 for all; prior value =
final value = bundled value in every row — no corrections applied):

| Provider | Fact class | First-party source | Prior → final | Disposition |
|---|---|---|---|---|
| opencode-go | base `https://opencode.ai/zen/go/v1`; chat/responses/messages surfaces; bearer (+`x-api-key` on anthropic surface); `GET /models` | `opencode.ai/docs/go` | unchanged | confirmed |
| minimax | base `https://api.minimax.io/anthropic`; `/v1/messages`, `/v1/models`; `x-api-key` + `anthropic-version` | `platform.minimax.io/docs` | unchanged | confirmed |
| minimax-cn | base `https://api.minimaxi.com/v1`; `/chat/completions`; bearer | `platform.minimaxi.com/docs` | unchanged | uncertain-deferred: models host (`minimaxi.com` vs `minimax.cn`) needs live verification; template already cautions and sets `require_models = false` |
| openrouter | base `https://openrouter.ai/api/v1`; chat/models; bearer + optional attribution | `openrouter.ai/docs` | unchanged | confirmed |
| ollama-local | `http://localhost:11434/v1`; `/models`; no auth | operator-local runtime | unchanged | intentionally operator-configurable |
| lmstudio-local | `http://localhost:1234/v1`; `/models`; no auth | operator-local runtime | unchanged | intentionally operator-configurable |
| llamacpp-local | `http://localhost:8080/v1`; `/responses`; no auth | operator-local runtime | unchanged | intentionally operator-configurable |
| vllm-local | `http://localhost:8000/v1`; `/responses`; no auth | operator-local runtime | unchanged | intentionally operator-configurable |
| localai-local | `http://localhost:8080/v1`; `/models`; no auth | operator-local runtime | unchanged | intentionally operator-configurable |
| custom-compatible | `http://localhost:8000/v1`; `/models`; no auth | operator template | unchanged | intentionally operator-configurable |
| openai | base `https://api.openai.com/v1`; `/chat/completions`, `/responses`, `/models`; bearer | `developers.openai.com` | unchanged | confirmed |
| anthropic | base `https://api.anthropic.com/v1`; `/messages`; `x-api-key` | `docs.anthropic.com` (+ Bearer now also accepted; `x-api-key` remains valid) | unchanged | confirmed (alternative auth noted, no change) |
| groq | base `https://api.groq.com/openai/v1`; chat/models; bearer | `console.groq.com/docs` | unchanged | confirmed |
| deepinfra | base `https://api.deepinfra.com/v1/openai`; chat/models; bearer | `docs.deepinfra.com` | unchanged | confirmed (bundled `base + /models` matches the `/v1/openai/models` form) |
| gemini | base `.../v1beta/openai`; chat; bearer | `ai.google.dev/gemini-api/docs/openai` | unchanged | confirmed |
| gemini-native | base `.../v1beta`; interactions/generateContent; `x-goog-api-key` | `ai.google.dev/api` | unchanged | confirmed |
| xai | base `https://api.x.ai/v1`; `/chat/completions`, `/models`; bearer | `docs.x.ai` | unchanged | confirmed |
| mistral | base `https://api.mistral.ai/v1`; chat/models; bearer | `docs.mistral.ai` | unchanged | confirmed |
| siliconflow | base `https://api.siliconflow.cn/v1`; chat/models; bearer | `docs.siliconflow.cn` | unchanged | confirmed |
| deepseek | base `https://api.deepseek.com` (no `/v1`, intentional); chat/models; bearer | `api-docs.deepseek.com` | unchanged | confirmed |
| together | base `https://api.together.ai/v1`; `/chat/completions`, `/models`; bearer | `docs.together.ai` (canonical); `.xyz` only on legacy migration page | unchanged (plan's `.xyz` correction rejected) | confirmed |
| fireworks | base `https://api.fireworks.ai/inference/v1`; chat; bearer | `docs.fireworks.ai` | unchanged | confirmed for base/chat/auth; OpenAI-shape `/models` under the inference base is not explicitly shown first-party (low finding, §10) |
| alibaba | base `.../compatible-mode/v1` (Beijing legacy, still available); chat; bearer | `help.aliyun.com/model-studio` (workspace-regional URLs now recommended) | unchanged | confirmed for chat/auth; compatible-mode `/models` probing + regional migration deferred to live verification (low finding, §10) |

CodeGG comparison matrix (shared IDs; explicitly non-authoritative,
informational only — nothing imported):

| Shared ID | EggPool (this repo) | CodeGG (`crates/codegg-providers/src/setup_catalog.rs`) | Classification |
|---|---|---|---|
| openai | `https://api.openai.com/v1` | same | agree |
| groq | `https://api.groq.com/openai/v1` | same | agree |
| deepinfra | `https://api.deepinfra.com/v1/openai` | same | agree |
| mistral | `https://api.mistral.ai/v1` | same | agree |
| xai | `https://api.x.ai/v1` | same | agree |
| minimax | `https://api.minimax.io/anthropic` | same | agree |
| together | `https://api.together.ai/v1` (canonical) | `https://api.together.xyz/v1` (legacy alias) | intentional difference; CodeGG corrective opportunity |
| opencode_go | `https://opencode.ai/zen/go/v1` (correct) | `https://opencode.ai/go/v1` (missing `/zen`) | intentional difference; CodeGG corrective opportunity |
| anthropic | `https://api.anthropic.com/v1` | `https://api.anthropic.com` (bare host; path appended at request) | informational; functionally equivalent composition |
| opencode_zen | no EggPool template | `https://opencode.ai/zen/v1` | informational; no EggPool counterpart in scope |

## 4. Verification executed

All commands run locally in this worktree (serial `--test-threads=1` for
tests); no CI run is claimed.

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --test operations_o004 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_profiles -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
```

### Results

- `cargo fmt --check`: pass (after one `cargo fmt` normalization of the new tests).
- `operations_o004`: 18 passed (13 pre-existing + 5 new).
- `catalog_refresh`: 4 passed.
- `wire_profiles`: 9 passed.
- `wire_runtime`: 8 passed.
- `cargo clippy` (default, all targets): no issues.
- Full workspace `--all-targets`: 815 passed across 65 suites, 0 failed.
- `cargo check --no-default-features`: pass (62 crates).
- `cargo clippy --no-default-features`: no issues.
- No live provider test was run or claimed; no credentials were used.

## 5. Invariant review

Per source-plan §4, each invariant remains true:

- Provider IDs/config keys stable: no template ID touched; test asserts the full 23-ID set.
- Operator/regional endpoints configurable where allowed: locals, `custom-compatible`, and regional notes untouched.
- Base URL + path validated as one composition: new tests compose every surfaced pair and reject `/v1/v1` and missing-prefix shapes.
- Protocol/wire/auth matched per endpoint family: static auth-mode sweep + per-surface assertions; Anthropic Bearer alternative noted without changing the valid `x-api-key` shape.
- Discovery bounded and secret-safe: default/declared `/models` compositions only; no credentials in evidence or tests.
- No live credentialed traffic in CI: no network test added; tests parse the committed file.
- No routing/quota/health/retry/persistence/transport ownership change: zero production Rust diff outside tests/docs.
- No sibling-repository authority: CodeGG used for comparison evidence only (§3 matrix); all dispositions cite first-party sources.

## 6. Failure and recovery review

No new asynchronous or runtime behavior was authorized or added, so there
are no new failure, cancellation, restart, or contention semantics to
review. Existing catalog-refresh (non-destructive on failure), provider
request, cancellation, and retry semantics are unchanged and were
re-qualified by the focused + full suites above. Template misconfiguration
still fails closed at `Config::validate` / `load_provider_templates`, as
exercised by the construction tests.

## 7. Migration and compatibility review

No migration required. Bundled defaults are byte-identical to the
baseline, so newly resolved defaults and persisted operator configurations
behave exactly as before; explicit operator endpoints continue to win per
existing config semantics. No provider-ID change, no storage change, no
public-API change. If a future review corrects a default that could
invalidate persisted configuration, that correction must arrive with its
own separately scoped migration plan.

## 8. Security review

Secret-free throughout: the review matrix stores URLs and document
references only; tests use synthetic placeholders (`example.invalid`,
fixture keys are pre-existing test constants); `redact_key` behavior is
covered by the pre-existing test. Auth shapes were reviewed for correctness
(bearer vs `x-api-key` vs none) without handling any real credential. No
new auth enforcement, privilege, or DoS surface was introduced.

## 9. Documentation and operations

- `architecture/deep-dive-providers.md`: "Bundled provider-template
  authority" section (owner, first-party authority, 2026-10-02 outcome,
  composition rule, no freshness checker).
- `architecture/deep-dive-catalog.md`: "Discovery bootstrap" pointer.
- No operator action required; no diagnostics or recovery instructions
  change. Future template reviews follow the same bounded-corrective shape
  with the matrix recorded in closure evidence.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Fireworks OpenAI-shape `GET .../inference/v1/models` is not explicitly shown in first-party docs (Gateway documents an account-scoped list API instead) | Discovery probe may 404 on a fresh account; refresh is non-destructive so impact is a `Failed` outcome, not data loss | Defer to a future corrective with live verification; do not guess a different path |
| low | Alibaba compatible-mode `GET .../compatible-mode/v1/models` is undocumented (native listing is a separate regional API) and docs now recommend workspace-regional base URLs over legacy Beijing | Same non-destructive discovery-failure shape; regional migration is operator guidance, not a default change | Defer to a future corrective with live verification; legacy URL remains available so no default change is justified now |

No medium-or-higher finding remains. No corrective pass is required: both
lows are explicitly deferred uncertainties, not defects introduced or missed
by this milestone's verification.

## 11. Roadmap disposition

Milestone 001 is closed. The roadmap's completion definition is met: every
bundled template has an evidence-backed disposition, confirmed drift was
zero (the one proposed correction was refuted by canonical evidence and
documented rather than applied), focused path-composition regressions are
landed, and no unresolved medium-or-higher finding remains. No successor
milestone is registered in the provider-profile-metadata roadmap; any
future re-review (including the two low deferred items) requires a new
bounded plan. No blocked work in any other subsystem depended on M001, so
no promotion occurs in this closure pass.

## 12. Registry updates

Applied in the same commit as this record: `plans/registry.md` moves M001
from dependency-ready to recently closed, the subsystem row's current
milestone becomes "M001 closed", and an unblock-audit note records that no
blocked work was promoted. The source roadmap's milestone status table is
updated to `closed` with a pointer to this record, and the implementation
plan's status line is updated to `closed`.
