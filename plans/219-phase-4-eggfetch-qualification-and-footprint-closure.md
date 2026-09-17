# Phase 4 — Eggfetch Qualification and Footprint Closure

Status: complete

Depends on:
- `215-eggfetch-transport-consolidation-roadmap.md`
- `216-phase-1-eggfetch-direct-transport-foundation.md`
- `217-phase-2-eggfetch-egress-dialer-integration.md`
- `218-phase-3-eggfetch-transport-cutover-and-cleanup.md`

## Objective

Close the Eggfetch migration with behavioral qualification, dependency/feature verification, and controlled before/after footprint measurements. This phase determines whether the consolidation achieved its maintenance objective without introducing transport regressions or an unacceptable artifact/dependency cost.

Binary-size reduction is a measured outcome, not a prerequisite assumed by the roadmap.

## Why this phase exists

Eggfetch 0.1.5 provides the transport primitives Eggpool needs, but replacing mature connection code can produce subtle lifecycle regressions that are not visible in simple request-success tests. Eggpool also targets lightweight local/SBC deployments, so the final dependency and release-artifact impact should be measured rather than inferred from crate names or minimal external fixtures.

Eggfetch's own minimal comparison fixtures have shown that Eggfetch can resolve fewer packages than a comparable `reqwest` setup while still producing a somewhat larger minimal executable. That data should not be projected directly onto Eggpool because Eggpool already contains Hyper/Rustls plus bespoke transport code. Only an Eggpool before/after build is authoritative for this migration.

## Scope

### In scope

- run the complete transport acceptance suite,
- run the full Rust validation suite used by repository CI,
- verify coordinator-facing retry/error behavior remains unchanged,
- inspect the resolved Eggfetch feature graph,
- inspect direct and resolved dependency changes,
- produce controlled before/after release artifact measurements,
- perform a small runtime/resource smoke comparison where practical,
- inspect release/installer target compatibility after the MSRV bump,
- record the measured result and any justified follow-up,
- close or document any remaining migration-specific TODO/scaffolding.

### Out of scope

- inventing a permanent heavyweight benchmark CI system,
- enforcing an arbitrary binary-byte budget with no historical baseline,
- optimizing Eggfetch itself solely to improve one Eggpool number,
- enabling new protocols/features because the migration is complete,
- unrelated provider/router performance work.

## Qualification baseline

Use a pre-migration Eggpool revision from immediately before phase 1 and the final post-phase-3 revision.

Record both commit SHAs in the implementation/closure notes so measurements are reproducible.

All before/after measurements must use the same:

- target triple,
- Rust toolchain version where technically possible,
- Cargo profile,
- feature set,
- linker configuration,
- LTO/codegen settings,
- strip settings,
- environment class.

Because the migration raises MSRV to Rust 1.89, if the original baseline cannot build under the exact final toolchain, first attempt to build both revisions with Rust 1.89. If that changes the baseline artifact materially, record the limitation rather than comparing unrelated build conditions silently.

## Behavioral qualification

### 1. Provider transport acceptance suite

Run the complete existing `rust/tests/provider_transport.rs` suite without excluding slow-but-bounded proxy cases that are part of normal repository qualification.

The suite should continue to cover:

- HTTP/1.1 request shape,
- `Host` and `Content-Length`,
- direct connection reuse,
- idle expiration,
- no redirect following,
- no hidden retry behavior,
- incremental/chunked response handling,
- premature upstream close,
- request-body size rejection,
- physical connection admission,
- physical admission timeout,
- cancellation while waiting for/using capacity,
- refused/unreachable connections,
- connect timeout,
- read/write inactivity classification,
- explicit CA trust,
- hostname/certificate validation,
- separate direct/proxied/account pool identities,
- SOCKS4/SOCKS5,
- HTTP CONNECT,
- Shadowsocks,
- SSR,
- Trojan,
- SSH,
- chained Eggress routes,
- proxy authentication/rejection,
- fail-closed custom routes,
- cancellation recovery and subsequent client usability.

Any failure in these areas is a migration blocker unless the old behavior is proven incorrect and the behavior change is reviewed separately.

### 2. Coordinator/provider behavior

Run tests above the raw transport boundary that exercise:

- retry/failover attempt counts,
- provider/account suppression/backoff decisions,
- handling of `TransportError` categories,
- recovery after upstream failures,
- no process/router poisoning after malformed or failed upstream requests.

Specifically look for accidental extra attempts caused by transport retries. One Eggpool coordinator attempt must not turn into multiple hidden upstream logical attempts.

### 3. Cancellation and resource recovery

Stress the lifecycle cases most likely to expose pool/permit leaks:

- repeated canceled requests while pool capacity is saturated,
- early response-body drop,
- upstream close during body streaming,
- route establishment cancellation,
- proxy authentication failure followed by a valid request,
- read timeout followed by a valid request.

The client must recover without requiring an Eggpool restart or client/pool rebuild.

A bounded loop inside an existing integration test is sufficient. Do not add an unbounded soak requirement to normal CI.

