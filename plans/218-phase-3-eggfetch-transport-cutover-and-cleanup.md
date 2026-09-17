# Phase 3 — Eggfetch Transport Cutover and Cleanup

Status: planned

Depends on:
- `215-eggfetch-transport-consolidation-roadmap.md`
- `216-phase-1-eggfetch-direct-transport-foundation.md`
- `217-phase-2-eggfetch-egress-dialer-integration.md`

## Objective

Make Eggfetch the sole generic HTTP/TLS transport implementation behind `ProviderHttpClient`, remove the now-redundant bespoke Hyper/Rustls connection lifecycle from Eggpool, and reduce Eggpool's direct dependency/maintenance surface without changing coordinator/provider behavior.

This is the deletion/consolidation phase. Do not begin it until both the direct and Eggress qualification paths are green on Eggfetch.

## Scope

### In scope

- finalize one Eggfetch-backed `ProviderHttpClient` implementation for direct and proxied routes,
- centralize all Eggfetch-to-`TransportError` translation,
- remove temporary dual-stack migration scaffolding,
- delete Eggpool-owned generic physical admission/connect/TLS/I/O wrapper machinery now replaced by Eggfetch,
- remove direct Hyper/Rustls-related dependencies no longer referenced by Eggpool,
- regenerate/update lock state,
- keep externally observable provider transport behavior stable,
- retain acceptance tests while deleting only implementation-specific tests that no longer describe a supported unit.

### Out of scope

- provider routing redesign,
- coordinator retry/backoff/failover redesign,
- Eggress protocol redesign,
- new transport protocols,
- HTTP/2/HTTP/3 enablement,
- Eggfetch high-level request conveniences,
- broad provider module cleanup unrelated to transport ownership,
- performance tuning not required to restore current behavior.

## Desired end state

`ProviderHttpClient` should remain the local boundary seen by the rest of Eggpool, but its internal responsibilities should be narrow:

1. validate Eggpool-specific request constraints,
2. construct the native HTTP request,
3. select/configure the already-built direct or Eggress-backed Eggfetch client,
4. execute through `execute_http_body(...)`,
5. translate Eggfetch errors into stable `TransportError` values,
6. expose the streaming response in the form expected by current callers.

It should no longer contain a parallel general-purpose HTTP transport implementation.

## Remove obsolete custom transport machinery

After confirming there are no remaining call sites, delete the bespoke equivalents now owned by Eggfetch. Based on the current transport implementation, candidates include the code responsible for:

- Hyper legacy-client construction,
- the custom Hyper connector service used only to establish provider streams,
- `AdmissionConnector` or equivalent connection semaphore wrapper,
- physical connection permit ownership implemented locally,
- local physical-admission timeout wrapper,
- `TimedConnection` or equivalent established socket read/write timeout wrapper,
- direct TCP connector implementation that Eggfetch now supplies,
- manual destination Rustls client configuration and connector wiring,
- manual destination TLS handshake/SNI wrapper over direct/Eggress streams,
- Hyper-specific pool configuration duplicated by Eggfetch,
- Hyper-specific canceled-request retry configuration duplicated by Eggfetch,
- transport error string/source inspection that is superseded by typed Eggfetch error metadata.

Do not delete Eggpool-specific request validation or Eggress route configuration merely because it resides in the same file today.

## Preserve `ProviderHttpClient` as the containment boundary

Avoid introducing a new public/internal trait hierarchy just because the implementation changed.

The rest of Eggpool should not need to know whether transport is powered by Hyper directly or through Eggfetch. Preserve existing constructors/method signatures where practical. If the native response-body type requires a local wrapper/type alias to keep callers stable, prefer that small compatibility layer over propagating Eggfetch types throughout provider/coordinator code.

A goal of this phase is to **reduce** the number of transport concepts Eggpool maintains.

## Finalize the error translation boundary

