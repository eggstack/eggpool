# Shared Provider Profile Contract M001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/shared-provider-profile-contract/001-neutral-provider-profile-contract-and-opencode-wire-hints.md`

Source subsystem roadmap:

- `plans/subsystems/shared-provider-profile-contract-roadmap.md#milestone-001--neutral-provider-profile-contract-and-opencode-go-surface-hints`

Repository baseline reviewed: `b73eea6bc4ecdb6ce6b8974769b7821cf5e7e036`

Implementation commits or pull requests:

- `9ac6a131` — implement neutral provider-profile contract and OpenCode Go wire hints (new `eggpool-provider-profile` crate, canonical asset move, EggPool cutover, 30 reviewed hints, guards)

Immutable downstream revision for sibling consumers: **`9ac6a1318e8db3c034b5ab54987317752d5ffea6`**

## 1. Executive finding

The milestone is complete as infrastructure. A secret-free, sans-I/O
`eggpool-provider-profile` crate now owns both the reusable provider-profile
contract and the canonical bundled profile data; EggPool reads that same data
through one adapter and no longer keeps its own copy. Every bundled provider
projects onto a byte-identical runtime `ProviderConfig` before and after the
extraction, so provider/account runtime behavior is unchanged. The two
additive facts the plan authorized landed and are isolated: an explicit
credential-verification policy (conservative and unproven for every
credentialed profile) and 30 exact non-fixed OpenCode Go model-to-wire hints
from the current first-party endpoint table.

