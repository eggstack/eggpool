# Provider Profile Metadata Milestone 001 — Provider Template Endpoint and Source Reconciliation

Status: ready

Repository baseline: `c17a55218b2810791fcf6f3136b8805becfa27c1`

Source roadmap:

- `plans/subsystems/provider-profile-metadata-corrective-roadmap.md#milestone-001--provider-template-endpoint-and-source-reconciliation`

Long-term requirements:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`
- `plans/002-long-term-roadmap.md`

Applicable ADRs:

- None required. This is a bounded metadata/config correctness pass.

Primary class: invariant

## 1. Objective

Bring EggPool's bundled provider-template endpoint/protocol/auth/model-discovery facts back into evidence-backed alignment with current first-party provider documentation, and add regression evidence that makes path-composition drift visible without adding runtime freshness checks.

## 2. Why this milestone is ready

The template catalog and provider/wire runtime are already stable. The trigger is concrete and externally verified: Together's current bundled base URL is stale, while OpenCode Go demonstrates that some existing EggPool values are correct and must not be overwritten from sibling-repository state.

No upstream package/API dependency is required.

## 3. Current implementation evidence

At the baseline:

- `rust/assets/providers/_templates.toml` is the bundled template authority.
- Together is configured with `base_url = "https://api.together.ai/v1"`.
- Current first-party Together examples use `https://api.together.xyz/v1/chat/completions`, implying a base prefix of `https://api.together.xyz/v1`.
- OpenCode Go is configured with `base_url = "https://opencode.ai/zen/go/v1"`.
- Current first-party OpenCode documentation uses that same prefix for Chat Completions, Responses, Anthropic Messages, and model discovery.
- CodeGG currently has the inverse mix: Together is on `.xyz`, OpenCode Go is missing `/zen`. This is comparison evidence only, not authority.
- Existing template/runtime tests do not record a first-party-source disposition for every externally mutable provider fact.

## 4. Invariants that must not regress

- Provider IDs/config keys remain stable.
- Operator-configured/regional endpoints remain configurable where currently allowed.
- Base URL and endpoint path are validated as one composition; do not duplicate `/v1` or omit required prefixes.
- Protocol/wire surfaces and auth headers remain matched to the selected endpoint family.
- Model-discovery behavior stays bounded and secret-safe.
- No live credentialed provider traffic in CI.
- No provider/account routing, quota, health, retry, persistence, or transport ownership change.
- No assumption that CodeGG or any sibling repository is authoritative for provider facts.

## 5. Scope

### In scope

- Audit every bundled entry in `rust/assets/providers/_templates.toml` against current first-party documentation.
- Record a compact review matrix in the closure evidence (or a small committed source-evidence file if implementation proves that is cleaner) with provider, fact class, first-party source, review date, prior value, final value, and disposition.
- Correct Together's base URL to `https://api.together.xyz/v1` unless newer first-party evidence supersedes the reviewed source.
- Retain/qualify OpenCode Go's `https://opencode.ai/zen/go/v1` prefix and its per-wire-surface paths.
- Correct other confirmed endpoint/auth/model-discovery/wire-surface drift found by the audit.
- Add focused static/runtime tests for corrected path composition and template parsing.
- Update provider/catalog architecture documentation to describe review authority.

### Explicitly out of scope

- New provider additions.
- Pricing refresh unless needed to correct a touched provider template's invalid schema.
- Reusable provider-profile crate extraction.
- Cross-repo synchronization machinery.
- Web/network checks in runtime or CI.
- Broad model capability inference.
- Public configuration or storage migration unless a separate plan is registered.

## 6. Required production changes

### Evidence-backed template audit

Create one bounded provider-fact matrix covering all bundled providers. For each template, check:

- base URL and regional/default semantics;
- protocol family/wire surfaces;
- chat/messages/responses path composition where explicitly configured;
- auth header/scheme and required static headers;
- models endpoint/path/shape;
- verification protocol and model only when first-party docs support them;
- operator-configurable vs fixed endpoint semantics.

