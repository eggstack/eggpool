# Shared Provider Profile Contract Milestone 001 — Neutral Provider Profile Contract and OpenCode Go Surface Hints

Status: implemented

Repository baseline: `b73eea6bc4ecdb6ce6b8974769b7821cf5e7e036`

Closure record: `plans/closure/shared-provider-profile-contract/001-status.md` (implementation `9ac6a131`)

Source roadmap:

- `plans/subsystems/shared-provider-profile-contract-roadmap.md#milestone-001--neutral-provider-profile-contract-and-opencode-go-surface-hints`

Long-term requirements:

- `plans/000-long-term-specification.md`
- `plans/001-terminology-and-domain-model.md`
- `plans/003-planning-process.md`

Applicable ADRs:

- None. Stop and write an ADR if the work would make the new crate semver-stable/publicly versioned, move runtime/account/credential authority out of EggPool, or require the EggPool daemon in sibling consumers.

Primary class: infrastructure

## 1. Objective

Create a neutral, secret-free, sans-I/O `eggpool-provider-profile` crate that owns the reusable provider-profile data contract and canonical embedded profile data currently represented by EggPool's provider templates. Cut EggPool over to the shared source without changing provider/account runtime ownership, and add reviewed non-fixed OpenCode Go model-to-wire hints so downstream consumers can select Chat Completions, Responses, or Messages without guessing.

## 2. Why this milestone is ready

Provider-profile metadata M001 and its planning reconciliation are closed. The current bundled template facts have already been audited against first-party sources, including OpenCode Go's base URL and per-surface auth/path composition.

The request-admission/wire extraction is closed and `eggpool-wire` is already sibling-consumable. The new crate can depend on the neutral wire-surface vocabulary without importing the EggPool root runtime.

Current OpenCode Go documentation reviewed 2026-10-07 publishes an explicit model/endpoint table. The public `https://opencode.ai/zen/go/v1/models` response contains model IDs but no wire-surface field, so downstream consumers need a separate reviewed hint source rather than treating discovery as wire selection.

## 3. Current implementation evidence

- `rust/src/config.rs` defines `ProviderAuthConfig`, `ProviderWireSurfaceConfig`, `ProviderModelsEndpointConfig`, `ProviderVerifyConfig`, `ModelWirePreference`, and the containing `ProviderConfig`.
- `rust/assets/providers/_templates.toml` is the reviewed bundled metadata source.
- OpenCode Go already declares:
  - base `https://opencode.ai/zen/go/v1`;
  - `/chat/completions` + Bearer auth;
  - `/responses` + Bearer auth;
  - `/messages` + `x-api-key`;
  - `/models` discovery through the provider base.
- `rust/src/coordinator/wire_resolver.rs` already supports provider/model wire preferences and can treat non-fixed hints as advisory.
- `eggpool-wire` owns the finite/streaming protocol implementations and intentionally owns no credentials or provider catalog.
- The current `verify` structure does not explicitly distinguish catalog availability from proof that a credential is accepted. That distinction must be represented in the shared profile contract conservatively.

## 4. Invariants that must not regress

- No API-key values, secret refs, account records, proxy credentials, prompt bodies, response bodies, or user identifiers enter the shared crate or embedded profile data.
- EggPool root retains accounts, credentials, routing, quota, health/quarantine, retry, persistence, live catalog refresh, transport, and wire-negotiation ownership.
- `eggpool-wire` remains the codec/semantic authority; the new crate references surfaces but never reimplements payload grammar.
- Existing provider IDs and effective bundled endpoint/auth values remain stable unless current first-party evidence proves a correction.
- Explicit operator configuration continues to override bundled defaults under current semantics.
- Model-wire hints are exact-ID facts from reviewed evidence. Do not create wildcard/prefix guesses.
- A profile with ambiguous wire support and no model hint remains unresolved.
- One canonical embedded provider-profile data source must exist after cutover.
- No runtime network check or filesystem dependency is added.

## 5. Scope

### In scope

- New workspace crate `rust/crates/eggpool-provider-profile`.
- Neutral exported profile types and validation.
- One canonical embedded profile asset/data source consumed by EggPool root and sibling consumers.
- A conservative credential-verification policy that explicitly separates model discovery from authentication proof.
- Current first-party OpenCode Go exact model-to-wire hints, stored as non-fixed preferences.
- EggPool root adapter/cutover and parity tests.
- Consumer fixture proving an external crate can load/inspect profiles without EggPool root dependencies.

