# Runtime Efficiency Milestone 001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/runtime-efficiency/001-provider-wire-resolver-hotpath-cleanup.md`

Source subsystem roadmap:

- `plans/subsystems/runtime-efficiency-roadmap.md#milestone-001--providerwire-resolver-hot-path-ownership-and-cache-cleanup`

Repository baseline reviewed: `b9ba100d8f4165e338c76a11359381137590b1d2`

Implementation commits:

- `77664694` — optimize provider wire resolver hot path
- `c5c33b69` — fix borrowed coordinator lint and isolate lifecycle test errors

## 1. Executive finding

M001 is complete. Finite and streaming attempt preparation now borrows provider/profile data synchronously; the prepared attempt remains owned before I/O. Resolver lookup and LRU maintenance avoid the identified temporary allocations and linear recency scan while preserving resolver behavior. Default and no-default workspace suites, strict Clippy, formatting, and the locked release build passed. No downstream hard dependency was waiting on M001; M002 and M003 remain ready.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Borrow providers for finite and streaming preparation | `rust/src/coordinator/finite.rs`, `rust/src/coordinator/streaming/coordinator.rs`; full serial workspace suites | pass | References do not cross the submit await; owned attempt boundary retained. |
| Avoid cloning full profile vectors on the common path | `rust/src/coordinator/attempt.rs::prepare_candidates_borrowed`; `coordinator_c011` | pass | Public candidates still own their data; compact filtering remains local. |
| Remove temporary provider/model tuple lookup strings | `rust/src/coordinator/wire_resolver.rs`; strict Clippy and resolver tests | pass | Nested maps support borrowed provider/model lookup. |
| Preserve fingerprint bytes | `coordinator_c011::c011_wire_fingerprint_matches_preallocation_reference_and_lru_recency` | pass | Test computes the former format and compares SHA-256 output. |
| Bounded LRU with correct recency/eviction | Same C011 regression; resolver capacity 2, touched A remains after B eviction, entries remain <=2 | pass | Indexed BTreeMap order replaces `VecDeque::retain`; capacity trimming updates both indexes. |
| Preserve candidate, learning, rejection, TTL, negotiation, retry and compact behavior | C011, C008, C009, coordinator-boundary, wire-runtime, wire-qualification, and provider-transport suites | pass | No policy or public API changes. |
| Default/no-default parity | Full serial workspace tests in both feature configurations | pass | Exact commands below. |
| No dependency or schema changes | `git diff b9ba100d..77664694 -- rust/Cargo.toml rust/Cargo.lock rust/src/db rust/assets/db/migrations` | pass | No Cargo, DB, or migration changes. |

## 3. Production implementation evidence

Finite/streaming coordinator paths keep the selected `ProviderConfig` borrowed from immutable generation state and use borrowed profile slices for synchronous candidate preparation. `PreparedUpstreamAttempt` remains fully owned before provider submission. A crate-private borrowed candidate helper avoids cloning the entire static profile vector first.

Resolver preference state uses nested provider/model maps, avoiding temporary tuple-string lookup keys. Fingerprinting streams the same candidate and preference bytes directly into SHA-256 without an intermediate formatted vector and joined string. Cache recency uses two bounded BTreeMap indexes; ordinary touch is O(log N), with deterministic least-recently-used eviction and capacity trimming. Architecture ownership notes were updated in `architecture/deep-dive-request-lifecycle.md`.

Before/after ownership and complexity:

| Path | Before | After |
|---|---|---|
| Selected provider | Deep clone per attempt | Borrow during synchronous preparation |
| Provider profiles | Clone full vector before candidate filtering | Borrow slice; own only resulting candidates |
| Preference lookup | Allocate `(provider, model)` strings | Borrow through nested maps |
| Fingerprint | Build formatted `Vec<String>` and joined string | Incremental hash updates over equivalent bytes |
| LRU hit | Linear `retain` scan | O(log capacity) indexed recency update; O(capacity) storage |

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
CARGO_BUILD_JOBS=1 cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
CARGO_BUILD_JOBS=1 cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
umask 077; mkdir -m 700 /tmp/eggpool-m001-full-runtime-20261003; EGGPOOL_PID_FILE=/tmp/eggpool-m001-full-runtime-20261003/eggpool.pid EGGPOOL_RUNTIME_DIR=/tmp/eggpool-m001-full-runtime-20261003 CARGO_BUILD_JOBS=1 CARGO_PROFILE_TEST_DEBUG=0 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
umask 077; mkdir -m 700 /tmp/eggpool-m001-no-default-runtime-20261003; EGGPOOL_PID_FILE=/tmp/eggpool-m001-no-default-runtime-20261003/eggpool.pid EGGPOOL_RUNTIME_DIR=/tmp/eggpool-m001-no-default-runtime-20261003 CARGO_BUILD_JOBS=1 CARGO_PROFILE_TEST_DEBUG=0 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
CARGO_BUILD_JOBS=1 cargo build --manifest-path rust/Cargo.toml --locked --release
git diff --check
```

Focused targets also passed: `coordinator_c011` (18), `coordinator_c008` (29), `coordinator_c009` (13), `coordinator_boundaries` (5), `provider_transport` (35 default / 36 no-default), `wire_runtime` (8), and `wire_qualification` (16). Full workspace commands completed successfully with serial test execution. The private runtime directory and restrictive umask were needed by existing process lifecycle and updater fixtures on this host. An initial parallel full-suite attempt exhausted linker memory; the serial low-debug rerun completed. No scope was skipped.

## 5. Invariant review

- Candidate ordering, preference precedence, rejection suppression, TTL, and negotiation singleflight remain covered by existing resolver tests and passed.
- Fingerprint identity is checked against the pre-change serialization in a regression test.
- Cache storage is limited by configured capacity; recency is indexed without a stale-event queue.
- Borrowed references are used only during synchronous preparation; owned attempt/context facts cross the await boundary.
- Retry, health, quota, finalization, public candidate/resolution surfaces, and compact profile filtering remain unchanged.

## 6. Failure and recovery review

This change adds no persistent state and changes no cancellation, retry, provider submission, or recovery authority. The full provider, coordinator, runtime lifecycle, and workspace suites passed. Test harness environmental failures encountered during qualification were resolved by isolating PID/runtime paths and using the updater's expected restrictive umask; no product-code failure remained.

## 7. Migration and compatibility review

No migration, schema, wire, configuration, or public API change. No new dependency. Existing resolver behavior and fingerprint bytes are retained. Rollback is an ordinary code revert.

## 8. Security review

No credentials or request data were added to cache keys, fingerprints, logs, or diagnostics. Resolver state remains capacity-bounded. No authorization or privilege boundary changed.

## 9. Documentation and operations

Updated `architecture/deep-dive-request-lifecycle.md` to record synchronous provider/profile borrowing and the owned pre-submit boundary. No operational command, diagnostic, or recovery procedure changed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No M001-scoped unresolved finding | — | — |

Active-request snapshot/claim-book optimization, routing selection locks, Tokio runtime flavor, stream handoff, and persistence topology remain outside this milestone and require their own evidence/plan. Persistence M007 remains independently blocked on physical Pi/MMC evidence.

## 11. Roadmap disposition

Milestone closed; next independent work may proceed. M002 and M003 were already ready and have no hard dependency on M001. M001's closure does not change persistence M007 or any other blocked work.

## 12. Registry updates

`plans/registry.md` and `plans/subsystems/runtime-efficiency-roadmap.md` mark M001 closed and link this record. M002 and M003 remain ready. The registry status transition and this closure record are committed together.