Create one authoritative mapping from `eggfetch_core::Error` to `TransportError` and ensure all request paths use it.

The translator should explicitly recognize, at minimum:

- physical connection admission timeout,
- connection-establishment timeout,
- established transport read timeout,
- established transport write timeout,
- custom dialer errors and their `DialErrorKind`,
- origin TLS errors,
- HTTP/protocol/framing errors,
- request/body construction failures where applicable,
- ordinary connection failures.

### Required classification invariants

- `PhysicalConnectionPolicy::admission_timeout` -> `TransportError::PoolTimeout`.
- origin connect timeout -> `TransportError::ConnectTimeout`.
- established transport read inactivity -> `TransportError::ReadTimeout`.
- established transport write inactivity -> `TransportError::WriteTimeout`.
- Eggress/custom-route timeout -> current Eggress timeout category where that is the established contract.
- Eggress authentication/rejection -> current Eggress/connect classification; never masquerade as origin TLS.
- origin certificate/SNI/handshake failure -> `TransportError::Tls`.
- malformed HTTP response -> `MalformedResponse` or the current exact category pinned by tests.
- generic HTTP protocol failure -> `Protocol`.
- physical admission/cancellation must not be reported as an upstream provider HTTP response.

Prefer stable Eggfetch predicates/accessors and typed `DialError` data. Do not retain legacy recursive string searches if typed data is sufficient.

If one existing `TransportError` variant becomes unreachable after correct typed mapping, retain it through this phase unless its removal is clearly internal and all call sites/tests prove no API/behavior dependency. Do not combine error-enum cleanup with transport cutover unless it is necessary.

## Preserve request semantics

Before deleting the old implementation, compare the final Eggfetch path against the existing request builder and retain:

- current URI joining/validation,
- HTTP/1.1 request version,
- `Host` behavior,
- `Content-Length` behavior,
- maximum request body enforcement,
- provider headers and authorization behavior,
- no automatic redirects,
- no transparent logical retry,
- no automatic body decoding introduced below the provider layer.

The native Eggfetch API should be used specifically to avoid moving policy down into the HTTP engine.

## Preserve response semantics

The final transport must remain streaming and must not introduce a hidden body collection step.

Confirm the final body adapter/wrapper preserves:

- DATA-frame ordering,
- trailers if the current call chain can observe them,
- EOF semantics,
- errors during body streaming,
- read inactivity errors after response headers,
- early drop behavior,
- pool lease/physical capacity release,
- cancellation safety.

If current callers only consume data frames, do not add unnecessary trailer-specific infrastructure, but also do not accidentally convert a frame-preserving Eggfetch body into a buffer solely for type convenience.

## Dependency cleanup

After code cutover, use code search and `cargo tree`/`cargo metadata` to determine which direct dependencies can be removed.

Expected candidates from the current `rust/Cargo.toml` are:

- `hyper`,
- `hyper-rustls`,
- `hyper-util`,
- `tower-service`,
- `webpki-roots`,
- direct `rustls` if there is no remaining production/test use outside the removed transport.

Do not remove a dependency just because Eggfetch also depends on it transitively. Remove it only when Eggpool no longer imports/uses it directly.

Likely retained dependencies include:

- `http`,
- `bytes`,
- `http-body-util` if still used to construct native request bodies,
- `tokio`,
- Eggress crates,
- other provider/router dependencies unrelated to this migration.

Regenerate lock data using the repository's normal Cargo workflow and inspect the diff for unintended feature activation.

### Feature-tree audit

After cleanup, confirm Eggfetch has not accidentally enabled:

- HTTP/2,
- HTTP/3,
- built-in proxy support,
- compression codecs,
- native roots,
- JSON/cookie/multipart convenience stacks.

Use `cargo tree -e features` or the repository-equivalent command to verify actual resolved features rather than relying only on the manifest declaration.

## Delete migration-only scaffolding