### Explicitly out of scope

- HTTP probing or credential verification execution.
- Account configuration, quota, routing, health, retry, persistence, or provider transport extraction.
- Dynamic wire negotiation extraction.
- Provider pricing/model-info enrichment.
- Runtime documentation scraping.
- Publishing the crate or promising semver stability.
- CodeGG changes; those land in the downstream repository after this milestone closes.

## 6. Required production changes

### Shared crate contract

Create a small crate with no `tokio`, HTTP, database, environment, filesystem-at-runtime, logging, or credential dependencies.

The public contract should cover at least:

```text
ProviderProfileRegistry
ProviderProfile
ProviderAuthProfile
ProviderWireSurfaceProfile
ProviderModelsEndpointProfile
ProviderVerificationPolicy
ProviderModelWirePreference
```

Reuse `eggpool_wire::profile::WireSurface` or an equally type-safe one-way adapter; do not introduce a second competing wire-surface vocabulary if a dependency cycle is not created.

Auth profiles contain only structural facts such as mode, header name, and scheme. They never contain credential values.

### Canonical embedded data ownership

Move or otherwise relocate the canonical bundled provider-profile bytes into the shared crate/package boundary so the crate is self-contained for Git and eventual package consumers.

EggPool root must consume that same canonical data through the crate. Do not retain a second independently editable live copy of the provider facts merely to preserve the historical path.

If tests/docs currently require `rust/assets/providers/_templates.toml`, either update them to the new canonical location or leave only a generated/guarded compatibility artifact whose byte identity is mechanically enforced. A manually maintained duplicate is not acceptable.

### Verification semantics

Represent model discovery and credential verification separately.

At minimum support these semantic outcomes/policies:

- no credential required;
- credential can be verified by a known authenticated, non-billable metadata endpoint;
- credential cannot be proven safely at connect time and must remain unverified until real inference;
- explicit probe required only when separately configured/authorized.

Do not infer "credential verified" merely because a static model list exists or because a public `/models` endpoint returned 200.

For provider profiles not yet evidence-qualified for authenticated metadata verification, use the conservative unverified/deferred mode rather than guessing.

OpenCode Go must be marked so its public model catalog is not treated as credential proof.

### OpenCode Go model-wire hints

Using the current first-party OpenCode Go endpoint table as the reviewed evidence source, add exact model-ID preferences for the currently documented models.

Required representative fixtures:

- Responses: `gpt-6-luna`, `gpt-5.6-luna`, `grok-4.7`, `grok-4.6`, and the currently documented Muse contributor IDs.
- Chat Completions: representative GLM/Kimi/DeepSeek/MiMo/Hy/LongCat/Space Bunny IDs that current docs place on `/chat/completions`.
- Anthropic Messages: current MiniMax and Qwen IDs documented on `/messages`.

Use the exact current documentation list at implementation time; the plan intentionally does not freeze this planning-time list as eternal truth.

Hints must be `fixed = false` unless first-party evidence plus EggPool runtime semantics justify a hard pin. The existing wire resolver therefore retains the ability to learn/recover when an upstream contract changes.

Unknown model IDs must have no fabricated hint.

### EggPool root integration

Adapt the existing provider template/config bootstrap to consume the shared profile source. Preserve the existing full `ProviderConfig` runtime shape where root-only fields are still required; use an explicit adapter rather than moving accounts/runtime policy into the shared crate.

Feed the shared model-wire preferences into the current configured preference path without changing resolver ownership.

### Documentation and guards

Update provider/catalog/wire architecture docs to state:

- first-party provider docs are external authority;
- `eggpool-provider-profile` is the local shared secret-free metadata owner;
- `eggpool-wire` remains the wire grammar owner;
- live runtime account/routing/health remain EggPool-owned;
- sibling consumers should pin an immutable revision until publication/stability is separately approved.

Add a static/boundary test that rejects runtime-only dependencies from the new crate and detects a second canonical provider-profile asset.

## 7. Ordered work packages

### Work package A — Extract the neutral schema and package boundary

Create the crate, define typed validation/errors, and establish a consumer fixture. Prove no secret/runtime dependencies are pulled in.

Acceptance evidence: package tests, dependency/boundary guard, consumer fixture compile.

### Work package B — Establish one canonical embedded profile data source

Move/cut over the bundled data and adapt EggPool root loading without changing effective profile values.

Acceptance evidence: old-vs-new normalized profile parity across every bundled provider and stable provider-ID set.

