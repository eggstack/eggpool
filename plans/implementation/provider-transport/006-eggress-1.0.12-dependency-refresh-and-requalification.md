# Provider Transport Milestone 006 — Eggress 1.0.12 dependency refresh and requalification

Status: implemented

Repository baseline: `087366af4f9d89738715936a8b27026e2fad8c1a` (EggPool `main`, 2026-10-08; reconfirm at execution).

Source roadmap:

- `plans/subsystems/provider-transport-roadmap.md#milestone-006--eggress-1012-dependency-refresh-and-requalification`

Long-term requirements:

- `plans/000-long-term-specification.md` — provider/coordinator attempt boundary, stable transport contracts, secret-free evidence, and bounded runtime.
- `plans/001-terminology-and-domain-model.md` — account, request, generation, and provider-attempt ownership.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — dependency requalification within provider transport.
- `plans/003-planning-process.md` — scoped plan, qualification, and closure governance.

Applicable ADRs:

- None required: this is an exact-pinned patch of an existing durable Eggress dependency, without new transport ownership, protocol, authentication semantics, public contract, or platform target. Stop and reassess the ADR threshold if that ceases to be true.

Primary class: infrastructure

Hard dependencies: Provider Transport M001–M005 closed (including M002's subsequent Eggfetch 0.2.2 adoption); upstream Eggress 1.0.12 release/publication available, subject to exact registry resolution gate below.

Interface dependencies: unchanged Eggress `eggress-outbound`/`OutboundConnector`, typed `connect_tcp_detailed`, `eggress-server` test executor, and Eggfetch custom-`Dialer` interfaces.

Operational dependencies: hosted CI/dependency audit and local release-artifact qualification required **for closure**, not for preparing the patch.

## 1. Objective

Upgrade every Eggress crate used by EggPool from exact `=1.0.11` to exact `=1.0.12`, including optional/test-support and development dependencies; regenerate the lockfile with no unrelated dependency churn; requalify the current proxy feature graph, provider transport contracts, and release footprint. Preserve existing API, capabilities, error semantics, TLS ownership, and runtime behavior.

This is **not** an Eggfetch, EggServe, routing, protocol expansion, or performance optimization milestone.

## 2. Why this milestone is ready

- The current EggPool `rust/Cargo.toml` pins `eggress-outbound`, `eggress-core`, `eggress-config`, `eggress-pproxy-compat`, `eggress-server`, `eggress-uri`, and the development-time `eggress-protocol-shadowsocks`/`eggress-protocol-trojan` to `=1.0.11`.
- Upstream tags `v1.0.11...v1.0.12` compare as nine commits. The changed paths are release/version metadata, documentation and planning, plus `eggress-runtime/tests/reload.rs` (process-wide SIGHUP test serialization); no production Rust source changed in that tag range.
- Upstream `Cargo.lock` comparison retains 413 packages, with version changes confined to 29 Eggress workspace packages (1.0.11 → 1.0.12). This is an upstream observation, **not** proof of EggPool's downstream feature graph or binary size.
- Eggress `v1.0.12` is tagged and GitHub Release/CI published on 2026-10-08; upstream `AGENTS.md`/`docs/ROADMAP.md` report crates.io publication. **Confirm all required 1.0.12 Rust crates resolve from crates.io before implementation; never substitute a local path or git override to conceal a missing publication.**
- The previous M005 closure qualified the 1.0.11 outbound boundary, including default, `test-support`, and no-default profiles. M002 subsequently qualified Eggfetch 0.2.2 typed transport classification. Neither milestone needs reopening.

Upstream review authority: `https://github.com/eggstack/eggress/compare/v1.0.11...v1.0.12`; `https://github.com/eggstack/eggress/releases/tag/v1.0.12`.

## 3. Current implementation evidence

- `rust/src/providers/transport.rs` owns the thin `EggressDialer` that wraps `eggress_outbound::OutboundConnector::from_pproxy_uri` and `connect_tcp_detailed`, maps typed kind/stage into Eggfetch `DialError`, and never routes an unsuccessful proxied dial directly.
- `build_eggfetch_client` keeps origin TLS, HTTP/1.1 framing, timeout, physical admission, and pooling in `eggfetch-core =0.2.2`. Eggress owns proxy-hop protocols and route-level TLS. Per-account pools remain isolated.
- `rust/Cargo.toml` root `ssh` enables `eggress-outbound/ssh` and `eggress-pproxy-compat/ssh` independently; `test-support` enables `eggress-server/ssh` and optional test-only Eggress crates. `--no-default-features` intentionally rejects SSH configurations while retaining non-SSH routes.
- `rust/tests/provider_transport.rs` already covers HTTP CONNECT, SOCKS4/5, chains, credential refusal, target refusal, Shadowsocks/Trojan/SSH, timeouts, cancellation/recovery, redaction, TLS trust and connection-pool isolation. Reuse, rather than duplicate, these fixtures.
- `plans/closure/provider-transport/005-status.md` records the 1.0.11 qualification evidence and production `eggress-server`/`eggress-embed` exclusion. `plans/closure/provider-transport/002-status.md` records subsequent 0.2.2 Eggfetch adoption.
- Current live version mentions in `AGENTS.md`, `architecture/`, `docs/proxy.md`, `rust/README.md`, `rust/src/providers/transport.rs`, and development skills must be reconciled after successful upgrade. Historical M003/M005 plans and closure records are immutable.

## 4. Invariants that must not regress

- Exactly one upstream transport submission per coordinator attempt; no Eggfetch retry, redirects, auto-auth, compression, built-in proxy, or HTTP/2/3 activation.
- `eggress-outbound` remains listener-free production authority; no `eggress-embed`, `eggress-runtime`, production `eggress-server`, or unsolicited QUIC/UDP listener/runtime feature.
- Configured proxy routes always fail closed, with no bypass/direct fallback. `direct://` retains its explicit documented behavior.
- Identical supported pproxy URI families, parsing/validation, chain hop order, credentials, DNS target identity, proxy auth/refusal, and SSH-enabled/default vs SSH-disabled behavior.
- Distinct proxy-hop TLS and destination/origin TLS trust configuration, ALPN/verification, and error categories. TLS policy is never implicitly weakened.
- `OutboundConnectError.kind()/stage()` mappings, `TransportError` variants/diagnostic classes, retry and health-policy interpretations remain unchanged; no string-parsed classifier or secret-bearing diagnostic.
- Same account-scoped physical pool isolation, admission caps, timeouts, cancellation cleanup, request/body/trailer streaming, and finite/SSE terminal/replay ownership.
- Stable external API, config, database schema, process/restart/reload contracts, and existing Rust MSRV 1.89.
- No raw proxy URI, credential, prompt, provider body, or response trailer values in logs, test reports, or closure artifacts.

## 5. Scope

### In scope

- All direct `eggress-*` exact pins in `rust/Cargo.toml` and corresponding registry-resolved `rust/Cargo.lock` updates.
- Registry-publication gate, candidate resolver/feature-tree comparison, compile-first assessment, focused provider and coordinator/wire regression gates, advisory/license analysis, release-size comparison.
- Narrow production source correction **only if** a demonstrated 1.0.12 compilation/semantic incompatibility makes it essential. Otherwise, version comments/docs only.
- Reconcile live dependency-version authority after qualification; record closure in `plans/closure/provider-transport/006-status.md` and transition roadmap/registry accordingly.

### Explicitly out of scope

- Eggfetch 0.2.2, EggServe 0.4.0, Hyper/Rustls, new protocols, pproxy parity expansion, proxy listener ownership, WebSocket M003 upstream future API adoption, upstream changes, unrelated Cargo updates, new benchmarks, persistence or dashboard changes.
- Changing `eggress-outbound` feature flags, removing the test `eggress-server/ssh` coupling, or changing route/config behavior on speculation.
- Publishing a new EggPool release; normal packaging qualification is mandatory, publication is separately authorized.
- New ADR, schema migration, user-facing API change, or broader performance review.

## 6. Required production changes

1. Update all eight directly specified Eggress entries (six normal/optional, two dev) from `=1.0.11` to `=1.0.12` in `rust/Cargo.toml`. Preserve each dependency's optionality, `default-features`, and feature selectors.
2. Resolve **registry artifacts** and refresh `rust/Cargo.lock` with targeted Cargo operations (such as `cargo update --manifest-path rust/Cargo.toml -p eggress-outbound --precise 1.0.12` after editing manifests, allowing related exact-pinned Eggress packages to co-resolve). If targeted resolution fails, diagnose lockstep availability; do not run indiscriminate `cargo update` or add git/path patches.
3. Keep `eggfetch-core =0.2.2` (including transitive `eggfetch-http-connect 0.2.2`) and `eggserve-server =0.4.0` intact. Any non-Eggress lockfile changes need a package-by-package explanation and explicit review, not automatic acceptance.
4. Do not change `rust/src/providers/transport.rs` beyond the stale version comment unless a failing build or test demonstrates a needed compatibility correction. Preserve typed error translation, SSH flags, and the test-only `build_chain_executor` seam.
5. After passing qualification, update current-version claims in authoritative docs/skills (e.g., `AGENTS.md`, `architecture/deep-dive-providers.md`, `docs/proxy.md`, `rust/README.md`, `.opencode/skills/development/SKILL.md` and `.opencode/skills/architecture/SKILL.md` where applicable). Do not retroactively rewrite M003/M005 historical closure evidence. Reconcile the provider-transport roadmap's stale live Eggfetch 0.2.1 claim to actual 0.2.2 while preserving historical descriptions.

## 7. Ordered work packages

### A — Record baseline and registry availability

Intent: reproducible before/after comparison without confusing published tags with registry packages.

Required: record EggPool head, toolchain, full `cargo tree -e features` (default/no-default/`test-support`), `cargo tree -e no-dev`, Eggress inverse tree, `Cargo.lock` package count, locked release binary bytes on a single host/profile; verify exact 1.0.12 registry availability for all direct/transitive Eggress crates.

Acceptance: baseline files/evidence identified; no local path patches; absence of required crates is documented as a blocker rather than bypassed.

### B — Apply exact-pin and targeted lockfile refresh

Intent: make the smallest dependency-only patch.

Required: update `rust/Cargo.toml`; targeted Cargo resolution; inspect `git diff -- rust/Cargo.toml rust/Cargo.lock`; enumerate all `eggress-*` packages/versions and non-Eggress changes.

Acceptance: every resolved Eggress crate at 1.0.12, no 1.0.11 residual, crate provenance registry-based; Eggfetch 0.2.2 and EggServe 0.4.0 unchanged; no unreviewed graph churn.

### C — Compile first and check feature containment

Intent: discover any downstream cfg/signature regression without silently modifying interfaces.

Required: check default, no-default, and `test-support`; compare resulting feature graphs. Confirm upstream `ssh` and `pproxy-compat` split, test-server feature arity, disabled optional QUIC/UDP, absence of unexpected listener/runtime packages in normal production.

Acceptance: compile clean without source adaptation or each minimal source correction is backed by a concrete failing symbol/path, test, and review. No new production owner or feature is enabled.

### D — Requalify provider and consumer behavior

Intent: preserve exact behavior and failure categories rather than count passing tests alone.

Required: serial `provider_transport` in all three feature profiles, specifically malformed and authenticated routes, HTTP CONNECT/SOCKS4/SOCKS5, chains, Shadowsocks SSR/legacy, Trojan TLS, SSH default/no-default, proxy/origin TLS trust separation, timeout/refusal classification, cancellation/recovery, pool reuse/account isolation, and redaction. Run representative coordinator C008/C009/C011, boundary/finalization/publication and wire-runtime tests.

Acceptance: supported route capabilities and typed transport/evidence outputs unchanged; fail-closed behavior verified, including negative routes. Add only a narrowly targeted regression when existing coverage fails to detect a real compatibility issue.

### E — Whole-workspace, policy, and release-footprint gate

Intent: prove dependency safety and preserve resource usage across constrained deployments.

Required: fmt, strict Clippy/default/no-default, serial workspace tests default/no-default, `cargo deny`, dependency-tree duplicates/inverses, locked release build, before/after lock count/non-dev tree and binary size. Run existing runtime-package/release-doc validators when their owned files change, and hosted CI/dependency-audit.

Acceptance: complete pass or severity-tagged, understood deviations; no new advisory/license/source failure, no unexplained graph/size delta, no unexpected optional feature. A size delta alone is not grounds for redesign; material changes must be explained.

### F — Documentation and closure

Intent: leave an accurate dependency authority and bounded evidence record.

Required: update current-version docs/skills and comments after evidence; write `plans/closure/provider-transport/006-status.md`; mark M006 `closing` only after implementation has landed, `closed` only after local + hosted evidence and unblock audit. Reconcile `plans/subsystems/provider-transport-roadmap.md` and `plans/registry.md` in the closure transition.

Acceptance: traceable release input, tests, footprint, security and no-default evidence; no historical record rewritten; final status matches evidence.

## 8. Failure, cancellation, restart, contention semantics

The upgrade must not modify application runtime semantics. Invalid/missing proxy or SSH-disabled configuration is rejected before dialing; failed proxy establishment cannot trigger direct retries. Downstream cancellation while awaiting physical capacity, establishing TCP/proxy/SSH/origin TLS, writing, or reading must release resources under existing bounded cleanup and avoid a second upstream submission. Pooling is per account, SSH/H2 trust/session reuse remains policy-scoped upstream, and shutdown/reload/generation retirement contracts remain unchanged. Use observable fixture gates rather than fixed sleeps for cancellation tests; serialized Rust suites prevent shared-fixture contention.

## 9. Compatibility and migration

No config, DB, wire, API, provider profiles, installer, or release-version migration. Keep existing feature flags and exact dependency pins. Patch versions are internal implementation changes only; production binary exports and CLI controls should be identical. If the new version requires source/API/feature adaptation, isolate the smallest change; a substantial redesign, behavior difference, authentication/TLS weakening, or exposed protocol change is a stop condition requiring new scope.

## 10. Required tests

- `rust/tests/provider_transport.rs` default / `test-support` / no-default, including both happy and failing routes, typed error taxonomy, redaction, cancellation and account isolation.
- Existing focused `rust/tests/coordinator_c008.rs`, `coordinator_c009.rs`, `coordinator_c011.rs`, `coordinator_boundaries.rs`, `coordinator_finalization.rs`, `coordinator_publication.rs`, and `wire_runtime.rs`.
- Entire Rust workspace serial tests in both default and no-default profiles; locked production release build.
- Cargo feature and inverse-tree assertions (expected absence of `eggress-embed` and normal-production `eggress-server`), duplicate dependency review, license/advisory/source policy, artifact-size and dependency graph comparison.
- If a real defect is observed, add a deterministic regression in the owning existing target and rerun affected gates.

## 11. Required verification commands

From repository root, record baseline numbers before dependency edits, then execute after updating the lockfile:

~~~bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --features test-support
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --no-default-features --test provider_transport -- --test-threads=1
for test in coordinator_c008 coordinator_c009 coordinator_c011 coordinator_boundaries coordinator_finalization coordinator_publication wire_runtime; do
  cargo test --manifest-path rust/Cargo.toml --test "$test" -- --test-threads=1 || exit 1
done
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml -e features --no-default-features
cargo tree --manifest-path rust/Cargo.toml -e features --features test-support
cargo tree --manifest-path rust/Cargo.toml -e no-dev
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo tree --manifest-path rust/Cargo.toml -i eggress-outbound
cargo tree --manifest-path rust/Cargo.toml -i eggress-server --features test-support
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
~~~

For the default production inverse tree, `cargo tree ... -i eggress-server` and `-i eggress-embed` are **expected to report absence/nonzero**; do not mark their expected absence as a failed gate. Also inspect `rust/Cargo.lock` for **all** `eggress-*` entries, not only explicitly named crates. Run `uv run python scripts/validate_release_docs.py` and `uv run python scripts/validate_runtime_package_boundary.py` if owned documentation or packaging surfaces change. Record actual output and hosted run identifiers, not predicted results.

## 12. Documentation updates

- Live dependency/version claims: `AGENTS.md`, `architecture/deep-dive-providers.md`, `docs/proxy.md`, `rust/README.md`, relevant development/architecture skills, `rust/src/providers/transport.rs` comment.
- `plans/subsystems/provider-transport-roadmap.md` current-state and milestone status (historical M003/M005 statements retained with historical framing), `plans/registry.md`, final `plans/closure/provider-transport/006-status.md`.
- Do not touch immutable legacy Plans 215–220/241/243, completed M003/M005 plans/closure reports, or unrelated docs purely to normalize old version numbers.

## 13. Acceptance criteria

1. Full registry-resolved Eggress dependency family at `1.0.12` with no mixed `1.0.11`, path, or git override; exact constraints retained.
2. No unreviewed non-Eggress dependency drift; `eggfetch-core`/`eggfetch-http-connect 0.2.2` and `eggserve-server 0.4.0` remain pinned/resolved.
3. No production listener/runtime facade, unintended SSH/QUIC/UDP feature, or new transport policy owner.
4. All three provider test profiles and focused coordinator/wire regression suites pass; default and no-default workspace tests and strict lint/check pass.
5. Typed route/error/redaction, fail-closed, proxy/origin TLS separation, cancel/recover, connection/account pooling and one-submission invariants hold.
6. Cargo policy, reproducible locked release build, and comparable package/tree/binary footprint measurements recorded, deltas explained.
7. Live-version documentation accurate; hosted CI/audit evidence accepted; closure record accepted and M006 registered closed only then.

## 14. Stop conditions

Stop rather than improvise if:

- any required Eggress 1.0.12 package is unavailable/yanked in the registry or forces mixed Eggress major/minor/patch resolution;
- upstream package contents deviate materially from reviewed tag (code/API/feature/dependency changes) or the exact graph cannot be resolved;
- a required feature changes, `eggress-server/ssh` signature breaks, or production requires an unsolicited listener/runtime/QUIC/UDP graph;
- route failure becomes direct fallback, typed transport classes or TLS trust semantics regress, SSH no-default accepts what it previously rejected, or cancellation causes duplicate submits;
- unrelated dependency churn/advisory/license failures cannot be isolated, or artifact/graph changes are material and unexplained;
- patch demands changing provider/coordinator behavior, any persisted or public contract, or another repo; escalate to a separate reviewed corrective/ADR if appropriate.

## 15. Closure evidence required

`plans/closure/provider-transport/006-status.md` must record: baseline/candidate commit IDs and toolchain; exact upstream tag/release/registry-resolution evidence; manifest and Cargo.lock crate-version inventory and all non-Eggress deltas; default/no-default/`test-support` feature graph + listener absence; each focused/default/no-default test and strict lint result; dependency policy + hosted CI/audit links; observed fail-closed/auth/TLS/SSH/cancel/typed-error/redaction qualification; same-host baseline/candidate package counts, dependency-tree and release-binary size; changed-current-doc list; severity-tagged unresolved findings; no-migration conclusion; explicit `closed` / `conditionally closed` / `corrective pass required` / `blocked` disposition; dependency-unblock audit.

## 16. Handoff notes

Begin with a clean working tree and confirm `main`/branch baseline, Cargo registry state, and whether a different agent advanced M006. Prefer dependency-only changes and compile before touching `transport.rs`. Preserve unrelated user changes. Run network/proxy tests serial with `--test-threads=1`. Do not generate secrets or record raw credential-bearing URIs in artifacts. Do not replace missing registry releases with vendoring or local git dependencies. The separate upstream Eggress bounded WebSocket API plan is **not shipped by 1.0.12** and is out of scope. An EggPool release must not be tagged or published solely because this dependency bump closes.
