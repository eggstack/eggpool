# Plan 174 — Extract the Shared Model-Routing Core

Date: 2026-09-11
Status: complete (verified 2026-09-12)
Parent roadmap: `plans/173-shared-model-routing-crate-roadmap.md`
Planning baseline: `c2154cdbb0767ff0daf128da675cbd030719d616`
Priority: P1 architecture / behavior-preserving extraction
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Objective

Extract the deterministic semantic-routing state currently concentrated in `rust/src/model_router.rs` into `rust/crates/eggpool-model-routing` while preserving EggPool's externally observable behavior and keeping all provider/account routing in the EggPool application.

This is an extraction, not a feature expansion. The exact semantic behavior established by Plans 162–167 is the compatibility target.

## 1. Establish the minimal Cargo workspace

Keep `rust/Cargo.toml` as the EggPool package manifest used by Maturin. Add the smallest workspace declaration that includes the existing root package and the new crate. Do not relocate the EggPool package directory.

Preferred shape:

```text
rust/Cargo.toml
rust/crates/eggpool-model-routing/Cargo.toml
rust/crates/eggpool-model-routing/src/lib.rs
```

The new crate should use edition 2021 and `rust-version = "1.81"` unless implementation evidence shows a real incompatibility. The EggPool root remains edition 2024 / Rust 1.88.

After the workspace edit, immediately prove that these still resolve the intended root EggPool package:

```bash
cargo metadata --manifest-path rust/Cargo.toml --format-version 1
cargo build --manifest-path rust/Cargo.toml --locked --release
```

Also verify the current Maturin/native-wheel tooling still uses `rust/Cargo.toml` as the EggPool binary manifest. Do not alter package identity or version authority.

## 2. Introduce neutral policy/error types

The shared crate must not import `crate::config::{ConfigError, ModelRouterConfig}`.

Create neutral input types representing only semantic routing policy. Preserve all current fields needed to generate the same compiled result. The shared validation error must be independent of EggPool config-file paths/TOML parsing.

EggPool keeps its existing TOML-facing `ModelRouterConfig` and `ModelRouteConfig`. Add a narrow conversion/adaptation function in EggPool that maps those application config values into the shared policy types and maps shared validation errors back into `ConfigError::Validation` at the config boundary.

Do not move the entire EggPool config module into the crate and do not make the shared crate parse TOML.

## 3. Move deterministic compilation and registry semantics

Move the following ownership into the shared crate with equivalent names or clearly mapped replacements:

- route/policy structural validation;
- virtual-model/reference/label/description byte bounds;
- no-empty-route/default-membership checks;
- nested virtual-reference prohibition when validating a mapping;
- deterministic route-label ordering and compact route IDs;
- description normalization;
- `SELECTOR_PROTOCOL_VERSION`;
- compiled static policy bytes and size ceiling;
- semantic configuration fingerprint;
- compiled route lookup / contains-model helpers;
- immutable registry of compiled virtual routes.

Preserve the current golden output exactly. In particular, existing fixtures expecting:

```text
model-router/v1|choose id;reply id only|...
```

and existing SHA-256 fingerprints must continue to pass after extraction.

Do not include catalog availability or provider health in shared validation.

## 4. Extract identity primitives independently of HTTP

The explicit session identity and automatic bounded conversation-prefix identity are semantically reusable and have no EggPool provider dependency. Move them to the crate if they compile cleanly at Rust 1.81.

Preserve:

- `AFFINITY_SESSION_HEADER_MAX_BYTES`;
- `AUTOMATIC_PREFIX_MAX_BYTES`;
- reserved first-user entropy behavior;
- SHA-256 hashing before identity leaves the boundary;
- no raw session value in Debug output;
- surface-specific conservative behavior, including the existing Responses behavior.

The shared API should accept neutral text fragments/surface identifiers, not Axum headers or EggPool request structs. EggPool continues extracting those values from its request surfaces.

## 5. Decide affinity ownership deliberately

`ModelRouterAffinity` currently contains bounded TTL/LRU storage and Tokio `watch`-based single-flight/cancellation recovery. Do not automatically move all of it merely because it is in the same source file.

Evaluate two options:

A. Preferred if Codegg will use identical sticky-selection semantics: place affinity behind an `affinity-tokio` feature in the shared crate. Default/core compilation must remain usable without Tokio.

B. Preferred if downstream use is currently only policy compilation: leave the async cache implementation in EggPool and move only the neutral identity/selection types. The EggPool cache can consume shared compiled routers.

Choose the smaller public API. A feature that only EggPool uses provides little benefit.

Whichever option is selected, retain existing EggPool TTL/LRU, single-flight, cancellation, invalid-selection and statistics behavior. Do not replace it with DashMap, an external cache, persistence, or background cleanup.

## 6. Keep selector execution application-owned

Do not move selector HTTP/provider execution into the crate. EggPool's selector calls must continue through the existing concrete request/coordinator path so usage, quotas, health, retries and accounting remain normal EggPool behavior.