### Work package C — Separate discovery from credential-verification policy

Add the conservative profile policy and map existing provider templates without claiming unverified facts.

Acceptance evidence: fixtures showing a public catalog cannot imply credential verification; OpenCode Go is deferred/unverified.

### Work package D — Add OpenCode Go exact model-wire hints

Record current first-party endpoint evidence and add non-fixed model preferences.

Acceptance evidence: representative surface lookup tests and an exhaustive test over the reviewed OpenCode table captured in the fixture.

### Work package E — Root integration and closure qualification

Run catalog/config/wire-resolver/default/no-default tests, update docs, and record the immutable downstream revision.

## 8. Failure, cancellation, restart, and contention semantics

The shared crate has no async/runtime work, so cancellation/restart/contention behavior is unchanged.

Malformed embedded data must fail deterministically before runtime provider use. A missing/unknown model-wire hint remains an unresolved metadata condition. EggPool's existing wire resolver continues to own runtime negotiation and its singleflight/cooldown behavior.

## 9. Compatibility and migration

No database migration.

Provider IDs and explicit operator config remain stable. The normalized bundled profile projection before and after extraction must compare equal except for the intentionally additive verification-policy fields and non-fixed OpenCode Go model-wire hints.

Do not silently rewrite persisted operator configuration.

## 10. Required tests

Focused unit tests:

- auth/profile validation;
- base URL + path composition invariants;
- verification policy cannot equate public/static catalogs with authenticated credentials;
- OpenCode Go per-surface auth/path;
- exact representative model-wire lookup and unknown-model unresolved behavior.

Integration tests:

- all bundled provider profiles normalize equivalently through the new crate/root adapter;
- existing catalog/template tests remain green;
- existing wire resolver consumes non-fixed OpenCode hints without disabling negotiation.

Security/negative tests:

- malformed header/auth/profile values reject;
- debug/serialized profile contains no secret value fields;
- dependency guard forbids account/credential/environment/HTTP/database imports.

Compatibility tests:

- stable provider ID set;
- existing operator overrides still win;
- no second editable canonical profile asset.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml -p eggpool-provider-profile -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o004 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_profiles -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
git diff --check
```

Adjust exact focused target names only if repository mechanics differ; closure must record what was actually run.

## 12. Documentation updates

- `architecture/deep-dive-providers.md`
- `architecture/deep-dive-catalog.md`
- `architecture/deep-dive-transcoder.md` or the nearest current wire-owner doc if needed
- shared crate README/rustdoc
- planning registry and closure record

## 13. Acceptance criteria

M001 may close only when:

1. A sibling consumer can depend on the new crate without importing EggPool root runtime.
2. One canonical embedded provider-profile data source exists.
3. EggPool itself consumes that source.
4. Existing bundled provider facts retain parity except explicitly documented additive metadata.
5. Discovery and credential-verification semantics are distinct.
6. OpenCode Go's model catalog is not classified as credential proof.
7. Current documented OpenCode Go models have exact non-fixed wire hints with representative exhaustive fixture coverage.
8. Unknown models remain unresolved.
9. No credential/account/runtime I/O enters the crate.
10. Default/no-default and focused qualification are green.
11. Closure records an immutable commit revision suitable for CodeGG pinning.

## 14. Stop conditions

Stop and report if:

- a single shared profile source would require moving account credentials/runtime routing into the crate;
- the crate would need network I/O to be correct;
- provider-profile extraction creates an `eggpool-wire` dependency cycle;
- current first-party OpenCode evidence contradicts the reviewed base/auth/surface contract;
- a bundled-provider change requires a storage/public API migration;
- implementation wants wildcard model-family routing to cover unknown OpenCode models;
- publication/semver promises become necessary for closure.

## 15. Closure evidence required

Create `plans/closure/shared-provider-profile-contract/001-status.md` containing:

- implementation commit(s) and immutable downstream revision;
- normalized provider-profile parity matrix;
- new crate dependency/boundary evidence;
- canonical asset ownership evidence;
- verification-policy disposition, including OpenCode Go;
- reviewed OpenCode Go model-wire table/source date and exhaustive fixture result;
- focused and broad commands/results;
- compatibility/security review;
- unresolved findings by severity;
- unblock audit for the registered CodeGG downstream plan.

## 16. Handoff notes

CodeGG is expected to consume the closure revision directly. Do not optimize this milestone around CodeGG-specific TUI or connection-store types; the shared contract must remain provider-profile metadata only.