A value is not corrected merely because another local repository differs.

### Correct confirmed drift

Apply only first-party-supported corrections. Together is the known minimum correction.

For each changed base URL, add a regression that composes the actual request/model path and asserts the final URL, so a future `/v1/v1`, missing prefix, or wrong host cannot hide behind a base-url-only test.

### Documentation and guards

Document the template authority and review process. Prefer focused assertions over a giant golden file that makes unrelated provider updates difficult.

Do not add a test that fetches provider documentation from the network.

## 7. Ordered work packages

### Work package A — Provider-fact inventory

Intent: classify every bundled fact before editing.

Acceptance evidence: complete matrix, first-party source references, explicit uncertain/deferred entries.

### Work package B — Correct and qualify endpoint/path/auth facts

Intent: fix confirmed drift while preserving protocol-specific shapes.

Acceptance evidence: Together correction; OpenCode Go retained/qualified; focused path-composition and auth/wire tests for every changed entry.

### Work package C — Catalog/model-discovery coherence

Intent: ensure touched providers' models endpoint and verification protocol still compose with the corrected base URL.

Acceptance evidence: catalog/provider fixtures parse and exercise corrected discovery paths without live secrets.

### Work package D — Full closure and cross-repo comparison

Intent: finish with one local source of truth and classify sibling differences.

Acceptance evidence: compare shared provider IDs with CodeGG's current setup catalog, but record differences only as informational/intentional/corrective evidence. Do not import CodeGG metadata.

## 8. Failure, cancellation, restart, contention semantics

No new asynchronous/runtime behavior is authorized. Existing catalog refresh, provider request, cancellation, and retry semantics remain unchanged.

## 9. Compatibility and migration

Bundled default corrections affect newly resolved defaults; explicit operator endpoints continue to win according to existing config semantics.

If a default correction could invalidate persisted configuration or alter an account identity, stop and split a migration plan.

## 10. Required tests

At minimum:

- provider template parsing/validation;
- provider config construction for every touched provider;
- exact final URL composition for changed base/path pairs;
- model-discovery URL/path fixtures for touched providers;
- auth/static-header/wire-surface tests where changed;
- existing catalog refresh/provider/wire tests relevant to touched templates;
- no-default check and strict Clippy according to current development guidance.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_profiles -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
```

Also run any focused provider-template/config test target discovered by implementation. Do not claim a live provider test unless one is actually run.

## 12. Documentation updates

- `architecture/deep-dive-catalog.md`
- provider/config deep dive if endpoint authority is described there;
- `plans/subsystems/provider-profile-metadata-corrective-roadmap.md`
- registry and closure record.

## 13. Acceptance criteria

- Together resolves to the current first-party `.xyz` API prefix.
- OpenCode Go retains the current first-party `/zen/go/v1` prefix.
- Every bundled provider template has a reviewed disposition.
- Every changed endpoint has path-composition regression coverage.
- No runtime web/freshness checker or cross-repo metadata dependency is introduced.
- Full required local verification is green with no unresolved medium-or-higher finding.

## 14. Stop conditions

Stop and report if:

- first-party documentation is contradictory or unavailable for a proposed correction;
- a change requires persisted-config migration, provider-ID changes, or routing policy;
- a fix requires provider-specific code outside the existing template/config seams;
- closure would depend on live credentials unavailable to the implementer.

## 15. Closure evidence required

Record:

- implementation commit(s);
- full provider review matrix with first-party sources/review date;
- old/new values for every changed provider fact;
- exact Together/OpenCode Go disposition;
- focused path/auth/discovery test results;
- full/default/no-default verification results actually run;
- CodeGG comparison matrix for shared provider IDs, explicitly labeled non-authoritative;
- residual findings by severity.

## 16. Handoff notes

Provider facts are externally mutable. The implementation should use current first-party documentation at execution time and update the plan only if newer evidence changes a specific factual premise. Do not broaden this milestone into provider-profile crate extraction.