Remove any temporary enum, alternate constructor, test-only transport selector, or old/new branch introduced in phases 1-2 solely to stage migration.

The final code should not carry both transports or a hidden legacy fallback.

Do not retain a runtime `use_eggfetch` switch. Source control is the rollback mechanism after parity has been established.

## Test cleanup policy

Keep tests that assert behavior visible outside the deleted implementation.

Examples that must remain:

- physical capacity/admission behavior,
- idle reuse/expiry,
- request shape,
- redirect behavior,
- timeout classification,
- response streaming/premature close,
- TLS trust/hostname behavior,
- proxy protocol integration,
- account pool separation,
- authentication/fail-closed behavior,
- cancellation recovery.

Tests may be deleted or rewritten only when they directly instantiate a now-deleted private helper such as the old connector/timer wrapper and their behavior is already covered through `ProviderHttpClient` acceptance tests.

Do not reduce the proxy matrix merely because Eggress is now behind a generic `Dialer`; those tests are precisely what demonstrate the boundary is correct.

## Static/code-search checks

Before considering the cutover complete, search production provider transport code for obsolete ownership patterns and imports.

Examples:

```bash
rg 'hyper(_rustls|_util)?|tower_service|webpki_roots|rustls' rust/src/providers rust/Cargo.toml
rg 'AdmissionConnector|TimedConnection' rust/src
```

Interpret results rather than requiring zero matches globally. A dependency may legitimately remain elsewhere. The goal is zero **redundant provider-transport ownership**, not zero transitive references across the repository.

Also search for any direct handling of Eggfetch errors above `ProviderHttpClient`; there should be none.

## Validation sequence

Run validation in increasing scope so classification/lifecycle failures are easier to diagnose:

1. focused direct transport tests,
2. focused Eggress/proxy transport tests,
3. entire `provider_transport` integration test,
4. provider/coordinator tests that consume `TransportError`,
5. complete Rust test suite,
6. clippy/check/fmt matching CI,
7. feature/dependency tree audit.

Baseline commands:

```bash
cargo fmt --check
cargo check --all-targets
cargo clippy --all-targets --all-features -- -D warnings
cargo test --test provider_transport
cargo test
cargo tree -e features
```

Adjust only to match repository CI/platform constraints; do not silently skip the provider transport integration suite.

## Review checklist

Before merging/handing off phase 3, verify:

- [ ] all provider HTTP traffic goes through Eggfetch,
- [ ] proxied traffic still routes through Eggress `Dialer`,
- [ ] no direct-network fallback exists for proxied clients,
- [ ] no old Hyper client is built in Eggpool,
- [ ] no old physical admission wrapper remains,
- [ ] no old established-I/O timer wrapper remains,
- [ ] no old origin TLS connector remains,
- [ ] no high-level Eggfetch retries or redirects are configured,
- [ ] per-account clients remain distinct,
- [ ] error translation is centralized,
- [ ] old direct dependencies are removed where unused,
- [ ] Eggfetch feature resolution remains minimal,
- [ ] all transport acceptance tests remain green.

## Phase completion criteria

Phase 3 is complete when:

- Eggfetch is the only generic provider HTTP transport engine,
- Eggpool no longer owns redundant Hyper/Rustls connection-lifecycle code,
- Eggress remains a raw-route dependency rather than an HTTP client implementation,
- `ProviderHttpClient` remains the stable containment boundary,
- typed error mapping preserves Eggpool's established error categories,
- temporary migration scaffolding is gone,
- unused direct transport dependencies are removed,
- lockfile/feature resolution is clean and intentional,
- the existing provider/coordinator behavior tests pass without weakening acceptance criteria.

## Handoff notes

This phase should result in materially less transport code in Eggpool. If the diff adds roughly as much new abstraction as it deletes old machinery, reconsider the design before proceeding. The intended architecture is Eggfetch for generic HTTP transport, Eggress for routing, and a thin Eggpool adapter/policy boundary between them.
