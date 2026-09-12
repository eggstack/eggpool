# Plan 173 — Shared Semantic Model-Routing Crate Roadmap

Date: 2026-09-11
Status: complete (verified 2026-09-12)
Planning baseline: `504c0f7d531e4956a0d4f4cadc39a3623042809c`
Priority: P1 architecture / cross-repository reuse
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Extract EggPool's already-separated semantic model-routing policy into a small reusable Rust crate that EggPool continues to use internally and Codegg can consume without inheriting EggPool's provider/account router, HTTP server, SQLite, Eggress, updater, or application configuration graph.

This is not a routing redesign. Plans 162–167 already established and corrected the semantic-routing behavior. Plans 168–172 already closed the Rust cleanup, dependency audit, and cross-era release/rollback tooling. This line exists only because the current semantic policy boundary in `rust/src/model_router.rs` has become useful outside the EggPool binary.

The intended ownership split is:

```text
Codegg/application policy
        |
        v
shared semantic model-routing crate
  - bounded route definitions
  - validation/compilation
  - deterministic route IDs/policy
  - selector-output validation
  - semantic fingerprinting
  - optional bounded affinity primitives
        |
        +---------------------+
        |                     |
        v                     v
EggPool adapter          Codegg adapter
        |                     |
        v                     v
EggPool provider/account  Codegg session/provider
routing, quota, health,   connection ownership,
retry, claims, catalog    provider execution/UI
```

EggPool remains authoritative for **which provider/account serves a concrete model**. The shared crate may answer only **which configured concrete model a semantic policy resolves to**.

## Findings from the post-review audit

### Work already complete; do not duplicate it

No new rollback/update plan is required. The current neutral release qualification already exercises the Python `0.7.4` -> Rust `0.8.0` -> Python `0.7.4` -> Rust `0.8.0` package cycle for uv-tool, pipx, and pip. Plan 171 explicitly preserves that compatibility contract. Standalone binaries correctly remain unable to synthesize a Python package-manager environment for a cross-era downgrade.

No new dependency-cleanup plan is required. Plan 170 audited direct Rust dependencies/features, removed unused `tower-http` and the no-op Eggress `common` feature, and closed with the remaining graph justified. This roadmap must not reopen dependency shaving except as a property of the new crate itself.

### Current extraction seam

`rust/src/model_router.rs` is already deliberately independent of request, provider, catalog, and selector-client code. It owns compiled routes, registry behavior, deterministic policy bytes, fingerprints, session identity derivation, and process-local bounded affinity. Its main application coupling is `crate::config::{ConfigError, ModelRouterConfig}` plus the Tokio synchronization used by affinity.

The existing `rust/tests/model_router.rs` provides valuable golden behavior: deterministic route ordering, `model-router/v1` policy bytes, fingerprint stability, structural validation, explicit/automatic session hashing, TTL/LRU bounds, single-flight behavior, cancellation recovery, and invalid-selection rejection. Those tests are the behavioral contract for extraction.

### Downstream compatibility constraint

EggPool declares Rust 1.88 / edition 2024. Codegg and its extracted crates currently declare Rust 1.81 / edition 2021. The shared crate must therefore have an independent MSRV and avoid requiring 1.88-only language/library behavior unless Codegg deliberately raises its MSRV in a separate decision.

Target the new crate at Rust 1.81 and edition 2021 if the extracted implementation can do so without semantic compromises. Verify this rather than assuming source compatibility.

## Target crate

Preferred package name: `eggpool-model-routing`.

Preferred repository location:

```text
rust/
  Cargo.toml                 # existing eggpool package + workspace root
  crates/
    eggpool-model-routing/
      Cargo.toml
      src/
        lib.rs
        policy.rs
        compile.rs
        identity.rs
        affinity.rs          # only if kept shared
        error.rs
```

Do not create a generic `eggpool-routing` crate. That name would blur the boundary with the stateful account/provider router in `rust/src/routing/` and invite unrelated code into the dependency.

The existing root package may become a workspace root while remaining the same Maturin package manifest target. Do not move the EggPool binary/package into another directory solely to create the workspace. Cargo supports a root manifest that contains both `[package]` and `[workspace]`; prefer that minimal shape and revalidate Maturin/release tooling afterward.

## Dependency target

The reusable crate should have a deliberately narrow dependency graph.

Required/default core should be approximately:

- `sha2` for the existing stable SHA-256 fingerprints/session digests;
- `thiserror` only if it materially improves the public error contract; a small manual error type is also acceptable.

`serde` should be optional or omitted from the core unless both consumers need direct serialization of the neutral policy types.

Tokio should not be required merely to compile/validate policies. If shared `ModelRouterAffinity` remains async and useful to both consumers, put it behind a narrow optional feature such as `affinity-tokio`, or keep affinity EggPool-side initially and extract only the deterministic policy/identity layer. Choose based on actual Codegg use, not symmetry.

The shared crate must not depend on Axum, Hyper, Tower, Eggress, rusqlite/tokio-rusqlite, TOML, Clap, tracing, provider/catalog types, or EggPool application configuration.