The shared crate may provide a tiny pure function to validate/resolve an exact route ID returned by a selector. It must not know about response status codes, request bodies, providers, protocols, or retries.

Prompt semantic extraction from EggPool request formats should remain application-owned unless a source file is already fully neutral. Static policy construction belongs in the crate; request-surface canonicalization does not need to be shared for this plan.

## 7. Rewire EggPool with a compatibility adapter

Update EggPool imports so application modules consume the extracted types through the new dependency. Prefer a small compatibility/re-export module only if it materially reduces churn; do not keep a duplicate implementation in `rust/src/model_router.rs` after migration.

The EggPool config validation path must still reject the same invalid files with bounded human-readable diagnostics. Exact wording may vary only where tests/documentation do not define it as contract.

The runtime registry, live rehash behavior, virtual `/v1/models` behavior, selector/default flow, metrics and request handling must remain unchanged.

## 8. Migrate tests to the correct ownership level

Move pure/golden tests into the crate where possible:

- policy byte/fingerprint golden vector;
- deterministic route ordering;
- structural validation and UTF-8 byte limits;
- exact route-ID lookup/rejection;
- explicit/automatic session identity vectors;
- affinity unit tests if affinity is extracted.

Retain EggPool integration tests for:

- config -> shared policy conversion;
- invalid config rejection;
- runtime registry publication/rehash;
- selector execution/default behavior;
- virtual model request flow;
- provider/account router non-interference.

Do not weaken coverage by merely moving tests. The current `rust/tests/model_router.rs` is the minimum behavioral inventory.

## 9. Dependency and MSRV verification

The shared crate must have a visibly small `cargo tree`. It must not pull in EggPool application dependencies.

Run, at minimum:

```bash
cargo tree --manifest-path rust/crates/eggpool-model-routing/Cargo.toml
cargo check --manifest-path rust/crates/eggpool-model-routing/Cargo.toml
cargo test --manifest-path rust/crates/eggpool-model-routing/Cargo.toml
```

Verify with a Rust 1.81 toolchain before marking the crate downstream-compatible. If a dependency's current release has a higher MSRV, pin an appropriate compatible version only when safe and documented; do not lower Codegg's MSRV silently.

Then run EggPool's normal strict gates:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
```

If workspace-wide commands alter established CI behavior materially, update the existing bounded job rather than adding another workflow.

## 10. Packaging and release regression checks

Because `rust/Cargo.toml` becomes a workspace root, explicitly re-run current package-boundary/release validators and a local native-wheel build or equivalent dry-run used by the repository.

Required invariant: Maturin still builds package `eggpool` from the root package, not `eggpool-model-routing`; PyPI package name/version and installer/updater behavior remain unchanged.

The shared crate does not need crates.io publication in this phase.

## Acceptance criteria

- `eggpool-model-routing` exists as a small independent crate.
- It compiles at Rust 1.81 without forcing EggPool to lower its own 1.88 baseline.
- EggPool config types are adapted into neutral crate policy types; the crate does not import EggPool config/TOML/application modules.
- Existing `model-router/v1` static policy bytes and fingerprint goldens are unchanged.
- Structural validation, route ordering, identity hashing and any extracted affinity behavior remain equivalent.
- `rust/src/routing/` provider/account routing is untouched except for incidental import formatting if unavoidable.
- The crate has no Axum/Hyper/Tower/Eggress/SQLite/Clap/TOML/provider/catalog dependency.
- EggPool selector execution still uses the existing coordinator and accounting lifecycle.
- Root Maturin/package/release behavior is unchanged after workspace introduction.
- Strict workspace Clippy/tests and current release/package validators pass.

## Definition of done

The extraction is done when EggPool is itself a consumer of the neutral crate, no duplicate semantic-policy implementation remains in the application, the new crate is independently testable at Rust 1.81 with a small dependency graph, and EggPool's model-routing/request/provider behavior and package/update surface are unchanged.

## Execution record

Completed on 2026-09-12. The neutral policy compiler, registry, route-ID
validation, deterministic policy/fingerprint generation, and explicit/automatic
identity primitives now live in `rust/crates/eggpool-model-routing`; EggPool
adapts TOML config into those types and retains its process-owned async affinity
cache and all provider/account routing.

Verification included Rust 1.81 standalone compilation/tests, strict workspace
format/Clippy/tests (470 Rust tests), locked release build, Maturin wheel
inspection, release/package validators, Ruff, Pyright, and tooling tests (75
passed, 1 skipped).

Closure revalidation added the canonical invalid-default, nested-target, exact
UTF-8 boundary, long-first-user identity, and downstream-mirrored policy/
fingerprint/identity vectors. The implementation and closure-test commits are
`d70b5963daa373bd16e193637dc2210723418d9c` and
`af58de5e9315ea1d8faee9c05f0021f370cdd534`.
