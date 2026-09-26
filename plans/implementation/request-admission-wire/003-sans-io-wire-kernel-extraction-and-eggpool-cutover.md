# Request Admission and Wire Milestone 003 — Sans-I/O Wire-Kernel Extraction and EggPool Cutover

Status: ready

Repository baseline: `61470ef788e287c49b4a51062eaba439049d46dc`

Source roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-003--sans-io-wire-kernel-extraction-and-eggpool-cutover`
- `plans/subsystems/request-admission-wire-roadmap.md#13-wire-kernel-extraction-extension`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#4-protocol-and-compatibility`
- `plans/001-terminology-and-domain-model.md`
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining`

Applicable ADRs:

- None required if M002 closes with the planned ownership boundary and `rust/src/wire/ir.rs` remains EggPool's canonical facade. If the implementation needs the reusable crate to own EggPool runtime/admission/config/routing decisions or changes the canonical semantic contract, stop and write an ADR.

Primary class: infrastructure

## 1. Objective

Move the M002-qualified pure wire kernel into one internal Rust workspace crate and cut EggPool over to that single implementation, preserving all current request, response, adaptation, streaming, native preservation, Codex compatibility, runtime, and provider capabilities.

The crate is an internal extraction first. It MUST remain `publish = false` until this milestone closes; EggPool must not depend on crates.io or another repository to build or preserve behavior.

## 2. Why this milestone is ready

Blocked on the hard dependency: request-admission-wire M002 must be closed with a clean dependency seam and compatibility corpus. Once M002 is closed, no external API or service is required.

Do not begin the source move while M002 is only partially complete.

## 3. Current implementation evidence

The baseline wire implementation spans `rust/src/wire/{ir,adaptation,codec,codecs,additional_codecs,registry,stream,runtime}.rs` plus canonical decoding/native preservation pieces in `rust/src/request/admission.rs`.

M002 is expected to leave an explicitly extractable subset with no root-runtime imports. M003 must use that qualified subset rather than rediscovering or broadening the boundary.

`wire/runtime.rs` currently owns EggPool joining logic: selected profile/context, request admission/error mapping, native preservation decisions, body encoding limits, compact preparation, finite response conversion, and stream runtime objects. It stays in the root package.

## 4. Invariants that must not regress

All M002 invariants remain binding, plus:

- There is exactly one implementation of canonical types, adaptation policy, finite codecs, and pure stream state machines after cutover.
- Root compatibility modules/re-exports may preserve existing internal paths, but must not retain copy-pasted implementations that can diverge.
- EggPool's canonical request identity remains type-identical across admission, routing, runtime, codecs, and tests; no JSON serialize/deserialize bridge may be added merely to cross the crate boundary.
- Native Responses finite forwarding remains on the current native-preservation path; translated targets still consult preservation blockers/notices first.
- Native Responses streaming remains source-byte forwarding plus shared decoder observation; the extracted crate must not force buffering or translated re-encoding.
- `WireRuntime`, generation/request ownership, profile selection, compaction execution, routing/catalog facts, config reload, retries, provider transport, publication/finalization, and HTTP status mapping remain root-owned.
- The crate has no credential, environment, filesystem, network, clock, random, async runtime, database, or logging side effects.
- MSRV remains Rust 1.89 or the repository's then-current explicit MSRV; the extraction must not raise it independently.
- `unsafe_code = "forbid"` and repository dependency/license policy apply.

## 5. Scope

### In scope

Create a workspace crate under `rust/crates/` (provisional package name `eggpool-wire`) that owns the qualified neutral modules from M002, expected to include:

- canonical request/response/event IR and supporting types;
- canonical structural decoder + neutral decode limits/policy inputs established by M002;
- bounded native feature/provenance summary vocabulary needed by conversion;
- adaptation notices, loss policy, capability-neutral adaptation facts, stable tool-call IDs;
- wire/codec IDs, surface/family/profile data types that do not consume EggPool config;
- OpenAI Chat Completions finite codec;
- OpenAI Responses finite codec;
- Anthropic Messages finite codec;
- Gemini Interactions finite codec;
- Gemini generateContent finite codec;
- SSE framing, incremental provider event decoding, canonical client event encoding, usage normalization, terminal evidence, and native observation state.

Retain in the EggPool root:

- request body parsing ownership, aggregate resource reservations, token/context estimates, stateless product policy and compact-specific policy;
- adapters from catalog/routing/config types into neutral wire facts;
- profile registry construction from EggPool config and model preferences if that construction remains product policy;
- `WireRuntime` and all runtime/coordinator integration;
- provider transport, HTTP server/client concerns, credentials, retries, timeouts, persistence, health, quota, lifecycle, and error-to-HTTP mapping.

### Explicitly out of scope

- crates.io publication, semver-1.0 promises, separate repository creation, or package renaming.
- New provider features or protocol semantic changes.
- Replacing current preservation with the generalized provenance/fidelity API; M004 owns that.
- Splitting the kernel into many microcrates.
- Optional Tokio/Axum/HTTP-client features in the core crate.
- Moving EggPool integration tests wholesale out of the root package; protocol-only tests may move/share as appropriate, but root end-to-end tests remain authoritative for no-regression.
- Changing current release artifact count/packaging solely because a workspace library exists.

## 6. Required production changes

### Workspace package

Add one library workspace member with:

- edition 2024;
- repository MSRV;
- `publish = false`;
- `unsafe_code = "forbid"`;
- minimal dependencies only. The expected baseline is `serde`, `serde_json` with the ordering behavior EggPool currently requires, `thiserror`, and `sha2` if deterministic stable call IDs remain SHA-256 based. Any additional normal dependency requires written justification in closure evidence.
- no default feature that pulls runtime/network packages. Prefer no feature matrix at all unless a real codec-size boundary is proven necessary.

### Source-of-truth move

Move the neutral implementation rather than wrapping/copying it. Root `rust/src/wire/` may retain narrow adapter/facade files so existing EggPool imports and the canonical specification path remain coherent.

A preferred shape is:

```text
rust/crates/eggpool-wire/src/
  lib.rs
  ir.rs
  decode.rs
  adaptation.rs
  codec.rs
  codecs/
  profile.rs
  stream.rs

