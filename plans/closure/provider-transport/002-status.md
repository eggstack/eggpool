# Provider Transport Milestone 002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/provider-transport/002-eggfetch-0.2.2-transport-failure-classification-adoption.md`

Source subsystem roadmap:

- `plans/subsystems/provider-transport-roadmap.md#milestone-002--adopt-stable-eggfetch-transport-error-taxonomy`

Repository baseline: `8db2d16c73cb7bb832c23a2e037d5fe0823021f7`

Final implementation source SHA: `6f0cfd532e753a43c454b370453850c55679aab5`
Status transition commit: commit containing this closure record.

Implementation commits:

- `86623b1f` — activate Provider Transport M002 after Eggfetch 0.2.2 supplied
  the required public classifier API.
- `67d6ceb3` — adopt Eggfetch 0.2.2 transport failure classification.
- `cd9bdcc1` — enter closing after local qualification.
- `6f0cfd532e753a43c454b370453850c55679aab5` — current implementation and
  documentation head; also contains the separately scoped M007 qualification
  feature, which does not change M002's dependency or provider behavior.

## 1. Executive finding

M002 adopts Eggfetch 0.2.2's public transport-failure classifier and removes
EggPool's provider-side dependence on nested Hyper/Rustls error-chain
inspection. EggPool maps upstream Connect/TLS/Protocol/Cancelled facts into
its existing `TransportError` categories while preserving precedence for
physical admission, timeouts, proxy routes, Eggress custom dialers, targets,
request construction, and defensive residual errors. No retry, routing,
health, protocol, or public EggPool taxonomy changed.

The exact dependency convergence is Eggfetch core 0.2.2 plus Eggfetch HTTP
CONNECT 0.2.2, with Eggress 1.0.11 and EggServe 0.4.0 unchanged. Local gates
pass. Hosted CI and Dependency audit ran against implementation commit
`6f0cfd532e753a43c454b370453850c55679aab5`; both passed.

