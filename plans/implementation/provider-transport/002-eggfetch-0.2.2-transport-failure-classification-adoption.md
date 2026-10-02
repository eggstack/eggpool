# Provider Transport Milestone 002 — Eggfetch 0.2.2 Transport Failure Classification Adoption

Status: closing

Repository baseline: `8db2d16c73cb7bb832c23a2e037d5fe0823021f7`

Source roadmap:

- `plans/subsystems/provider-transport-roadmap.md#milestone-002--adopt-stable-eggfetch-transport-error-taxonomy`

Long-term requirements:

- `plans/000-long-term-specification.md` — provider transport remains one bounded HTTP owner beneath coordinator policy; no second retry/fallback owner.
- `plans/001-terminology-and-domain-model.md` — provider/account/runtime-generation identity and failure ownership remain authoritative.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — provider dependency qualification and transport-boundary hardening are sustaining Phase 1 work.
- `plans/003-planning-process.md` — bounded implementation, closure evidence, and registry lifecycle.

Applicable ADRs:

- None required. M002 consumes the already-established Eggfetch/Eggress provider boundary and the accepted upstream Eggfetch error-domain API. Stop and reassess the ADR threshold if implementation would move retry/routing ownership, enable a new protocol, or create a new public EggPool compatibility surface.

Primary class: infrastructure

## 1. Objective

Unblock and complete provider-transport M002 by adopting the published Eggfetch
0.2.2 native transport-failure classifier and removing EggPool's remaining
dependence on Hyper/Rustls implementation error chains.

The bounded outcome is:

- exact-pin `eggfetch-core` from `=0.2.1` to `=0.2.2`;
- resolve the Eggress-consumed `eggfetch-http-connect` package coherently at
  0.2.2 without changing Eggress 1.0.11;
- translate Eggfetch's public
  `Error::transport_failure_kind() -> Option<TransportFailureKind>` facts into
  EggPool's existing `TransportError` contract;
- remove provider-side nested Hyper/Rustls downcasts and recursive source-chain
  classification that upstream now owns;
- retain EggPool-specific timeout, proxy-route, custom-dialer, target,
  request-build, and defensive compatibility classifications ahead of the
  generic upstream classifier;
- fully requalify provider transport and its coordinator/wire consumers.

This is a transport-boundary decoupling and dependency adoption. It is not a
new retry taxonomy, provider capability, or coordinator policy change.

## 2. Why this milestone is ready

The historical interface blocker is satisfied.

Eggfetch v0.2.2 was published on 2026-10-02 from signed release candidate
`015a56d7ec3edf186eec8ebccff01cbf5584274e`. It publishes
`eggfetch-core 0.2.2` with one additive public diagnostic API:

- non-exhaustive
  `TransportFailureKind::{Connect, Tls, Protocol, Cancelled}`;
- `Error::transport_failure_kind()`.

The upstream contract is specifically suitable for native
`Client::execute_http_body()` and `NativeResponseBody` consumers. It
classifies dispatch-time and body-time failures without requiring consumers to
downcast Hyper, hyper-util, rustls, or nested I/O sources. It leaves
`Error`, `Error::kind()`, timeout facts, physical-admission facts,
`DialErrorKind`, and `RequestFailure/NetworkFailureKind` intact.

Upstream M007 qualification covered refused connection, nested Rustls/TLS
verification, Hyper cancellation, malformed response heads, truncated
Content-Length bodies, malformed chunk framing, normal EOF, read timeout
non-classification, lease release, API/semver compatibility, and Rust 1.89.
The coordinated 0.2.2 release then passed Tier 1/2/3 and external
fresh-consumer smoke.

EggPool's own prerequisites are closed:

- Provider M001 — Eggfetch adapter contract hardening;
- Provider M003 — Eggress adoption/requalification;
- Provider M004 — typed transport diagnostic evidence;
- Provider M005 — Eggfetch 0.2.1 / Eggress 1.0.11 refresh.