rust/src/wire/
  mod.rs          # re-exports + EggPool adapters
  ir.rs           # compatibility/canonical facade re-export
  registry.rs     # EggPool config/profile registry adapter if needed
  runtime.rs      # EggPool runtime join, unchanged in ownership
```

Exact file mechanics may differ if current repository evidence supports a cleaner layout; ownership may not.

### EggPool adapters

Convert EggPool catalog capability facts, request native-preservation ownership, routing facts, and config profile definitions into the extracted crate's neutral types without serializing through JSON or copying prompt/body content unnecessarily.

### Tests and fixtures

Move protocol-only unit tests next to the crate where useful, but keep the root integration suite exercising the same root public/internal paths. The M002 contract corpus must be consumable from both levels without divergent copies.

### Manifest/lock/release effects

Add the workspace/path dependency and regenerate the lockfile normally. Confirm the library does not alter the shipped binary's externally packaged files or introduce a second binary. Run `cargo deny`, feature-tree, duplicate-tree, and locked release build evidence because the dependency graph changes.

## 7. Ordered work packages

### Work package A — Create crate shell and compile the neutral core

Intent: establish a dependency-minimal library without changing root behavior.

Required changes:

- Add workspace package and library lints.
- Move the smallest leaf modules first (IR/profile data/adaptation primitives) and re-export them through root compatibility modules.
- Keep root tests compiling continuously.

Acceptance evidence:

- Crate builds/tests independently.
- Root builds with type-identity preserved; no serde bridge or duplicated type definitions.

### Work package B — Move finite codecs and decoder

Intent: establish one source of truth for all finite protocol grammars.

Required changes:

- Move canonical structural decoder and all five finite codecs.
- Keep EggPool product policy/admission wrappers in root.
- Preserve current error mapping and ordered adaptation notices.

Acceptance evidence:

- M002 corpus and all finite/multimodal/Codex tests pass unchanged.

### Work package C — Move stream state machines

Intent: extract incremental SSE and terminal semantics without changing forwarding ownership.

Required changes:

- Move framing/event decode/client encode/usage/terminal/native-observation machinery.
- Keep the root runtime object that decides NativeObserved vs Translated and forwards bytes.

Acceptance evidence:

- Arbitrary-chunk corpus, `wire_stream.rs`, runtime stream tests, Codex streaming cases, native terminal/usage observation and EOF classification all pass.
- Native-forwarded bytes remain exactly the bytes EggPool previously forwarded.

### Work package D — Remove duplicate implementations and qualify the workspace

Intent: ensure the extraction actually lowers maintenance burden.

Required changes:

- Delete or reduce root implementation copies to facades/adapters.
- Add a guard preventing reintroduction of a second codec implementation.
- Run full dependency, default/no-default, release, and tooling gates.

Acceptance evidence:

- Only one implementation source exists for each extracted primitive/codec.
- EggPool root remains first consumer and all integration behavior passes.

## 8. Failure, cancellation, restart, contention semantics

No new runtime work occurs in the crate, so cancellation/restart/contention behavior must remain exactly EggPool's current behavior. The extracted stream decoder is stateful only per request/stream object and must remain bounded. It may not spawn tasks, own sockets, retry, sleep, consult clocks, read environment/config, or persist anything.

Crate parse/adaptation errors must still map through EggPool's existing `WireRuntimeError`/`error.rs` authority. Do not add crate-level HTTP status semantics.

## 9. Compatibility and migration

This is an internal code-location migration with zero public migration.

During the cutover:

- preserve root module re-exports where practical to minimize unrelated churn;
- preserve serialized forms of public/stored types only if any are actually serialized today; do not add serialization promises merely for publication;
- preserve ordered adaptation codes and exact typed error categories;
- preserve current `WireCodecId`/`WireSurface` string values used by config/fixtures;
- preserve native Responses same-surface behavior and compact operation semantics;
- preserve release binary/package behavior.

No external consumer should be required to change because the crate is still `publish = false`.

## 10. Required tests

All M002 focused tests remain mandatory. Add crate-local tests for every extracted module and keep root integration coverage for:

- request admission → canonical → routing facts;
- profile/config selection → codec encode;
- native and translated finite provider response paths;
- Responses Compact;
- native and translated streams;
- provider error envelopes and usage;
- Codex Responses streaming/tool/custom/deferred-search behavior;
- Gemini Interactions and generateContent;
- default/no-default root behavior.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

Run the extracted crate's package-only tests/checks as a fast loop in addition to the root-focused M002 targets.

## 12. Documentation updates

- Update `architecture/deep-dive-transcoder.md` with the workspace ownership split.
- Update `architecture/README.md`/overview indexes only as needed to point to the new source-of-truth.
- Document in `rust/README.md` that the internal workspace library is part of the shipped EggPool build but not independently published yet.
- Keep canonical long-term semantics unchanged; if the literal `rust/src/wire/ir.rs` path becomes a re-export facade, document that it remains the EggPool canonical boundary while implementation types come from the workspace library.

## 13. Acceptance criteria

- EggPool consumes the extracted library for all canonical types, adaptation rules, finite codecs, and stream state machines.
- No extracted implementation copy remains in the root.
- Root runtime/admission/routing/config/transport/lifecycle ownership is unchanged.
- All five current upstream surface families and all three public client endpoints retain current finite/streaming behavior.
- Native Responses preservation and source-byte stream forwarding remain intact.
- Full default/no-default, locked release, dependency audit, and feature/duplicate tree gates pass.
- The extracted library has no runtime/network/persistence dependency and remains unpublished.
- No medium-or-higher compatibility or maintenance finding remains.

## 14. Stop conditions

Stop and report if:

- M002 is not closed or its contract corpus is incomplete;
- moving a module requires transferring runtime/admission/config/routing ownership to the library;
- type identity can only be preserved through JSON serialization bridges;
- native same-surface forwarding would regress;
- package dependency growth pulls Tokio, Axum, Hyper, TLS, SQLite, config, environment, or provider clients into the library;
- a public EggPool API or serialized/config value must change;
- the code move produces two live codec implementations.

## 15. Closure evidence required

The closure record must include:

- exact package dependency list and `cargo tree` evidence;
- source-of-truth map showing what moved and what remained EggPool-owned;
- proof root compatibility modules are facades/adapters, not duplicate implementations;
- M002 contract-corpus results plus focused finite/stream/native/Codex tests;
- full default/no-default test and clippy/check results;
- locked release build and cargo-deny results;
- release/package inspection proving no new binary/artifact requirement;
- explicit no-regression statement for every current client/provider surface, native Responses preservation, compact, adaptation policy, terminal evidence, request limits, config/reload, and routing;
- residual findings and whether M004 is unblocked.

## 16. Handoff notes

Do not publish the crate during this milestone. The goal is to prove that EggPool can depend on a clean internal extraction without losing capability. Favor source moves plus re-exports over broad call-site rewrites. Keep the library sans-I/O even if adding an HTTP convenience would reduce root code temporarily.
