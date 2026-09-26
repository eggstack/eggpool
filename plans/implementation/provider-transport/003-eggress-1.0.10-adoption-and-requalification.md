# Provider Transport Milestone 003 — Eggress 1.0.10 adoption and requalification

Status: ready

Repository baseline: `8bf99e7cb7d3a4c8e0cad383be8efb4ffa9cb91f`

Source roadmap:

- `plans/subsystems/provider-transport-roadmap.md#milestone-003--eggress-1010-adoption-and-requalification`

Long-term requirements:

- `plans/000-long-term-specification.md` §2 — secret-free diagnostics and no-default build parity.
- `plans/000-long-term-specification.md` §3 — immutable generation-owned `ProviderClientPool` topology.
- `plans/000-long-term-specification.md` §6 — no second HTTP/retry owner and no EggPool SSH executor fallback.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — exact-pin/feature-graph sustaining requalification.
- `plans/003-planning-process.md` — dependency, verification, and closure rules.

Applicable ADRs:

- None required. Eggress remains the already-selected proxy-chain owner and `eggress-outbound` remains the already-selected listener-free integration boundary.

Primary class: infrastructure

## 1. Objective

Upgrade every live Eggress package pin consumed by EggPool from 1.0.8 to the published 1.0.10 family and requalify the existing listener-free provider proxy integration without changing routing, retry, health, provider HTTP, origin TLS, proxy fallback, or public API semantics.

The expected production change is primarily `rust/Cargo.toml` plus `rust/Cargo.lock` and current-authority version documentation. Source changes are permitted only when actual 1.0.10 API/feature behavior proves a compatibility adjustment necessary. Do not use the upgrade as a proxy transport redesign.

## 2. Why this milestone is ready

Legacy Plan 243 closed the 1.0.8 migration to `eggress-outbound` and the typed `OutboundConnectError` boundary. Eggress v1.0.10 is published (2026-09-24; tag commit `0e4b49678f4a4cc9acba14049311b8c1744eefaa`). The v1.0.8..v1.0.10 range is 44 commits ahead and retains the public `OutboundConnector::from_pproxy_uri` and `connect_tcp_detailed` methods used by EggPool.

The upstream delta is materially relevant to embedding consumers: outbound connection metadata, direct connector metadata acquisition, hop-zero SSH/H2 reuse, TLS-policy identity scoping for pooled H2, nested-hop pooling behavior, and TLS `ClientConfig`/ALPN adaptation changed so caller trust/mTLS/verifier state is preserved. These changes justify requalification but do not require a new architecture.

There is no hard dependency on provider-transport M001. M001 and M003 can be reasoned about independently, but both may touch `rust/src/providers/transport.rs` and `rust/tests/provider_transport.rs`. They SHOULD NOT be implemented concurrently in one worktree; if M001 lands first, rebase M003 on its closure candidate before editing.

## 3. Current implementation evidence

At the baseline:

- `rust/Cargo.toml` exact-pins `eggress-outbound`, `eggress-core`, `eggress-config`, `eggress-pproxy-compat`, `eggress-server`, `eggress-uri`, and dev protocol crates at `=1.0.8`.
- Production `eggress-outbound` disables defaults and enables `pproxy-compat`, `pproxy-legacy`, and `legacy-crypto`; the root `ssh` capability additionally enables `eggress-outbound/ssh` and `eggress-pproxy-compat/ssh`.
- `test-support` retains optional low-level Eggress core/config/compat/server/uri crates and `eggress-server/ssh` for deterministic proxy TLS roots.
- `rust/src/providers/transport.rs::EggressDialer` owns an `Arc<eggress_outbound::OutboundConnector>` and calls `connect_tcp_detailed()` exactly once per dial.
- `build_eggress_dialer()` supplies no alternate direct route after proxy selection. `direct://` is validated through Eggress and intentionally maps to Eggfetch direct dialing.
- `map_outbound_dial_error()` matches typed kind/stage facts only; there is no message-string route classifier.
- `build_eggfetch_client()` installs the Eggress dialer as Eggfetch's only custom physical route while Eggfetch retains provider/origin TLS, HTTP/1.1, physical admission, pooling, and timeout ownership.
- `rust/src/providers/client_pool.rs` builds dedicated clients per proxied provider account and atomically retires the generation topology.
- `rust/tests/provider_transport.rs` contains the direct/proxy/authentication/target-refusal/route-TLS/timeout/cancellation/recovery coverage that closed Plan 243.

Plan 243 recorded prior acceptance evidence (provider transport default/test-support, no-default, coordinator focused targets, serial workspace suite, `cargo deny`, and release footprint). Those are historical facts only; M003 must rerun qualification on 1.0.10.

## 4. Invariants that must not regress

