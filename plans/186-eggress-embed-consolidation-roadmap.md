# Plan 186: Eggress embed consolidation roadmap

> **Status:** complete (verified 2026-09-13; SSH facade exception documented)
>
> **Parent:** Follow-on to Plan 170 dependency minimization and the completed Plan 178 maintenance-consolidation line
>
> **Phase:** Roadmap
>
> **Scope:** Replace Eggpool-owned Eggress chain-construction plumbing with the current stable `eggress-embed` outbound facade, preserve transport semantics, then remove unnecessary direct production dependencies and measure the result.

## Problem statement

Eggpool currently pins Eggress `1.0.2` and reaches through the stable `eggress-embed` facade into several Eggress implementation crates from `rust/src/providers/transport.rs`:

- `eggress-core`
- `eggress-config`
- `eggress-pproxy-compat`
- `eggress-server`
- `eggress-uri`
- `eggress-transport-ssh`

Those direct dependencies were justified when Plan 170 was written because Eggpool itself had to parse and compile multi-hop pproxy expressions, build chain executors, and construct SSH-aware proxy transports.

That premise changed in Eggress `1.0.6`. The current stable `eggress_embed::outbound::OutboundConnector::from_pproxy_uri` accepts both a single pproxy URI and the canonical `__`-separated multi-hop form, compiles the parsed chain directly to the native Eggress representation, and constructs the chain executor internally. With the `ssh` feature enabled, the facade also owns the SSH-capable executor path.

Eggpool therefore now duplicates logic that belongs behind the Eggress embed boundary. The goal is to remove that duplication without changing provider HTTP behavior, retry ownership, proxy routing, TLS verification, account isolation, or failure classification.

This is primarily a **boundary and maintenance consolidation**. A lower direct-dependency count is expected. A smaller linked binary is possible but is not assumed: `eggress-embed` still depends transitively on several Eggress implementation crates, so manifest simplification and binary-size reduction must be measured separately.

## Current transport boundary

The provider stack currently has three layers that must remain conceptually distinct:

1. Eggpool owns provider HTTP semantics: Hyper/Rustls, destination TLS, request/response streaming, physical connection admission, timeouts, and the stable `TransportError` categories.
2. Eggress owns the optional TCP proxy leg below destination HTTP/TLS.
3. The coordinator owns request attempts, retry/failover, persistence, and account routing.

This roadmap changes only layer 2's construction boundary. It must not move retry or provider HTTP ownership into Eggress.

## Research findings that drive this roadmap

### Eggress 1.0.6 can replace Eggpool's production chain builder

The current `OutboundConnector::from_pproxy_uri`:

- accepts single-hop and canonical `__`-separated multi-hop pproxy expressions;
- rejects unsupported chain members rather than silently dropping them;
- compiles the compatibility chain directly to native `ProxyChainSpec` state;
- executes the chain through the native `ChainExecutor`;
- supports SSH when the embed `ssh` feature is enabled;
- exposes a listener-free `connect_tcp(host, port)` suitable for Eggpool's Hyper connector adapter.

Consequently the normal production path no longer needs Eggpool's `ChainEgressProxyDialer`, `proxy_uses_ssh`, manual pproxy translation/TOML compilation, or direct `build_chain_executor` call.

### `direct://` remains a deliberate compatibility edge

Eggpool currently recognizes `direct://`, validates it through Eggress, then uses its normal direct Hyper `HttpConnector`. The existing comment documents this as intentional for loopback/test provider targets and to keep the direct provider transport identical to the unproxied path.

Do not silently change that behavior merely because current Eggress can construct a direct `OutboundConnector`. Preserve the existing `direct://` special case unless an explicit parity test proves there is no behavioral regression for loopback/private destinations, timeout mapping, connection admission, and destination TLS.

### Deterministic proxy-TLS tests still need a narrow internal seam

Eggpool's `test-support` constructor can supply a caller-provided Rustls root for deterministic local TLS proxy peers such as the Trojan integration fixture. The current public `eggress-embed` outbound facade does not expose caller-supplied chain TLS configuration.

Production does not need this hook. The preferred near-term design is therefore:

- stable `eggress-embed` only in the default production path;
- retain the existing custom-root chain construction only behind `test-support`, with its internal Eggress dependencies made optional/test-only;
- do not disable certificate verification;
- do not add a public Eggress API solely to eliminate a test-only dependency edge.