Eggress 1.0.11 declares `eggfetch-http-connect = "0.2.0"`, so Cargo's
compatible 0.2.x range admits the published 0.2.2 HTTP CONNECT primitive.
M002 therefore needs no Eggress release or source change to converge the
Eggfetch family.

## 3. Current implementation evidence

At the baseline:

- `rust/Cargo.toml` exact-pins `eggfetch-core =0.2.1` with
  `default-features = false`, `native-http1`, and `tls-rustls`.
- `rust/Cargo.lock` resolves `eggfetch-core 0.2.1`,
  `eggfetch-http-connect 0.2.1`, Eggress 1.0.11, and EggServe 0.4.0.
- `rust/src/providers/transport.rs::map_eggfetch_error` already preserves
  typed physical-admission, `TimeoutPhase`, `TransportIoDirection`, and
  custom `DialErrorKind` facts.
- Remaining Eggfetch implementation leakage is localized to
  `contains_source::<rustls::Error>()`,
  `eggfetch_source_is_canceled()`,
  `eggfetch_source_is_protocol()`,
  `contains_source` / `contains_io_kind`,
  `HyperClient(inner).is_connect()`, and direct I/O refusal inspection.
- `StdError` must not be deleted mechanically: the same module still uses it
  for independent typed Eggress test-support `ChainError` classification.
- Hyper/Rustls workspace dependencies retain live owners outside this
  classification seam, including `operations/update.rs` and test support.
- Existing provider real-socket tests already cover direct refusal, origin TLS
  failure, premature close, timeouts, cancellation/recovery, proxy families,
  route auth/refusal/TLS separation, SSH/no-default behavior, pool isolation,
  and redaction.
- Existing unit coverage pins custom `DialErrorKind` mapping. This matters
  because upstream broadly reports
  `CustomTransport(DialErrorKind::Connection)` as
  `TransportFailureKind::Connect`, while EggPool must retain
  `TransportError::ProxyConnect`.

Current-authority docs still name Eggfetch 0.2.1 and provider-side Hyper/Rustls
source inspection. Keep those claims until the M002 candidate qualifies.

## 4. Invariants that must not regress

- One coordinator attempt produces at most one provider transport submission.
- Provider transport remains HTTP/1.1 only.
- Eggfetch remains `native-http1,tls-rustls`; do not enable the `http1`
  compatibility alias, `standard-http1`, high-level URL policy, logical
  retry, redirects, Basic auth, built-in proxy, compression, HTTP/2, or HTTP/3.
- Configured Eggress routes remain fail-closed with no direct fallback.
- Eggress owns route/proxy establishment and route TLS; Eggfetch owns origin
  HTTP/TLS after byte-stream establishment.
- Account/client-pool isolation is unchanged.
- `TransportError` remains the stable provider taxonomy and M004 diagnostic
  labels remain static, secret-free, and policy-neutral.
- EggPool-specific specific facts take precedence over the generic classifier:
  pool/admission, timeouts, proxy-specific errors, custom `DialErrorKind`,
  target/request errors, and defensive compatibility mappings.
- Custom `DialErrorKind::Connection` remains `ProxyConnect`, never generic
  `Connect`.
- Do not introduce Display/Debug string classification or a new public
  EggPool-local compatibility taxonomy.
- Unknown future non-exhaustive `TransportFailureKind` variants must not panic
  or acquire retry/health semantics; retain existing phase fallback.
- Response streaming/body lease/cancellation recovery and diagnostics secrecy
  remain unchanged.
- Eggress stays 1.0.11, EggServe stays 0.4.0, updater/server ownership remains
  separate, and no config/wire/database migration is introduced.

## 5. Scope

### In scope

- pin `eggfetch-core =0.2.2`;
- converge `eggfetch-http-connect` to 0.2.2 through targeted resolution;
- consume `TransportFailureKind` at `map_eggfetch_error`;
- delete now-redundant Eggfetch Hyper/Rustls/source-chain classifiers;
- preserve all EggPool-specific mapping precedence;
- add narrow mapping/body-framing regressions where current coverage is
  insufficient;