This milestone is deliberately not presented as operator-visible capability:
no operator surface changed, no new runtime decision is made from the new
metadata, and CodeGG consumption lands in the downstream repository.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Neutral exported profile contract and validation | `rust/crates/eggpool-provider-profile/src/{lib,profile,verification,surface}.rs`; `tests/contract.rs` (14 tests) | pass | `ProviderProfileRegistry`, `ProviderProfile`, `ProviderAuthProfile`, `ProviderWireSurfaceProfile`, `ProviderModelsEndpointProfile`, `ProviderVerificationPolicy`, `ProviderModelWirePreference` all exported |
| No second wire-surface vocabulary | `src/surface.rs` re-exports `eggpool_wire::profile::WireSurface`; `model_wire` maps are keyed by that type | pass | One-way edge; `eggpool-wire` has no dependency on the new crate, so no cycle |
| No runtime/credential dependency in the crate | `tests/boundary.rs::crate_has_no_runtime_only_dependency`, `crate_sources_import_no_runtime_or_eggpool_module` | pass | `[dependencies]` is exactly `eggpool-wire`, `serde`, `thiserror`, `toml`; no `std::env`/`fs`/`net`/`process`/`time`, no tokio/HTTP/DB/logging |
| Secret-free contract and embedded data | `deny_unknown_fields` parser; `static_headers_reject_credential_headers_with_literal_values`; `secret_reference_fields_are_rejected_by_the_parser`; `embedded_data_has_no_field_that_could_carry_a_secret`; `auth_profiles_carry_structure_and_never_values` (serialized field-name scan) | pass | No `api_key`, `api_key_env`, `value_env`, `secret`, `password`, or `token` key exists in the asset; a literal value on a credential header is rejected at validation |
| One canonical embedded profile data source | `git mv rust/assets/providers/_templates.toml` → `rust/crates/eggpool-provider-profile/assets/_provider_profiles.toml`; `tests/boundary.rs::exactly_one_canonical_provider_profile_asset_exists`; `eggpool_runtime_and_consumer_fixture_are_the_only_profile_consumers` | pass | Guard fails if any other canonical-named asset appears or if the historical path returns |
| EggPool consumes that source | `rust/src/provider_profile.rs` (`BUNDLED_PROVIDER_PROFILES`, `bundled_profiles`, `provider_config_table`); `operations/config_mutation.rs` reads it instead of `include_str!` | pass | `include_str!("../../assets/providers/_templates.toml")` and the const are gone |
| Old-vs-new normalized profile parity for every bundled provider | `rust/tests/operations_o004.rs::shared_profile_parity::shared_profile_projection_matches_the_canonical_document` | pass | 23/23 providers load through `Config::from_toml_bytes` to an identical serialized `ProviderConfig` on both paths |
| Stable provider-ID set | `tests/contract.rs::provider_ids_are_stable_across_the_extraction`; `operations_o004::bundled_templates_parse_with_stable_ids_and_well_formed_endpoints` | pass | 23 ids unchanged from the pre-extraction asset |
| Effective endpoint/auth values unchanged | Same parity test; `operations_o004` composition tests; `tests/contract.rs::derived_surfaces_match_the_runtime_normalization_shape` | pass | Zero base-URL/path/auth/discovery corrections; only additive metadata differs |
| Discovery separated from credential proof | `verification.rs` (`CredentialProof`, `CatalogEvidence`, `CredentialStatus`, `ProviderVerificationPolicy::assess`); `tests/contract.rs::verification_policy_never_equates_a_public_catalog_with_a_credential` | pass | Public/static catalog can never yield `Verified`, even for an evidence-qualified profile |
| OpenCode Go not classified as credential proof | `assets/_provider_profiles.toml` (`models_require_authentication = false`); `tests/opencode_go.rs::opencode_go_is_not_classified_as_credential_proven`; `tests/contract.rs::no_credential_profile_reports_not_applicable` | pass | OpenCode Go policy is `DeferredUntilInference`; catalog evidence `PublicStatic` → `Unverified` |
| Conservative default for non-qualified profiles | `ProviderProfile::verification_policy` → `ProfileVerification::policy_with_credential(None)` | pass | No bundled profile claims `AuthenticatedMetadata`; no guessing |
| Exact non-fixed OpenCode Go hints | 30 `[providers.opencode-go.model_wire.*]` entries; `tests/opencode_go.rs::reviewed_table_is_covered_exhaustively_by_non_fixed_hints` | pass | Exhaustive in both directions against the reviewed table; every hint `fixed = false` |
| Required representative fixtures | `per_surface_authentication_matches_first_party_documentation`, `representative_lookups_return_the_documented_surface`, `base_url_and_per_surface_paths_match_first_party_documentation` | pass | Responses (luna/grok/muse), Chat (GLM/Kimi/DeepSeek/MiMo/Hy/LongCat/Space Bunny), Messages (MiniMax/Qwen) |
| Unknown models remain unresolved | `unknown_models_and_unknown_providers_stay_unresolved`; `ambiguous_support_without_a_hint_is_not_defaulted_to_chat_completions`; `a_profile_without_a_hint_leaves_the_runtime_preference_map_empty` | pass | No prefix/family guessing; unknown provider ids also resolve nothing |
| Hints reach the runtime as advisory preferences | `coordinator_boundaries.rs::bundled_model_wire_hints_resolve_as_advisory_preferences`; `operations_o004::opencode_go_hints_reach_the_runtime_configuration_as_preferences` | pass | Documented surface sorts first; fallbacks retained; resolver ownership unchanged |
| Sibling consumer can depend without EggPool runtime | `rust/crates/eggpool-provider-profile/consumer-fixture/` (own `[workspace]`, single path dependency) | pass | `cargo check` on the fixture compiles standalone; exercises parse, endpoint resolution, per-surface auth, verification policy, exact hint lookup |
| Operator configuration still wins | `operations_o004::shared_profile_parity::explicit_operator_configuration_overrides_bundled_defaults` | pass | Operator base URL and preferences survive the cutover |
| Existing catalog/wire tests remain green | `catalog_refresh` (7), `wire_profiles` (9), `wire_runtime` (8), `build_manifest` (4), `coordinator_boundaries` (6) | pass | Unmodified except the one new resolver test |
| Docs updated | `architecture/deep-dive-providers.md`, `deep-dive-catalog.md`, `deep-dive-transcoder.md`, `architecture/README.md`, `architecture/overview.md`, `AGENTS.md` | pass | Owner boundaries and first-party authority stated explicitly |

## 3. Production implementation evidence

**New crate `rust/crates/eggpool-provider-profile/`**

- `src/lib.rs` — `ProviderProfileRegistry` (`embedded`, `from_toml`, `require`,
  `model_wire_preference`), `ProviderProfileError`, and the canonical document
  const `EMBEDDED_PROVIDER_PROFILES_TOML`.
- `src/profile.rs` — profile/auth/surface/endpoint/hint/static-model types plus
  `validate_profile`, `validate_path`, `valid_header_name`, `compose_url`.
  Resolution helpers (`resolved_wire_surfaces`, `surface_path_template`,
  `resolved_models_endpoint`) mirror EggPool's configuration normalization,
  including the `/chat/completions` and `/messages` protocol defaults.