## Neutral public API boundary

Do not expose EggPool's `Config` or `ConfigError` from the shared crate. Define compact neutral input types, for example conceptually:

```text
ModelRouterPolicy
  selector_model
  default_model
  routes
  sticky
  affinity_ttl
  selector_timeout
  max_input_bytes
  repair_attempts

ModelRoutePolicy
  model
  description
```

Names may vary. The important property is that EggPool converts its TOML-facing `ModelRouterConfig` into neutral policy values at the application boundary. Codegg should do the same from its own config/session layer.

The crate should own the validation rules that define the semantic protocol itself: byte bounds, route non-emptiness, default membership, no nested virtual references when given the virtual-ID set, deterministic description normalization, route ordering/IDs, compiled-policy ceiling, semantic fingerprinting, and accepted selector route-ID validation.

EggPool-specific configuration parsing, TOML diagnostics, catalog collision warnings, runtime generation/reload ownership, metrics, selector network dispatch, and provider/account availability remain outside.

## Compatibility contract

Extraction must be behavior-preserving for EggPool. Before moving code, capture golden vectors for at least:

- compiled policy bytes;
- configuration fingerprint;
- route ID ordering;
- valid/invalid UTF-8 byte-bound cases;
- explicit session identity hash;
- automatic identity behavior for large shared prefixes and first-user entropy;
- affinity cache behavior if affinity is extracted.

`SELECTOR_PROTOCOL_VERSION` remains `model-router/v1` unless a deliberate semantic protocol change is separately planned. Moving code is not justification for a protocol-version bump.

## Downstream consumption model

Initial Codegg consumption should use a pinned Git revision or tag from this repository rather than tracking EggPool `main`. The crate can be published to crates.io later if there is a real release/consumer need; publication is not required merely to prove reuse.

A pinned dependency should select the package explicitly, e.g. conceptually:

```toml
eggpool-model-routing = { git = "https://github.com/eggstack/eggpool", rev = "<immutable-sha>" }
```

Do not make Codegg depend on the `eggpool` binary crate itself.

## Phases

### Plan 174 — Extract the neutral core and preserve EggPool behavior

Create the crate/workspace boundary, decouple neutral policy/error types from application config, move deterministic semantic routing logic, wire EggPool through a thin adapter, decide whether async affinity belongs in the crate or behind an optional feature, preserve golden behavior, and verify packaging/release commands still target the EggPool package correctly.

### Plan 175 — Integrate the crate into Codegg without duplicating provider routing

Add the pinned dependency to the appropriate Codegg crate(s), translate Codegg config/session context into the neutral semantic policy, and use the shared compiler/route validation while leaving Codegg provider connections/session selection authoritative. Any selector execution is implemented through Codegg's existing provider abstraction, not EggPool account-routing code.

### Plan 176 — Cross-repo parity, MSRV, packaging, and closure

Prove the same policy vectors compile identically in both repositories, verify Rust 1.81 for the shared crate, verify EggPool's Rust 1.88 build/package/release flow, pin the downstream revision, document ownership/versioning, and close the line without adding a permanent cross-repo CI farm.

Execute 174 -> 175 -> 176. Plan 175 may be implemented in the Codegg repository, but this EggPool plan remains the architectural source for what the shared crate is and is not allowed to own.

## Non-goals

- extracting `rust/src/routing/` provider/account selection;
- sharing EggPool quota, fairness, health, circuit, retry, claim, reservation, catalog, DB, or transport logic;
- changing semantic route behavior established by Plans 162–167;
- introducing cross-model failover after target submission;
- making Codegg silently change durable provider connections;
- replacing Codegg's EggPool `/models` probe;
- publishing a family of micro-crates;
- raising Codegg's MSRV merely for convenience;
- reopening Plans 168–172 cleanup work;
- adding a permanent cross-repository CI matrix or release orchestrator.

## Definition of done

This roadmap is complete when EggPool consumes a small neutral semantic-routing crate without behavior change, Codegg consumes that same crate for the semantic policy layer without inheriting EggPool infrastructure routing, the shared crate is verified at the downstream-compatible MSRV, deterministic policy/fingerprint vectors match across both consumers, and package/update/release behavior remains unchanged.

## Closure evidence

The roadmap is closed. Plan 174 landed the neutral crate and EggPool adapter in
`d70b5963daa373bd16e193637dc2210723418d9c`; Plan 175 integrated Codegg with an
immutable dependency pin at that revision in
`02400b130bf46ab53039355fdd16e5f250028c43`; and the exact shared
policy/fingerprint/identity vector is mirrored downstream in
`881c61720f9a80162094b85c6beeaa68da6af6d0`. Plan 176 added the final parity
coverage and ownership contract in `af58de5e9315ea1d8faee9c05f0021f370cdd534`.

EggPool remains authoritative for provider/account routing, quota, health,
retry, claims, and transport. Codegg owns its sessions and provider
connections. No permanent cross-repository CI workflow or public crate
publication was introduced.