If Eggress later gains a generally useful caller-trust API, Eggpool can remove this exception in a small follow-up.

### Existing qualification is already strong

`rust/tests/provider_transport.rs` already covers the behaviors this migration could regress, including:

- the mandatory pproxy construction corpus;
- explicit `direct://` operation;
- SOCKS4/SOCKS5 and HTTP-family construction;
- live Shadowsocks and SSR traffic;
- Trojan success with a deterministic test CA and failure without that CA;
- SSH success, authentication failure, timeout, and cancellation;
- ordered HTTP -> SOCKS5 multi-hop transport;
- credentials not appearing in diagnostics;
- account-specific proxy client isolation;
- unreachable proxies never falling back to direct transport.

Use this suite as the migration acceptance surface. Add only focused regressions that expose a newly discovered gap.

## Target state

After this roadmap closes:

- all normal proxy expressions are constructed through `eggress_embed::outbound::OutboundConnector::from_pproxy_uri`;
- Eggpool no longer parses or compiles pproxy chains in production code;
- Eggpool no longer constructs production Eggress `ChainExecutor` or `SshSessionCache` objects directly;
- default/non-test Eggpool code names `eggress-embed` as its Eggress API boundary;
- direct Eggress implementation dependencies remain only where proven necessary for `test-support` or integration fixtures;
- provider HTTP/TLS, physical connection limits, timeout semantics, and coordinator retry ownership remain unchanged;
- all protocol and failure-isolation tests pass against the upgraded Eggress version;
- dependency and binary-footprint changes are measured and recorded rather than inferred from manifest line count.

## Sequencing

### Plan 187 — Eggress 1.0.6 upgrade and production connector consolidation

Upgrade the Eggress family atomically, route production single-hop/multi-hop/SSH expressions through `OutboundConnector::from_pproxy_uri`, delete the duplicate production chain-building path, and preserve current direct/TLS/error semantics.

This phase proves behavior before attempting dependency pruning.

### Plan 188 — Test-support trust boundary isolation

Move the deterministic caller-root path behind an explicit feature/development boundary so it cannot justify implementation-crate coupling in default builds. Preserve certificate verification and the existing Trojan/SSH regression coverage.

This phase makes the production boundary structurally enforceable.

### Plan 189 — Dependency and footprint qualification closure

Remove now-unused direct production Eggress dependencies, inspect the feature graph, run the full qualification matrix, and compare same-profile release binary size before/after. Record what actually shrank and what remains transitively linked through `eggress-embed`.

This phase closes the work without inventing a binary-size claim.

## Cross-plan invariants

The following are acceptance invariants across every implementation phase:

1. A configured proxy failure never falls back to direct provider transport.
2. Eggress never owns provider HTTP retry/failover; one Eggpool coordinator attempt remains one visible transport attempt.
3. Destination HTTPS remains verified by Eggpool's Rustls configuration.
4. Proxy TLS verification remains enabled; deterministic local roots are test-only trust additions, not verification bypasses.
5. `max_connections`, `pool_timeout`, `connect_timeout`, read/write inactivity guards, keepalive limits, and body-size limits retain their current Eggpool semantics.
6. Proxy/account client topology remains immutable within a runtime generation.
7. Proxy credentials do not appear in `Debug`, `Display`, tracing fields, or surfaced error strings.
8. Unsupported or malformed pproxy expressions fail construction explicitly.
9. `direct://` keeps its current externally observable behavior unless a separately proven parity change is intentionally accepted.
10. No new proxy abstraction or generalized transport framework is introduced in Eggpool.

## Dependency policy

Do not optimize for the fewest manifest lines at the expense of clear ownership.

The desired rule is:

- production code depends on the stable Eggress facade;
- test-only code may depend on implementation crates when a public facade intentionally does not expose a testing capability;
- all Eggress crates used together must remain on one exact release line to avoid a mixed internal graph;
- optional/test-only implementation dependencies must be commented with the exact reason they remain.

Do not introduce git dependencies or path overrides into the committed production manifest. Use the published release line qualified by this roadmap.

## Verification philosophy

Each phase must be independently buildable and reviewable. Prefer semantic tests over snapshots of implementation details.