- `src/verification.rs` — the discovery-vs-credential-proof contract.
- `src/surface.rs` — the one-way `eggpool-wire` surface re-export.
- `assets/_provider_profiles.toml` — the single canonical editable asset.
- `consumer-fixture/` — compile-only sibling-consumer package.
- `tests/{contract,opencode_go,boundary}.rs` — 26 integration tests.

**EggPool root**

- `rust/src/provider_profile.rs` (new) — the only EggPool reader: canonical
  text, once-validated registry access, `provider_config_table` projection,
  `canonical_config_document()` (pre-extraction baseline) and
  `projected_config_document()` (shared-contract projection),
  `bundled_model_wire_hints()`.
- `rust/src/operations/config_mutation.rs` — bundled templates now come from the
  shared crate; the local `include_str!` and its const are deleted.
- `rust/src/lib.rs` — `pub mod provider_profile`.
- `rust/assets/runtime-manifest.json` — the hash-locked asset entry now points
  at the crate-owned source with a refreshed digest (`09eb9e3b…`). The installed
  `path` and `category` are unchanged so the manifest contract stays stable.

**Data changes** (the only edits to bundled provider facts)

- Removed 23 `api_key_env = "API_KEY"` placeholder lines. These were already
  stripped by `load_provider_templates` and the connect flow derives the
  environment variable name from the provider id, so no effective value
  changed; the shared asset simply cannot express a secret reference at all.
- Added `[providers.opencode-go.verify].models_require_authentication = false`
  (shared-contract only; stripped before the runtime configuration projection).
- Added 30 `[providers.opencode-go.model_wire.*]` non-fixed hints.

No account, credential, routing, quota, health, retry, persistence, catalog
refresh, transport, or negotiation code changed.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml -p eggpool-provider-profile -- --test-threads=1
cargo check --manifest-path rust/crates/eggpool-provider-profile/consumer-fixture/Cargo.toml
cargo test --manifest-path rust/Cargo.toml --test operations_o004 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_profiles -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test build_manifest -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

### Results

All commands ran locally on the implementation commit; nothing is quoted from CI.

| Command | Result |
|---|---|
| `fmt --check` | clean |
| `clippy` (default features) | clean, `-D warnings`, no allowlist |
| `clippy --no-default-features` | clean |
| `check --no-default-features` | clean |
| `cargo test -p eggpool-provider-profile` | 27 passed, 0 failed (contract 14, opencode_go 8, boundary 4, doctest 1) |
| consumer fixture `cargo check` | compiles standalone with a single path dependency |
| `operations_o004` | 24 passed (18 pre-existing + 6 new parity/contract tests) |
| `catalog_refresh` | 7 passed |
| `wire_profiles` | 9 passed |
| `wire_runtime` | 8 passed |
| `build_manifest` | 4 passed |
| `coordinator_boundaries` | 6 passed (5 pre-existing + 1 new advisory-hint test) |
| workspace serial suite | **930 passed, 0 failed, 1 ignored** |
| `cargo deny check` | advisories ok, bans ok, licenses ok, sources ok |
| `uv sync --frozen` | 9 packages checked |
| ruff format / ruff check | 59 files already formatted; all checks passed |
| pyright | 0 errors, 0 warnings |
| pytest tooling | 166 passed, 1 skipped |
| `git diff --check` | clean |

The single ignored test and the skipped tooling test are pre-existing and
unrelated to this change.

## 5. Invariant review