The upstream [Eggfetch v0.2.2 release](https://github.com/eggstack/eggfetch/releases/tag/v0.2.2)
publishes the non-exhaustive `TransportFailureKind::{Connect, Tls, Protocol,
Cancelled}` classifier and `Error::transport_failure_kind()` API. The release
is the compatibility boundary this milestone consumes; it leaves Eggfetch's
existing timeout, physical-admission, custom dialer, and request-failure facts
available to their existing owners.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Exact Eggfetch dependency convergence | `rust/Cargo.toml`, `rust/Cargo.lock`, Cargo feature/inverse trees | pass | Core and HTTP CONNECT resolve to 0.2.2 with no stale 0.2.1 package. |
| Preserve Eggress and EggServe | Manifest, lockfile, resolved dependency graph | pass | Eggress remains 1.0.11; `eggserve-server` remains 0.4.0. |
| Use upstream typed transport classifier | `rust/src/providers/transport.rs::map_eggfetch_error`; mapping unit tests | pass | Generic Connect/TLS/Protocol/Cancelled use `transport_failure_kind()`. |
| Remove source-chain coupling | Provider transport implementation review and tests | pass | No Hyper/Rustls downcasts or display/message parsing remain in Eggfetch mapping. Other Hyper/Rustls owners remain untouched. |
| Preserve EggPool mapping precedence | Unit and real-socket provider fixtures | pass | Timeout, admission, proxy, target, request-build, residual pool, and custom dialer mappings remain EggPool-owned. |
| Preserve custom dialer proxy category | `custom_dial_kinds_map_to_stable_proxy_transport_errors` | pass | `CustomTransport(DialErrorKind::Connection)` remains `ProxyConnect`, even though Eggfetch's broad kind is Connect. |
| Provider transport across feature profiles | `provider_transport` default / `test-support` / no-default | pass | 35 / 41 / 36 tests passed. |
| Coordinator and wire invariants | C008/C009/C011, boundaries, finalization, publication, `wire_runtime` | pass | 89 focused tests passed; retry ownership and diagnostic policy are unchanged. |
| Full Rust matrices | Default and no-default serial workspace suites | pass | 832 default and 833 no-default tests passed across 65 suites in the qualified M002 run. |
| Strict Rust and MSRV gates | fmt, default/no-default/test-support Clippy, no-default check, Rust 1.89 check | pass | Locked default release build passed. |
| Dependency and feature policy | `cargo deny`, default/test-support/no-default trees, inverse graphs | pass | No forbidden Eggfetch policy/protocol or production listener capability. Existing unrelated duplicate-version warnings remain. |
| Artifact/dependency footprint | M002 pre/post measurements | pass | Package metadata count 382 and non-dev tree lines 970 unchanged; M002 candidate binary 31,144,480 bytes vs 31,156,480 baseline (12,000 bytes smaller). |
| Tooling and authority docs | Ruff, Pyright, tooling suite, boundary/release validators; AGENTS/skills/architecture/proxy docs/Rust README | pass | Current authority describes Eggfetch 0.2.2 and retained external owners. |
| Hosted CI | Run `37070421301`, head `6f0cfd532e753a43c454b370453850c55679aab5` | pass | Manual `workflow_dispatch`; all jobs passed, including 832 default Rust tests across 65 suites and 152 tooling tests (3 platform skips). |
| Hosted dependency audit | Run `37070423689`, same head | pass | Completed successfully. |

## 3. Production implementation evidence

`rust/Cargo.toml` exact-pins `eggfetch-core = "=0.2.2"` with the existing
`default-features = false`, `native-http1`, and `tls-rustls` recipe. The lock
resolves both Eggfetch packages to 0.2.2. Eggress remains exact-pinned to
1.0.11 and EggServe to 0.4.0.

`map_eggfetch_error` uses EggPool-specific typed facts before consulting the
non-exhaustive upstream `TransportFailureKind`. The fallback remains
secret-free and defensive for unknown future variants. Custom Eggress
`DialErrorKind` handling stays separate from Eggfetch's generic classifier.
The implementation does not parse Error display/debug output and does not
inspect nested Hyper/Rustls sources for Eggfetch errors. Hyper/Rustls remain
because updater and test-support code still own those dependencies.

The qualified provider fixtures cover direct refusal, origin TLS, malformed
or truncated responses, cancellation, timeouts, proxy route/auth/refusal/TLS,
custom dialers, pooling, account isolation, recovery, redaction, SSH, and the
no-default behavior. Coordinator and wire tests preserve one-submission,
one-attempt, no-retry-after-handoff, and policy-neutral diagnostics.

## 4. Verification executed

Local evidence on the M002 implementation included:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --no-default-features --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
rustup run 1.89.0 cargo check --manifest-path rust/Cargo.toml --workspace --all-targets
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

Provider tests passed 35/41/36; focused coordinator and wire consumers passed
89 tests. The M002 qualification recorded 832 default and 833 no-default
workspace tests across 65 serial suites. Strict default/no-default/test-support
Clippy, no-default check, format, Rust 1.89, locked release, and Cargo policy
checks passed. Tooling recorded 153 passed and 1 skipped at the M002
qualification point. Hosted dependency audit `37063274364` passed on the
M002 implementation; final run `37070423689` passed on implementation head
`6f0cfd532e753a43c454b370453850c55679aab5`. Hosted CI run `37070421301`
also passed on that exact head.

## 5. Invariant, compatibility, and security review

- One request attempt still produces at most one provider submission; no new
  retry, health, routing, or fallback owner was introduced.
- HTTP/1.1-only transport and the existing Eggfetch feature recipe remain.
- `TransportError` variants/labels, public APIs, persisted diagnostics, schema,
  config, CLI, and wire behavior are unchanged.
- Failure labels stay bounded and secret-free. No raw upstream error text,
  URI, credentials, body, or nested implementation details are persisted or
  logged.
- Cancellation/body ownership and Eggfetch lease release remain unchanged.
- No new runtime dependency or production listener/server capability was
  added.

The later M007 change is separately feature-gated and is not part of M002's
dependency graph or failure-classification behavior. It is covered by the
branch's default hosted CI run but does not alter this milestone's outcome.

## 6. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No M002 mapping, compatibility, policy, or security finding remains. | None. | None. |

## 7. Unblock audit and roadmap disposition

M002 was made dependency-ready by the coordinated Eggfetch 0.2.2 publication
and its public `TransportFailureKind` / `Error::transport_failure_kind()` API.
Its completion unblocks no registered implementation plan: the active plan
table contains no provider-transport successor, and the only currently ready
plan `provider-profile-metadata-planning/documentation-reconciliation C001`
is independent. Historical blocked/unblock snapshots in `plans/registry.md`
remain unchanged.

Provider Transport M001–M005 are all closed after this record is accepted.
Under the roadmap's completion definition, the Provider Transport subsystem
roadmap is therefore closed with no ready successor. The distinct provider
profile metadata roadmaps remain independent.