- A configured proxy never silently falls back to direct networking after construction or dial failure.
- One coordinator attempt causes at most one provider transport submission.
- Eggress performs proxy route/protocol/TLS work; Eggfetch performs provider HTTP/1.1 and origin TLS.
- Provider account selection, retry budgets, health/backoff/quarantine policy, and wire selection remain EggPool-owned and unchanged.
- Dedicated account clients remain distinct even when accounts use identical proxy URIs; do not deduplicate pools.
- `OutboundConnectError` classification remains typed; no display/message parsing may return.
- Route-level TLS failure remains a proxy-route failure and must not become `TransportError::Tls`.
- Proxy authentication and proxy target refusal remain distinguishable where typed Eggress evidence supports them.
- Default builds preserve SSH proxy support. `--no-default-features` preserves direct/non-SSH proxy construction and rejects SSH configuration before dialing.
- No Eggress UDP, QUIC/H3, `insecure-tls`, listener, service runtime, or hidden retry surface enters the production graph.
- Credentials, credential-bearing proxy URIs, provider keys, prompts, raw bodies, and cache keys stay out of diagnostics and closure artifacts.
- Generation retirement still prevents new pool lookups while allowing already-cloned clients to finish under their lease.

## 5. Scope

### In scope

- exact-pin the live Eggress family at `=1.0.10`;
- narrow lockfile resolution with no unrelated dependency upgrade;
- compile/API adjustments only if 1.0.10 makes them necessary;
- preserve/verify root `ssh` and `test-support` feature contracts;
- requalify documented proxy protocols/chains, typed route errors, cancellation/recovery, account isolation, and fail-closed behavior;
- inspect resolved feature/dependency graph for new or removed families;
- compare immediate pre/post release artifact and dependency footprint on the same host/toolchain/profile;
- run `cargo deny`, strict Clippy, no-default gates, locked release build, focused coordinator/wire targets, serial workspace tests, and hosted CI/dependency audit;
- update current-authority 1.0.8 references after qualification;
- write `plans/closure/provider-transport/003-status.md` and update roadmap/registry from evidence.

### Explicitly out of scope

- no Eggfetch version/feature change;
- no provider HTTP/2/HTTP/3 migration;
- no new proxy protocols or new documented proxy syntax;
- no retry/backoff/health/quarantine changes based on richer Eggress errors;
- no account-client pool deduplication;
- no production proxy-CA or insecure-TLS option;
- no replacement of Eggress pproxy parsing/translation with EggPool code;
- no removal of legacy-crypto/pproxy-legacy without a separate compatibility decision;
- no M004 diagnostic enrichment;
- no upstream Eggress source change;
- no physical SBC run unless ordinary qualification exposes an unexplained material resource regression;
- no edits to historical flat Plans 186–191 or 243.

## 6. Required production changes

### Exact package-family upgrade

Update all live direct/dev/optional Eggress pins in `rust/Cargo.toml` from `=1.0.8` to `=1.0.10` as one family. Refresh `rust/Cargo.lock` narrowly. Verify every live `eggress-*` package resolves from registry/crates.io rather than git/path sources.

Do not accept mixed 1.0.8/1.0.10 Eggress packages unless Cargo proves an unavoidable external semver edge. If mixed versions appear, identify the exact owner and stop before closing.

### Preserve the production constructor and ownership split

Compile unchanged source first. The desired path remains:

`ProviderHttpClient -> Eggfetch Client -> EggressDialer -> OutboundConnector::connect_tcp_detailed -> raw routed stream -> Eggfetch provider/origin TLS + HTTP/1.1`.

EggPool currently discards `OutboundInfo`; do not add socket-metadata plumbing without a consumer. If 1.0.10 requires code changes, keep them at the existing provider adapter boundary. Do not introduce `eggress-embed`, runtime/listeners, a second timeout owner, or local proxy protocol code.

### Feature-graph qualification

Under default features preserve the intended outbound profile: `pproxy-compat`, `pproxy-legacy`, `legacy-crypto`/`extended`, plus SSH. Under `--no-default-features`, prove direct/non-SSH proxy construction remains and SSH is absent/rejected before dial.

Explicitly inspect whether 1.0.10 changes the necessity of the current `eggress-server/ssh` test-support coupling. Preserve it by default. Remove/alter it only if default + no-default/test-support compilation and provider fixtures prove the upstream cfg/arity issue is gone.

Do not enable `toml`, `udp`, `quic`, `insecure-tls`, or service/runtime features on `eggress-outbound`.

### Upstream-delta-focused regression evidence

Reuse existing `provider_transport` fixtures. Add only narrow coverage missing for 1.0.10 intersections:

1. Two proxied account clients using the same proxy expression remain isolated; failure/cancellation on one does not poison the other.
2. Default-build SSH cancellation followed by same-client recovery remains successful and never reaches the provider by direct fallback.
3. A multi-hop route retains target integrity and one provider submission.
4. Route TLS trust failure remains `ProxyConnect`, never origin `Tls`.
5. Proxy authentication, proxy target refusal, closed proxy endpoint, blackholed route, malformed expression, and cancellation retain stable Plan-243 outcomes.
6. Secret redaction assertions cover Display/Debug/source-chain surfaces exposed by EggPool.

Do not add H2 proxy capability solely because upstream changed H2 pooling. If an already documented/qualified EggPool expression exercises H2, requalify it; otherwise record H2 as non-consumed upstream machinery.

### Timeout ownership

Continue using Eggfetch's connect timeout around the custom dialer/origin-connect phase. Do not layer `connect_tcp_timeout_detailed()` on top. `ProxyConnectTimeout` on a proxied Eggfetch connect deadline means connection establishment timed out while using a proxy route; it is not proof that the proxy alone timed out.

### Dependency/footprint evidence

Record immediate pre-change and post-change values on the same host/toolchain/target/profile:

- `Cargo.lock` package count;
- `cargo tree -e no-dev -p eggpool` node/line count;
- enabled Eggress feature set;
- resolved Eggress family/version set;
- locked release `eggpool` executable bytes.

Use the current main 1.0.8 build as baseline. Do not substitute Plan 243's historical artifact size because later repository changes have altered the binary/dependency baseline.

Any material dependency/artifact increase needs a named owner/explanation. A small neutral increase is acceptable; footprint reduction is not itself an acceptance criterion.

### Current-authority documentation

After qualification update current-state 1.0.8 claims in `README.md`, `rust/README.md`, `docs/proxy.md`, `architecture/overview.md`, `architecture/deep-dive-providers.md`, `AGENTS.md`, and relevant `.opencode/skills` files.

If `plans/000-long-term-specification.md` embeds literal `Eggress 1.0.8` in the unchanged no-SSH-fallback invariant, de-version only that statement to the exact-pinned Eggress outbound owner (or equivalent durable wording). This is a factual durability correction under the user-directed maintenance line, not an ownership change. Historical Plan-243 evidence stays immutable.

## 7. Ordered work packages

### Work package A — Rebase and freeze the immediate 1.0.8 baseline

Intent: avoid stale comparisons and coordinate with M001.

Required changes: none.

Acceptance evidence: current execution SHA; whether M001 has landed/closed; toolchain/target/profile; current package count, non-dev graph count, Eggress feature set, release binary bytes.

### Work package B — Upgrade exact Eggress family

Intent: consume published 1.0.10 as an ordinary downstream.

Required changes: `rust/Cargo.toml` pins + narrow `rust/Cargo.lock` refresh.

Acceptance evidence: all live Eggress packages resolve 1.0.10 from registry sources; no unexplained 1.0.8 residue or unrelated version churn.

### Work package C — Compile and feature-contract gate

Intent: prove existing public production/test seams before changing source.

Required changes: minimal adapter compatibility fix only if compilation proves it necessary.

Acceptance evidence: default, `test-support`, and no-default check/Clippy compile; production remains on `eggress-outbound`; SSH behavior matches §4; no forbidden outbound feature selected.

### Work package D — Focused provider transport requalification

Intent: prove real Eggfetch/Eggress behavior.

Required changes: narrow regression coverage only where current fixtures miss §6 evidence.

Acceptance evidence: `provider_transport` passes default/test-support/no-default; typed failures, multi-hop target integrity, cancellation/recovery, redaction, and account-client isolation are explicit.

### Work package E — Coordinator/wire non-regression

Intent: prove no hidden replay or application-policy change.

Acceptance evidence: coordinator C008/C009/C011, boundaries, finalization, publication, and `wire_runtime` pass; one attempt still yields no more than one upstream submission.

### Work package F — Graph/security/footprint/full gates

Intent: qualify the dependency as shipped.

Acceptance evidence: feature/no-dev/duplicate inspection; `cargo deny`; locked release build; pre/post measurements; strict default/no-default Clippy; serial workspace suite; hosted CI/dependency audit green on exact candidate.

### Work package G — Documentation and closure

Intent: make current authority truthful and close by evidence.

Acceptance evidence: no stale current-state 1.0.8 claims outside immutable history; `plans/closure/provider-transport/003-status.md` accepted; registry/roadmap transition performed; blocked-work audit performed.

## 8. Failure, cancellation, restart, contention semantics

No new state machine is introduced.

- Proxy construction failure remains fail-fast during generation/client-pool construction; no partial proxied client publication.
- Dial/handshake failure remains one failed provider attempt with no direct fallback.
- Cancellation remains future drop/transport cancellation, not an Eggress retry; subsequent same-client use must recover.
- Retiring a generation removes pool-owned topology while already-cloned clients may finish under their lease.
- Concurrent accounts never share an EggPool client/pool object merely because proxy strings are equal.
- No proxy state persists across restart.
- If 1.0.10 internally reuses SSH/H2 physical sessions, reuse must stay bounded by upstream route/TLS identity and may not violate EggPool account-client trust assumptions; downstream isolation/recovery tests are the evidence.