| Source-plan invariant | Evidence it still holds |
|---|---|
| No API keys, secret refs, account records, proxy credentials, bodies, or user identifiers in the shared crate or embedded data | `deny_unknown_fields` rejects `api_key`/`api_key_env`/`value_env`/`unknown_key` documents; key-name scan of the parsed asset; recursive serialized-field scan; credential-header literal values rejected at validation; dependency guard forbids env/HTTP/DB imports |
| EggPool retains accounts, credentials, routing, quota, health/quarantine, retry, persistence, live catalog refresh, transport, and wire negotiation | No production change outside `provider_profile.rs`, one loader line, `lib.rs`, and the asset/manifest; `cargo deny` dependency set unchanged for the root crate except the new path crate |
| `eggpool-wire` remains codec/semantic authority | The new crate depends on it one-way and adds no codec, dialect, or payload grammar; `crate_has_no_runtime_only_dependency` pins the dependency set |
| Existing provider IDs and effective bundled endpoint/auth values stable | 23/23 provider projections identical; stable-ID test; composition tests unchanged |
| Explicit operator configuration overrides bundled defaults | `explicit_operator_configuration_overrides_bundled_defaults` |
| Model-wire hints are exact-ID reviewed facts; no wildcard/prefix guesses | Lookup is a `BTreeMap` hit on the exact id; unresolved-ids test; no pattern matching exists in the lookup path |
| Ambiguous wire support without a hint stays unresolved | `ambiguous_support_without_a_hint_is_not_defaulted_to_chat_completions`; `together` projects an empty preference map |
| One canonical embedded profile data source | Single-asset guard + historical-path guard + runtime-manifest hash lock |
| No runtime network check or filesystem dependency added | Boundary/import guard; the crate's only I/O is compile-time `include_str!` |

## 6. Failure and recovery review

- **Malformed embedded data** — `ProviderProfileRegistry::embedded` is a pure
  function that fails closed with a typed `ProviderProfileError`; the five
  malformed-document cases in `malformed_documents_fail_closed` cover id/key
  mismatch, malformed base URL, empty protocol list, a hint naming an unserved
  surface, and an unknown field. EggPool's loader surfaces the same bytes as
  before, so a corrupt asset fails at build/test time rather than at dispatch.
- **Missing hint** — an unresolved model is an ordinary lookup miss. It cannot
  fall back to Chat Completions through the profile path, and the resolver only
  reorders candidates it is given.
- **Cancellation/restart/contention** — unchanged; the crate has no async,
  clock, state, or I/O. Runtime generation leases, negotiation singleflight,
  and rejection TTL are untouched (covered by the unchanged resolver and
  runtime suites).
- **Duplicate delivery** — the asset is content-addressed by the runtime
  manifest digest, so drift is a `build_manifest` failure rather than a silent
  divergence.

## 7. Migration and compatibility review

- No database migration; no schema, storage, or public API change.
- No persisted operator configuration is rewritten. `connect` renders the same
  provider block shape it did before, because the shared projection reproduces
  the canonical table minus presentation-only keys.
- The only intended deltas are additive and documented in §3: 30 non-fixed
  OpenCode Go hints and the verification-policy field. `models_require_authentication`
  is shared-contract metadata and is stripped by
  `provider_profile::canonical_config_document`/`provider_config_table`, so
  `ProviderVerifyConfig` keeps its current schema.
- The runtime `ProviderConfig` schema is unchanged, so configuration files
  written before this milestone keep loading unchanged.
- The historical asset path `rust/assets/providers/_templates.toml` no longer
  exists; a guard fails if it returns, and the manifest still hash-locks the
  same bytes under the crate-owned source path.

## 8. Security review

- **Secret handling** — the contract has no field for a credential value or a
  secret reference. Auth entries are mode/header/scheme only. The one literal
  header value in the data (`anthropic-version = 2023-06-01` for `minimax`) is a
  public protocol constant, and the validator rejects any literal value on a
  credential-named header, so that hole cannot be reused for a key.
- **Serialization** — `auth_profiles_carry_structure_and_never_values` asserts
  the exact field-name set of a serialized auth profile and scans the full
  serialized profile for credential-bearing field names.
- **Privilege/DoS bounds** — parsing is bounded by the compile-time asset size;
  `ProviderProfileRegistry::from_toml` accepts caller-supplied text with no
  unbounded recursion beyond TOML nesting, and validation rejects oversized
  path/header shapes before use.
- **Audit behavior** — unchanged; no new diagnostics path was introduced.
- **Repository hygiene** — no `.env`, key, or account material was added; the
  consumer-fixture build artifacts (`target/`, fixture `Cargo.lock`) were removed
  rather than committed.

## 9. Documentation and operations

- `architecture/deep-dive-providers.md` — rewrote "Bundled provider-template
  authority" as "Bundled provider-profile authority": crate ownership, the
  single reader, secret-free/sans-I/O boundary, first-party documentation as
  external authority, review-date history, plus new "Model-to-wire hints" and
  "Discovery is not credential proof" subsections.
- `architecture/deep-dive-catalog.md` — discovery bootstrap now names the crate
  asset and states that discovery output is not credential proof.