At minimum the line of work ends with:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo check --manifest-path rust/Cargo.toml --all-targets --all-features
cargo clippy --manifest-path rust/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-features
cargo tree --manifest-path rust/Cargo.toml -e features
```

Plan 189 adds same-profile release-size and dependency-tree comparison.

## Non-goals

This roadmap does not:

- replace Eggpool's Hyper provider client with Eggfetch;
- replace Axum with Eggserve;
- redesign provider retry/failover policy;
- add HTTP/2 or HTTP/3 to provider transport;
- change supported proxy schemes;
- broaden proxy configuration syntax;
- weaken TLS verification;
- require an upstream Eggress API solely for Eggpool tests;
- chase transitive crate-count reduction when the stable facade legitimately owns those crates;
- introduce a new proxy framework inside Eggpool.

## Completion criteria

This roadmap is complete when Plans 187-189 are closed and the repository demonstrates all of the following:

- the default provider transport no longer constructs Eggress chains itself;
- multi-hop and SSH paths operate through the stable embed API;
- the custom trust-root exception is isolated to test support;
- direct production dependency declarations match actual source ownership;
- the full existing proxy qualification suite remains green;
- footprint results are recorded using comparable builds;
- no known proxy correctness or security regression remains.

## Approval checklist

- [x] Plan 187 is implemented and qualified.
- [x] Plan 188 is implemented and qualified.
- [x] Plan 189 is implemented and qualified.
- [x] Production source names only the intended stable Eggress boundary, with the documented 1.0.6 SSH facade fallback exception.
- [x] Test-only internal coupling is explicit and justified.
- [x] No proxy fallback, TLS, timeout, or retry invariant changed accidentally.
- [x] Final dependency/footprint evidence is recorded.

## Closure evidence

Verified 2026-09-13 from implementation base `7b25cec748d8d012172f47c9f26155f967a8c795`; exact implementation/closure head `d648df8`.

- Eggress declarations and the lockfile are atomically on `1.0.6`; no `1.0.2`
  Eggress package remains in the resolved graph.
- Normal single-hop, multi-hop, Shadowsocks, SSR, Trojan, HTTP, SOCKS, and
  direct transport construction now uses
  `eggress_embed::outbound::OutboundConnector::from_pproxy_uri`. The direct
  `direct://` special case remains unchanged.
- The provider adapter converts the facade stream at the EggPool boundary;
  provider HTTP/TLS, admission, timeout, retry, and error ownership remain
  outside Eggress.
- Eggress 1.0.6's published facade does not pass an SSH session cache when it
  builds its outbound executor. Consequently SSH reports `no handler for
  protocols: [Ssh]` through the facade. To preserve the supported SSH
  contract, EggPool retains a narrowly named `eggress-ssh-fallback` feature
  (enabled by default) using the matching 1.0.6 native executor and explicit
  compatibility host-key policy. This is the documented facade gap permitted
  by Plan 189; it is the only ordinary production implementation-crate
  exception.
- The deterministic custom-root path remains feature-gated by `test-support`,
  adds a caller-provided root without disabling verification, and retains the
  Trojan success/failure pair. Protocol fixture crates remain dev-only.
- The only redundant facade feature, explicit `extended`, was removed;
  `pproxy-legacy` and `legacy-crypto` imply the required extended runtime.
- Same-profile raw release size: baseline `30,096,008` bytes; final
  `29,570,824` bytes; delta `-525,184` bytes (`-1.75%`). `cargo-bloat` reported
  approximately `18.3 MiB` versus `17.9 MiB` of `.text`; its output is treated
  as attribution guidance, not exact accounting.
- The normal dependency tree measured 409 unique entries at the baseline and
  410 after the 1.0.6 upgrade; the small increase is not treated as a success
  metric. Implementation crates remain transitively owned by `eggress-embed`
  and by the explicitly documented SSH fallback.

Verification passed:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo check --manifest-path rust/Cargo.toml --all-targets
cargo check --manifest-path rust/Cargo.toml --all-targets --features test-support
cargo check --manifest-path rust/Cargo.toml --all-targets --all-features
cargo clippy --manifest-path rust/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport (35 passed)
cargo test --manifest-path rust/Cargo.toml --all-features -- --test-threads=1 (476 passed, 53 suites)
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo deny --manifest-path rust/Cargo.toml check
git diff --check
```

Plans 187, 188, and 189 were all `Ready`, their prerequisites are now
satisfied, and their statuses were advanced to complete with the SSH facade
exception recorded in each phase. No later plan in `plans/` depends on Plan
186 or these phases, so no additional future-plan status transition was
required.