## 9. Compatibility and migration

No config, database, API, or wire migration.

Existing proxy URL precedence/syntax remains unchanged. Existing 1.0.8 configs should construct identically on 1.0.10 unless upstream fixed a concrete security/correctness defect; any behavior change must be documented with the exact non-secret expression shape and treated as a compatibility finding before closure.

Rollback before release is an exact family pin/lockfile revert to 1.0.8. Do not keep mixed versions or shims merely to avoid clean rollback.

## 10. Required tests

Focused provider transport:

```bash
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --no-default-features --test provider_transport -- --test-threads=1
```

Focused coordinator/wire:

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
```

Where cancellation fixtures are touched, synchronize on observable accepted-TCP/handshake/origin gates. Repeat originally flaky focused tests at least 100 times before claiming a regression fixed. Do not inflate arbitrary sleeps/timeouts.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --no-default-features -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -p eggpool -e no-dev
cargo tree --manifest-path rust/Cargo.toml -p eggpool -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo tree --manifest-path rust/Cargo.toml -i eggress-outbound
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
```

Also run §10 targets. Closure records commands actually run/results; do not convert this checklist into presumed evidence.

## 12. Documentation updates

- `README.md`, `rust/README.md`, `docs/proxy.md`: live version/feature/no-default claims.
- `architecture/overview.md`, `architecture/deep-dive-providers.md`: current listener-free boundary plus dated 1.0.10 qualification evidence.
- `AGENTS.md` and relevant `.opencode/skills`: exact-pin/feature guidance.
- `plans/000-long-term-specification.md`: de-version only the unchanged literal 1.0.8 owner invariant if present.
- roadmap/registry and `plans/closure/provider-transport/003-status.md` at closure.

## 13. Acceptance criteria

1. Every live Eggress family pin consumed by EggPool resolves to 1.0.10 from registry sources with no unexpected mixed-version residue.
2. Production still depends directly on `eggress-outbound`, not `eggress-embed`/runtime/server.
3. Default/no-default feature contracts remain documented behavior.
4. Typed route errors, fail-closed routing, proxy/origin TLS separation, multi-hop target integrity, cancellation recovery, redaction, and account isolation pass focused tests.
5. No hidden retry/additional provider submission appears.
6. `cargo deny`, locked release build, strict Clippy, serial workspace tests, and documentation validators pass.
7. Immediate pre/post graph/artifact deltas are recorded/explained.
8. Current docs describe 1.0.10 while historical Plan-243 evidence stays untouched.
9. Closure record is accepted; implementation landing alone does not close M003.

## 14. Stop conditions

Stop rather than improvise if:

- 1.0.10 requires `eggress-embed`/runtime/listener ownership in production;
- the typed detailed-connect surface is removed/materially changes semantics;
- a supported current proxy expression regresses without an understood upstream correctness reason;
- default/no-default SSH capability cannot be preserved coherently;
- proxy failure can fall back direct, duplicate provider submission, or cross account/trust boundaries;
- insecure/QUIC/UDP/runtime features enter production unexpectedly;
- source changes expand beyond provider adapter compatibility into retry/routing policy;
- `cargo deny` reveals unresolved high-severity advisory requiring a separate decision;
- lockfile upgrade requires unrelated dependency churn that cannot be isolated.

## 15. Closure evidence required

`plans/closure/provider-transport/003-status.md` must contain:

- implementation/final SHAs and M001 coordination/rebase state;
- exact resolved Eggress package/version/source set;
- default/test-support/no-default feature graph evidence;
- whether `eggress-server/ssh` test-support coupling remained necessary;
- `provider_transport` results for relevant profiles;
- explicit authentication, target rejection, closed endpoint, route TLS, timeout, malformed config, multi-hop, cancellation/recovery, redaction, account-isolation evidence;
- coordinator/wire focused results and one-attempt/one-submission evidence;
- `cargo deny`, strict Clippy, no-default, serial workspace, hosted CI/dependency-audit results;
- immediate pre/post package count, non-dev tree count, release binary bytes, and material-delta explanation;
- current-authority docs updated;
- deviations, unresolved findings with severity, disposition;
- registry blocked-work audit, including M004 promotion only if its hard dependencies are closed.

## 16. Handoff notes

Compile unchanged source immediately after pin/lock update; do not adapt preemptively to upstream internals. Preserve unrelated user changes. Keep integration tests serial. M001 is a soft coordination dependency only: rebase if it lands first, but do not merge its trailer/pool-classification scope into M003. The private custom-root test seam may remain awkward; M003 does not require upstream API work to simplify it.