## Standard repository validation

Use the current GitHub workflow/configuration as the source of truth for exact flags and targets. At minimum the final implementation should pass the equivalent of:

```bash
cargo fmt --check
cargo check --all-targets
cargo clippy --all-targets --all-features -- -D warnings
cargo test --test provider_transport
cargo test
```

If CI uses additional target/platform checks, run or verify those as appropriate for the release matrix.

Do not permanently expand CI solely for this migration unless a newly discovered regression class justifies the added maintenance/runtime cost.

## Dependency and feature audit

### 1. Manifest audit

Confirm the final direct dependency set no longer includes transport crates that Eggpool itself does not use after consolidation.

Expected removals, subject to actual remaining imports:

- `hyper`,
- `hyper-rustls`,
- `hyper-util`,
- `tower-service`,
- `webpki-roots`,
- direct `rustls` if no longer referenced outside tests/other features.

Record any expected candidate that remains and why.

### 2. Eggfetch feature audit

Use:

```bash
cargo tree -e features
```

or a narrower package-specific equivalent to prove the final graph does not unintentionally enable:

- Eggfetch built-in proxy support,
- HTTP/2,
- HTTP/3,
- compression codecs,
- native-root trust,
- JSON/cookie/multipart convenience features.

The intended Eggfetch dependency remains the minimal HTTP/1 + Rustls configuration from the roadmap.

### 3. Resolved package count

Capture a reproducible before/after resolved dependency count. Use one consistent Cargo metadata/tree method for both revisions.

Do not claim dependency slimming based only on direct manifest lines. Distinguish:

- direct dependency ownership,
- total resolved packages,
- feature activation.

A migration can still be valuable if the resolved graph is similar while Eggpool no longer owns generic transport code.

## Release artifact measurement

### Required builds

At minimum, measure the primary release target used for routine distribution. If convenient within the current release environment, also measure one SBC/Linux target because low-footprint deployment is an explicit project concern.

Use the repository's normal release profile and stripping process. Do not compare an unstripped debug-ish baseline with a stripped final binary.

For each measured target record:

| Measurement | Before | After | Delta |
|---|---:|---:|---:|
| final artifact bytes | | | |
| direct dependencies | | | |
| resolved packages | | | |
| Eggfetch enabled features | N/A | | |

Optionally record build time/RSS only if it can be measured under comparable local conditions; these are secondary and should not become acceptance blockers without a clear regression.

### Interpretation

Classify the binary result descriptively:

- smaller,
- approximately flat,
- larger by a measured amount.

Do not invent a required percentage improvement.

Investigate if the final artifact grows materially more than expected. First check for accidental feature activation, duplicate TLS/root stacks, retained old direct dependencies, or both old and new transport implementations being linked.

A modest increase can be acceptable if:

- behavior is fully preserved,
- old bespoke transport code is actually removed,
- direct ownership/dependency maintenance is reduced,
- no accidental features are present,
- the artifact remains suitable for the project's SBC/local deployment targets.

If a large unexplained regression remains after feature/dependency cleanup, document it and decide whether to revert or pursue a separate general Eggfetch footprint improvement. Do not specialize Eggfetch for Eggpool inside this phase.

## Source-maintenance footprint

Record the code ownership removed as part of the closure notes. Exact line-count reduction is optional, but identify which categories were deleted:

- custom Hyper connector,
- custom physical admission wrapper,
- custom established-I/O timeout wrapper,
- manual origin TLS/Rustls connector logic,
- direct Hyper client pool construction,
- implementation-specific error plumbing.

This is the principal expected win and should be visible in the final diff even if linked binary size is flat.

## Runtime smoke checks

Do a bounded local smoke test representative of Eggpool's intended use:

- repeated sequential requests to demonstrate reuse,
- concurrent requests up to/over configured physical capacity,
- one failed route/provider followed by successful recovery,
- one proxied route where available in the existing integration harness.

Watch for:

- runaway connection creation,
- stuck permits,
- task leaks visible as non-termination,
- materially increased idle RSS under equivalent conditions,
- unexpectedly high per-request latency caused by rebuilding clients/connections.

Do not turn this into a production-scale benchmark project. Existing deterministic tests remain the primary gate.

## MSRV and release matrix verification

The migration raises Rust MSRV to 1.89. Verify that:

- manifest metadata reports 1.89,
- CI's oldest-toolchain check, if present, uses 1.89,
- release builders support 1.89,
- supported Linux/SBC, macOS, and Windows build paths are not pinned to an older compiler,
- installer/update logic is unaffected because artifact naming/distribution did not change.

Do not modify installer/update behavior unless the build/release verification exposes a concrete incompatibility.

## Documentation/closure record

Add or update a concise repository note if the project has an established place for architectural/dependency decisions. At minimum the final commit/PR description should record:

- Eggfetch version used,
- why native `execute_http_body` is used instead of the high-level request pipeline,
- why `PhysicalConnectionPolicy` maps to Eggpool `max_connections`/`pool_timeout`,
- why custom `Dialer` is used for Eggress rather than Eggfetch proxy support,
- the trust-store choice (`WebPkiOnly` plus additional roots),
- dependency removals,
- measured artifact/dependency deltas.