- run default, `test-support`, no-default, coordinator/wire, dependency,
  tooling, footprint, hosted CI, and dependency-audit qualification;
- update current-authority docs after qualification;
- write M002 closure and reconcile planning.

### Explicitly out of scope

- new `TransportError` variants/labels;
- retry/failover/health/routing policy changes;
- `RequestFailure::send_detailed()` or a detailed native body;
- Eggfetch high-level policy, built-in proxy, HTTP/2/3, compression;
- Eggress/EggServe/updater/server transport changes;
- unrelated dependency modernization;
- deleting Hyper/Rustls dependencies that retain other live owners;
- changing historical M001/M003/M004/M005 evidence.

## 6. Required production changes

### 6.1 Dependency convergence

Change only the direct Eggfetch exact pin from 0.2.1 to 0.2.2, retaining
`default-features = false` and `native-http1,tls-rustls`.

Targeted lock resolution must produce:

- `eggfetch-core 0.2.2`;
- `eggfetch-http-connect 0.2.2`;
- no stale/split 0.2.1 Eggfetch package;
- Eggress 1.0.11 unchanged;
- EggServe 0.4.0 unchanged.

If Eggress's compatible HTTP CONNECT range does not converge on 0.2.2, stop
before changing Eggress.

### 6.2 Error mapping ownership and precedence

Keep EggPool-owned categories ahead of the broad upstream classifier:

1. physical admission/residual pool;
2. established I/O and request-phase timeouts;
3. invalid target/request/config semantics;
4. proxy-specific Eggfetch variants;
5. custom Eggress `DialErrorKind`;
6. defensive protocol/error cases whose historical EggPool mapping is not
   equivalent to the broad classifier.

Then translate `error.transport_failure_kind()`:

- `Tls` -> `TransportError::Tls`;
- `Cancelled` -> `TransportError::Cancelled`;
- `Protocol` -> `TransportError::Protocol`;
- `Connect` -> `TransportError::Connect`;
- unknown future variant -> existing fallback.

A small private pure conversion helper is acceptable for deterministic tests;
it must not become a public taxonomy or policy owner.

### 6.3 Remove implementation-specific inspection

Delete provider-side logic used only to recover Eggfetch facts now owned
upstream:

- nested `rustls::Error` search;
- nested `hyper::Error::is_canceled()`;
- nested Hyper parse/incomplete-message inspection;
- recursive nested `UnexpectedEof` search;
- `HyperClient(inner).is_connect()`;
- redundant connection-refusal inspection where upstream supplies the same
  reachable fact.

Retain independent Eggress `StdError` logic and other live Hyper/Rustls
owners.

### 6.4 Preserve non-equivalent defensive mappings

Review and retain historical EggPool `Protocol` handling for variants the
upstream classifier leaves unknown or classifies differently, including
`Body`, decompression/content-encoding/body-limit variants,
`Http2GoAway`, `Http2StreamReset`, `H3ConnectionClosed`, `H3Stream`,
and notably `H3Connect` (historically `Protocol` in EggPool but broadly
`Connect` upstream). These provider features are disabled, but M002 must not
silently alter the private compatibility map.

Do not recreate the old recursive `UnexpectedEof` heuristic. Eggfetch 0.2.2
contextually owns native body truncation/malformed framing. Preserve an
explicit top-level `Error::Io(UnexpectedEof)` case only if current EggPool
tests or reachable behavior prove it is part of the contract.

## 7. Ordered work packages

### Work package A — Baseline and mapping matrix

Record baseline SHA/toolchain, manifest/lock versions, inverse trees, package
count, non-dev tree size, release binary bytes, and a branch-by-branch map of
`map_eggfetch_error` classified as retained/replaced/defensive/fallback.

### Work package B — Dependency adoption

Bump the core pin, targeted-update both Eggfetch packages, inspect lock/feature
diffs, and compile before source refactoring.