- `architecture/deep-dive-transcoder.md` — new "Who states which surface a model
  uses" section separating wire grammar ownership (`eggpool-wire`) from
  per-provider preference metadata (`eggpool-provider-profile`) and resolver
  ownership (`rust/src/coordinator/wire_resolver.rs`).
- `architecture/README.md`, `architecture/overview.md` — crate listed in the
  shared-boundary narrative and the review index.
- `AGENTS.md` — crate added to the reusable-crate list; new "Conventions agents
  miss" bullet forbids re-adding a bundled profile asset or a secret reference
  into profile data, and pins non-fixed hints.
- Crate rustdoc (`src/lib.rs`) documents the boundaries, usage, and the
  authority model, and is exercised by a doctest. Sibling crates in this
  repository carry no separate README, so the rustdoc is the crate's documented
  contract; no new doc convention was introduced.
- No operator command, diagnostic, or recovery procedure changed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | `rust/crates/eggpool-wire/assets/_wire_profiles.toml` still carries its own OpenCode Go `[[hints]]` rows (7 entries, `verified_on = "2026-09-02"`, `source = "provider_docs"`), including `minimax-m2.5`, which the 2026-10-07 first-party endpoint table no longer documents. | Two owned registries can disagree in appearance. No runtime path calls `WireProfileRegistry::hints()` — selection reads only the canonical profile hints — so this is documentation-level drift, not a dispatch divergence. | Optional: a future bounded pass reconciles that registry against the canonical profile hints. Not a defect in this milestone — the shared contract is the provider-profile authority, and no code path derives provider facts from it. |
| low | `ProviderProfile.runtime_capabilities` carries bundled `model_capabilities` blocks verbatim as an opaque passthrough. | The shared contract stores runtime policy it does not interpret; a future reader might expect semantics from a typed field. | Optional: split runtime capability policy into a runtime-owned overlay if that data is ever revised. The passthrough is required for lossless parity today and is documented as non-interpreted. |
| low | `CredentialProof::AuthenticatedMetadata` has no bundled profile using it. | The policy variant is contract vocabulary only; no first-party evidence currently qualifies a bundled endpoint for metadata verification. | Revisit only with first-party evidence that a specific metadata endpoint requires the credential. |

No critical, high, or medium findings remain.

## 11. Roadmap disposition

Milestone 001 is **closed**, and with it the
`shared-provider-profile-contract` roadmap: its only milestone met the roadmap's
completion definition (accepted closure record, single metadata copy consumed by
EggPool, reviewed non-fixed OpenCode Go hints present, immutable revision
`9ac6a1318e8db3c034b5ab54987317752d5ffea6` available to siblings). No further
milestone is registered; new provider-profile work requires a new bounded plan.

## 12. Registry updates

Applied in the same commit as this record:

- `plans/subsystems/shared-provider-profile-contract-roadmap.md` — roadmap
  status `closed`; M001 status `implemented (closed)`; milestone status table
  row records the closure record and removes the open blocker.
- `plans/implementation/shared-provider-profile-contract/001-neutral-provider-profile-contract-and-opencode-wire-hints.md`
  — status `implemented`.
- `plans/registry.md` — the roadmap row moves to `closed`; M001 leaves the
  dependency-ready table; a "Recently closed" row records the disposition and
  evidence.

## 13. Unblock audit

Searched `plans/implementation/`, `plans/subsystems/`, and the registry's
dependency-ready, active, and blocked tables for work gated on this milestone.

- **CodeGG (downstream repository)** — this was the only gated consumer, and it
  was recorded as an operational dependency rather than a registered plan. It is
  unblocked by the immutable revision `9ac6a1318e8db3c034b5ab54987317752d5ffea6`;
  CodeGG should pin that revision (or a later one) rather than track `main`,
  since the crate is unpublished and carries no semver promise.
- **In-repository plans** — none. No registered plan listed shared provider
  profile contract M001 as a hard, interface, or operational dependency, and the
  blocked-work table has no entry for this subsystem. Nothing is promoted.
- **Roadmap successors** — none registered; the roadmap closes terminal.

Sibling consumers must not treat CodeGG's product catalog, or any other sibling
repository, as first-party provider authority: bundled profile facts are
reviewed transcriptions of current provider documentation and are re-reviewed by
bounded corrective passes.