Do not add a large new architecture document solely to duplicate comments already clear in code/plans.

## Regression triage order

If final qualification fails, investigate in this order:

1. wrong timeout/lifecycle mapping,
2. accidental high-level Eggfetch retry/redirect behavior,
3. physical versus logical connection-limit confusion,
4. custom dialer target/fail-closed behavior,
5. origin TLS versus route TLS ownership,
6. response-body lease/drop lifecycle,
7. error translation precedence,
8. accidental feature/dependency activation.

Do not weaken acceptance tests before ruling out these integration mistakes.

## Final completion checklist

- [x] Rust MSRV is 1.89 everywhere it is authoritative.
- [x] Eggfetch is exact-pinned to reviewed 0.1.5 for the initial landing.
- [x] Only intended Eggfetch features are enabled.
- [x] All direct provider transport tests pass.
- [x] All Eggress/proxy transport tests pass.
- [x] Full Rust tests pass.
- [x] Formatting/check/clippy match CI.
- [x] Coordinator attempt counts show no hidden transport retries.
- [x] Error categories remain stable.
- [x] Cancellation/body-drop cases recover capacity.
- [x] No direct fallback occurs for custom routes.
- [x] Old generic Hyper/Rustls transport code is removed.
- [x] Unused direct transport dependencies are removed.
- [x] Resolved feature/dependency graph is audited.
- [x] Before/after release artifact sizes are recorded under comparable conditions.
- [x] Any binary-size increase is explained or escalated rather than hidden.
- [x] Supported release targets remain buildable.
- [x] No migration-only runtime switch/scaffolding remains.

## Phase completion criteria

Phase 4, and therefore the Eggfetch consolidation roadmap, is complete when all behavioral qualification is green, the dependency/feature graph is intentional, the release artifact impact has been measured and recorded, and Eggpool demonstrably owns less generic transport machinery without surrendering its routing/failure-isolation semantics.

## Handoff notes

The final decision should be based on Eggpool's measured result, not Eggfetch-versus-reqwest micro-fixtures. The expected architectural result is strong even if the stripped binary is roughly unchanged: Eggpool stops maintaining a custom Hyper/Rustls connection stack, while Eggfetch remains a general reusable HTTP engine and Eggress remains a general routing engine.

## Completion note

Implemented on `main`. Baseline `7cc0521c` (pre-phase-1) versus
post-phase-3 `028ad76a`, both built with Rust 1.89.0 for
`aarch64-apple-darwin` under the normal release profile without stripping:

- behavioral qualification green: `--features test-support --test
  provider_transport` (35 passed), `--test provider_transport` (30 passed),
  coordinator `c008`/`c009`/`c011`/`boundaries`/`finalization`/`publication`
  (no hidden retries, stable attempt counts and error categories,
  cancellation/body-drop recovery), serial `cargo test --workspace
  --all-targets` (680 passed, 0 failed);
- feature audit clean: `eggfetch-core` resolves only `http1` + `tls-rustls`
  (plus implicit `hyper-rustls`); no proxy/HTTP-2/HTTP-3/compression/
  native-root/JSON/cookie/multipart activation;
- dependency delta: direct `-1` (`tower-service` removed;
  `hyper`/`hyper-util`/`hyper-rustls`/`rustls`/`webpki-roots` retained for
  the live `operations/update.rs`, error-boundary, and `test-support`
  owners), resolved `383 -> 414` (`+31`, entirely the `eggfetch-core`
  closure: `url`/`idna`/ICU plus `dashmap`; nothing removed from the graph);
- artifact delta: `27,908,656 -> 30,139,568` bytes (`+2,230,912`, `+8.0%`),
  classified as larger by a measured amount, explained and acceptable per
  the interpretation criteria; SBC suitability preserved;
- source maintenance: only `rust/src/providers/transport.rs` changed under
  `rust/src/`; bespoke connector/admission/timer/TLS-string-plumbing
  ownership deleted, leaving request validation, route selection, and one
  typed error boundary;
- runtime smoke: release binary boots, serves degraded-but-valid
  `/v1/readyz` + `/api/status` with zero accounts, holds ~15.6 MB idle RSS,
  and stops cleanly with no lingering tasks;
- MSRV/release matrix: `1.89` in `rust/Cargo.toml`,
  `rust/crates/eggpool-connect/Cargo.toml`, and all `release.yml` builders
  (shared `1.81` crates unchanged by design); CI uses stable with no
  oldest-toolchain pin; installer/update paths untouched;
- footprint recorded in `architecture/deep-dive-providers.md`; no new
  architecture document, no migration scaffolding remains, no CI expansion.

This closes the Eggfetch consolidation roadmap (`215`–`219`): Eggpool owns
less generic transport machinery without surrendering routing or
failure-isolation semantics. Any future `url`/IDNA trimming inside
Eggfetch is a separate general upstream improvement.