### Work package C — Classifier cutover

Use `TransportFailureKind`, preserve EggPool-specific precedence, remove the
Eggfetch-specific source-chain helpers and HyperClient inspection, and retain
independent Eggress typed-source logic.

### Work package D — Mapping/body regression proof

- directly pin all four known classifier-to-`TransportError` mappings;
- prove `CustomTransport(DialErrorKind::Connection)` remains
  `ProxyConnect` even though the upstream broad kind is `Connect`;
- retain refused-connect, origin-TLS, and premature-body real-socket tests;
- add deterministic malformed-chunk/equivalent framing coverage if needed to
  exercise the new body-context classifier;
- keep existing cancellation/recovery lifecycle tests and avoid a fragile
  duplicate Hyper-cancellation reproducer.

### Work package E — Full requalification

Run provider transport under default/`test-support`/no-default, focused
coordinator/wire consumers, full serial workspace matrices, feature/no-dev/
duplicate/inverse trees, `cargo deny`, locked release build, tooling, and
validators.

Treat 0.2.2 as more than a classifier-only patch: review consumer-relevant
upstream hardening around timeout arithmetic, CONNECT target validation,
Content-Length conflicts, TLS/config fail-closed behavior, and transport
retry/cancellation. The exact native feature recipe limits exposure; real
provider fixtures are the proof.

### Work package F — Measure, document, close

Compare baseline/candidate dependency and binary footprint, update live
dependency/error-boundary docs, write
`plans/closure/provider-transport/002-status.md`, record hosted CI/dependency
audit, and reconcile roadmap/registry.

## 8. Failure, cancellation, restart, contention semantics

M002 adds no task, runtime, retry, queue, lock, persistence, or restart owner.
A mismatch fails the candidate; do not retain parallel old/new classifiers.

Proxy failures remain fail-closed. Cancellation/drop keeps existing pool/body
cleanup and same-client recovery. Unknown future classifier variants fall
through rather than acquiring policy. No reload/restart/config semantics
change.

## 9. Compatibility and migration

No database, config, wire, HTTP API, CLI, persisted-data, or user migration.

`TransportError` and diagnostic labels remain behavior-compatible.
Eggfetch 0.2.2 is additive but is fully requalified because it also includes
post-0.2.1 correctness/security changes.

Eggress 1.0.11 consumes the compatible HTTP CONNECT patch; this is lockfile
convergence, not an Eggress migration. Updater Hyper/Rustls ownership remains
unchanged.

## 10. Required tests

```bash
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --no-default-features --test provider_transport -- --test-threads=1

cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1

cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

All new tests must be deterministic and serial. Prefer existing loopback
fixtures and observable gates.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings

cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --no-default-features --test provider_transport -- --test-threads=1

cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1

cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1

cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml -e features --no-default-features
cargo tree --manifest-path rust/Cargo.toml --features test-support -e features
cargo tree --manifest-path rust/Cargo.toml -e no-dev
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo tree --manifest-path rust/Cargo.toml -i eggfetch-core
cargo tree --manifest-path rust/Cargo.toml -i eggfetch-http-connect
cargo tree --manifest-path rust/Cargo.toml -i eggress-outbound
cargo tree --manifest-path rust/Cargo.toml -i eggress-embed
cargo tree --manifest-path rust/Cargo.toml -i eggress-server

uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
```

Record package count, non-dev tree line count, release binary bytes, and exact
Eggfetch/Eggress/EggServe resolved versions before/after. Hosted CI and
dependency audit must pass before closure.

## 12. Documentation updates

After qualification update current-authority statements in:

- `AGENTS.md`;
- architecture/development/documentation skills;
- `architecture/overview.md`;
- `architecture/deep-dive-providers.md`;
- `architecture/deep-dive-core.md` if its live summary names 0.2.1;
- `docs/proxy.md`;
- `rust/README.md`;
- any current README/docs statement naming Eggfetch 0.2.1 or provider-side
  Hyper/Rustls error inspection.

The provider deep dive should describe Eggfetch 0.2.2's classifier seam and
remove source inspection as a live provider reason for Hyper/Rustls ownership,
while retaining updater/test-support owners. Historical plans/closures remain
unchanged.

## 13. Acceptance criteria

1. `eggfetch-core =0.2.2` exact-pin with unchanged native feature recipe.
2. Both Eggfetch packages resolve 0.2.2 with no stale/split 0.2.1 package.
3. Eggress 1.0.11 and EggServe 0.4.0 remain unchanged.
4. Generic TLS/cancellation/protocol/connect facts use
   `Error::transport_failure_kind()`.
5. Provider Eggfetch classification contains no Hyper/hyper-util/Rustls
   downcasts and no message parsing.
6. Pool/timeout/proxy/custom/target/request/defensive mappings preserve prior
   categories.
7. Custom dialer Connection remains `ProxyConnect` despite upstream broad
   `Connect`.
8. Direct refusal -> `Connect`, origin TLS -> `Tls`, malformed/truncated
   framing -> `Protocol`, known cancellation -> `Cancelled`.
9. Future non-exhaustive classifier values do not panic or change policy.
10. Default/`test-support`/no-default provider suites pass.
11. Focused coordinator/wire suites preserve one-attempt/one-submission and
    diagnostic policy neutrality.
12. Strict default/no-default/full serial workspace, `cargo deny`, release
    build, tooling, and validators pass.
13. Feature graphs show no forbidden Eggfetch policy/protocol or production
    listener/runtime capability.
14. Comparable dependency/footprint evidence is recorded.
15. Current-authority docs are accurate.
16. Hosted CI and dependency audit pass.
17. M002 closure exists and roadmap/registry are reconciled.

## 14. Stop conditions

Stop rather than improvise if:

- a currently reachable category still requires consumer Hyper/Rustls source
  inspection;
- preserving supported behavior requires Display/Debug parsing;
- a new public EggPool compatibility taxonomy is needed;
- Cargo cannot converge Eggfetch HTTP CONNECT 0.2.2 while retaining Eggress
  1.0.11;
- adoption requires high-level Eggfetch policy or a new protocol;
- custom Eggress failures lose proxy-specific classification;
- retry/fallback/health/routing semantics must change;
- supported proxy/TLS/SSH/account/body lifecycle cannot be preserved;
- unrelated lock churn cannot be isolated;
- dependency policy reveals a separate unresolved security decision;
- scope expands into updater/server or another subsystem.

## 15. Closure evidence required

`plans/closure/provider-transport/002-status.md` must record:

- baseline and implementation/final SHAs;
- upstream 0.2.2 release/tag and classifier API evidence;
- exact manifest/lock package versions;
- before/after mapping matrix and removed source-chain branches;
- proof of no provider Eggfetch Hyper/Rustls downcasts;
- custom-dialer precedence;
- direct connect/TLS/framing/cancellation evidence;
- all provider/coordinator/wire/default/no-default results;
- feature/listener-free/dependency evidence;
- `cargo deny`, release build, tooling, validator results;
- package/tree/duplicate/binary footprint comparison;
- current-authority docs updated;
- hosted CI/dependency-audit run IDs;
- deviations/unresolved findings and disposition;
- downstream unblock audit.

## 16. Handoff notes

Start with dependency convergence and compile before refactoring. Preserve
unrelated work and keep integration tests serial.

The implementation hazard is precedence, not API availability:
`TransportFailureKind` is intentionally generic. Consume it only after
EggPool's more specific route/proxy/custom facts.

Do not remove `StdError`, Hyper, or Rustls mechanically; verify remaining
owners. M002 removes Eggfetch-classification leakage, not repository-wide use.

Do not create a fragile Hyper-cancellation reproduction solely for downstream
coverage. Upstream 0.2.2 qualifies that evidence; pin the consumer conversion
deterministically and retain EggPool's lifecycle cancellation/recovery test.
