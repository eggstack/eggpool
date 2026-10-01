# Provider Transport Milestone 005 — Eggfetch 0.2.1 and Eggress 1.0.11 Dependency Refresh

Status: ready

Repository baseline: `869964c236cfd985a771beeb0ee815df3e1cf601`

Source roadmap:

- `plans/subsystems/provider-transport-roadmap.md#milestone-005--eggfetch-021-and-eggress-1011-dependency-refresh`

Long-term requirements:

- `plans/000-long-term-specification.md` — one bounded provider HTTP transport owner; no hidden retry/fallback owner.
- `plans/001-terminology-and-domain-model.md` — provider/account/runtime-generation identity and ownership remain authoritative.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — dependency qualification is sustaining transport work.
- `plans/003-planning-process.md` — bounded implementation/closure evidence and registry lifecycle.

Applicable ADRs:

- None required. This milestone preserves the durable Eggfetch/Eggress selection and provider-transport ownership established by prior work. Stop and reassess the ADR threshold if implementation would change ownership, enable a new protocol, or alter public compatibility semantics.

Primary class: infrastructure

## 1. Objective

Refresh EggPool's provider-side networking dependencies to the latest published
patch lines at the planning baseline:

- exact-pin `eggfetch-core` from `=0.2.0` to `=0.2.1`;
- resolve the Eggress-consumed `eggfetch-http-connect` package from 0.2.0 to 0.2.1;
- exact-pin every direct Eggress package used by EggPool from `=1.0.10` to `=1.0.11`, and resolve the complete live Eggress lockfile family coherently at 1.0.11;
- leave `eggserve-server =0.4.0` unchanged because 0.4.0 is already the latest published EggServe server line at this baseline.

The outcome is a dependency refresh plus requalification, not a provider
transport redesign. No API surface, routing policy, retry policy, wire
semantics, or user-visible proxy capability may regress.

## 2. Why this milestone is ready

Provider transport M001, M003, and M004 are closed. Their final qualified
boundary is therefore available as the compatibility target for this refresh.

The required upstream artifacts are published:

- Eggfetch v0.2.1 publishes `eggfetch-core 0.2.1` and `eggfetch-http-connect 0.2.1`. Its release notes explicitly state that public Rust/Python/C/CLI/HTTPX APIs, runtime/user-visible behavior, feature graph/defaults, and Rust 1.89 MSRV are unchanged from 0.2.0.
- Eggress v1.0.11 publishes the 1.0.11 workspace family. The v1.0.10..v1.0.11 range contains substantive fixes in code reachable by EggPool's outbound/pproxy path, including fail-closed parsing/validation, redaction, connection/DNS timeout behavior, HTTP proxy bounds, SSH compatibility/session scoping, and protocol handling.
- EggServe v0.4.0 remains current; no server-transport dependency change is required.

M002 is not a hard dependency. Eggfetch 0.2.1 intentionally does not add the
general-purpose typed transport classification API M002 requires, so M002
remains blocked before and after this milestone.

## 3. Current implementation evidence

At the baseline:

- `rust/Cargo.toml` exact-pins `eggfetch-core =0.2.0` with
  `default-features = false`, `native-http1`, and `tls-rustls`.
- The direct Eggress family in `rust/Cargo.toml` is exact-pinned at 1.0.10:
  `eggress-outbound`, optional `eggress-core`, `eggress-config`,
  `eggress-pproxy-compat`, `eggress-server`, `eggress-uri`, plus
  dev-only `eggress-protocol-shadowsocks` and
  `eggress-protocol-trojan`.
- `rust/Cargo.lock` resolves `eggfetch-core 0.2.0`,
  `eggfetch-http-connect 0.2.0`, and the complete live Eggress family at
  1.0.10.
- Production provider transport in `rust/src/providers/transport.rs` uses
  Eggfetch native HTTP/1 with a custom Eggress dialer. Eggress owns routed byte
  establishment; Eggfetch owns origin HTTP/TLS.
- Production uses listener-free `eggress-outbound` and
  `connect_tcp_detailed`; `eggress-server` is optional test support, not a
  production listener owner.
- Root `ssh` forwards to `eggress-outbound/ssh` plus
  `eggress-pproxy-compat/ssh`. `--no-default-features` retains direct and
  non-SSH proxy support and rejects SSH proxy configuration before dialing.
- `rust/tests/provider_transport.rs` already covers direct HTTP/HTTPS,
  keepalive/pool pressure, cancellation recovery, body bounds, direct/proxied
  client isolation, HTTP CONNECT, SOCKS4/5, chained routes, encrypted proxies,
  Trojan, SSH, authentication/refusal, route TLS failure, timeout
  classification, malformed proxy fail-closed behavior, redaction, and the
  no-SSH capability path.
- `eggserve-server =0.4.0` with `tower` is a separate downstream server
  boundary and is already current.

Repository documentation still truthfully describes the live 0.2.0/1.0.10
baseline. Do not update those current-version claims until the dependency
candidate has passed qualification.

## 4. Invariants that must not regress

- One coordinator attempt produces at most one provider transport submission.
- Provider transport remains HTTP/1.1 only.
- Eggfetch high-level URL/retry/redirect/Basic-auth/built-in-proxy and HTTP/2/3
  features remain disabled.
- The custom Eggress `Dialer` path retains Eggfetch advanced routing needed by
  `native-http1`.
- Configured proxy routes fail closed; no route failure may fall back to direct.
- Direct and proxied accounts retain separate Eggfetch client/pool ownership.
- Identical proxy URIs across accounts do not merge client/pool identity.
- Eggress establishes the route; Eggfetch performs origin TLS after route
  establishment. Proxy TLS failures remain distinct from origin TLS failures.
- `TransportError` remains the only stable transport error surface exposed
  above the provider adapter; M004 diagnostic labels remain static,
  secret-free, and policy-neutral.
- Default SSH and no-default non-SSH behavior remain identical to the current
  documented capability split.
- Provider response DATA remains incremental; trailer retention behavior from
  M001 is unchanged.
- Cancellation or dropped bodies release capacity and do not poison the client
  pool.
- Credentials, proxy secrets, raw request/response bodies, provider bodies,
  prompts, and dynamic route material remain absent from diagnostics.
- No production `eggress-embed`, listener/runtime facade, QUIC, H3, or UDP
  capability is introduced by feature unification.
- `eggserve-server 0.4.0` and downstream server semantics remain untouched.

## 5. Scope

### In scope

- Update the Eggfetch/Eggress exact pins in `rust/Cargo.toml`.
- Perform targeted Cargo resolution so `rust/Cargo.lock` moves the intended
  Eggfetch and Eggress packages to their latest patch lines without unrelated
  opportunistic dependency churn.
- Make the smallest compatibility-only source/test changes if the new registry
  crates require them.
- Requalify provider transport under default, `test-support`, and no-default
  profiles.
- Requalify the coordinator/wire surfaces that consume provider transport.
- Audit the resolved feature graph, duplicate graph, license/advisory policy,
  production listener-free boundary, and release artifact footprint.
- Update current-authority documentation/agent guidance only after the
  qualified dependency state is established.
- Write a closure record and reconcile roadmap/registry status.

### Explicitly out of scope

- Implementing M002 or inventing an EggPool-local typed Eggfetch taxonomy.
- Changing coordinator retry, failover, health, backoff, quarantine, or routing
  policy.
- Enabling Eggfetch high-level HTTP policy, compression, built-in proxy,
  HTTP/2, or HTTP/3.
- Moving updater HTTP onto Eggfetch.
- Enabling new Eggress protocols/capabilities merely because 1.0.11 contains
  them.
- Replacing `eggress-outbound` with `eggress-embed`, server/runtime, or a
  listener-owning facade.
- Refactoring the private custom-root test seam unless 1.0.11 requires a
  compatibility fix for the existing tests.
- Changing EggServe or downstream server transport.
- Broad Cargo dependency modernization unrelated to the targeted patch lines.
- Rewriting historical Plan 243, M003, or other closed evidence.

## 6. Required production changes

### 6.1 Manifest and lockfile

In `rust/Cargo.toml`:

- change `eggfetch-core = "=0.2.0"` to `=0.2.1` without changing its
  feature set;
- change all direct Eggress exact pins from 1.0.10 to 1.0.11, preserving
  `default-features`, optionality, and feature lists unless qualification
  proves a narrowly required compatibility correction.

Regenerate `rust/Cargo.lock` with targeted updates. The intended final graph
must resolve:

- `eggfetch-core 0.2.1`;
- `eggfetch-http-connect 0.2.1` where pulled transitively by Eggress HTTP
  protocol support;
- every resolved `eggress-*` package at 1.0.11;
- `eggserve-server 0.4.0` unchanged.

Do not accept unrelated lockfile churn without explaining why it is required by
these package resolutions.

### 6.2 Provider adapter compatibility

First compile and run focused tests with dependency-only changes. Do not
preemptively edit `rust/src/providers/transport.rs`.

If an API or semantic incompatibility appears, constrain fixes to preservation
of the existing adapter contract. In particular, retain
`connect_tcp_detailed` typed stage/kind mapping and the Eggfetch custom
`Dialer` boundary. Do not replace typed failures with message parsing.

### 6.3 Eggress 1.0.11 behavior-sensitive qualification

The upstream 1.0.11 delta includes changes relevant enough to require explicit
regression evidence around:

- pproxy URI/chain parsing, including chained routes and domain/IPv6 targets;
- fail-closed unsupported/malformed configuration behavior;
- proxy authentication and target-refusal classification;
- connection/DNS/blackhole timeout classification and bounded cancellation;
- route and diagnostic redaction;
- HTTP CONNECT request/response behavior and connection reuse;
- SSH authentication, compatibility translation, session/cache policy
  isolation, and cancellation recovery;
- proxy TLS versus origin TLS ownership;
- account/client pool isolation.

Use the existing real-socket provider fixtures; do not add a second transport
harness unless a specific untestable regression requires it.

### 6.4 Eggfetch 0.2.1 qualification

Treat Eggfetch 0.2.1 as a release-equivalent patch, but still prove the exact
consumer profile:

- `native-http1,tls-rustls` remains the selected feature recipe;
- no `http1` high-level alias, `standard-http1`, logical retry, redirects,
  Basic auth, built-in proxy, compression, HTTP/2, or HTTP/3 enters the graph;
- physical admission, connect/read/write timeout, cancellation, pooling, TLS,
  and response-frame behavior remain green.

The absence of an API/runtime change upstream does not waive EggPool's locked
build, dependency-policy, or provider integration gates.

### 6.5 Documentation and planning reconciliation

After qualification, update current live-version claims in:

- `AGENTS.md`;
- `.opencode/skills/architecture/SKILL.md`;
- `.opencode/skills/development/SKILL.md`;
- `architecture/overview.md` and `architecture/deep-dive-providers.md`;
- any current README/docs proxy/provider dependency statements found by a
  repository search;
- this roadmap and `plans/registry.md` during closure.

Do not rewrite closed M003/legacy evidence to say it tested 1.0.11.

## 7. Ordered work packages

### Work package A — Capture immediate baseline

Intent:

Create comparable pre-change graph/artifact evidence before the lockfile moves.

Required changes:

- no source changes;
- record the exact baseline commit/toolchain;
- build the locked release binary;
- record Cargo.lock package-entry count, non-dev dependency-tree size, relevant
  inverse trees, and release binary bytes;
- record current resolved Eggfetch/Eggress/EggServe package versions.

Acceptance evidence:

- reproducible baseline values suitable for immediate candidate comparison;
- baseline confirms `eggfetch-core 0.2.0`, `eggfetch-http-connect 0.2.0`,
  Eggress 1.0.10, and `eggserve-server 0.4.0`.

### Work package B — Perform targeted dependency refresh

Intent:

Move only the intended package families.

Required changes:

- update exact pins in `rust/Cargo.toml`;
- use targeted Cargo update/resolution for Eggfetch and Eggress;
- inspect the lockfile before any source adaptation.

Acceptance evidence:

- `cargo tree` shows `eggfetch-core 0.2.1`;
- `eggfetch-http-connect` resolves to 0.2.1;
- all resolved `eggress-*` packages are 1.0.11 and none remain at 1.0.10;
- `eggserve-server` remains 0.4.0;
- unrelated lockfile changes are absent or explicitly attributable.

### Work package C — Compile first, then adapt only if necessary

Intent:

Separate dependency compatibility from speculative source changes.

Required changes:

- run focused check/build/provider tests immediately after B;
- if failures are API/semantic incompatibilities, make the smallest
  compatibility-only changes in the existing adapter/test boundary;
- preserve typed route errors and exact feature containment.

Acceptance evidence:

- any source diff is directly tied to a demonstrated 0.2.1/1.0.11
  compatibility requirement;
- no retry/routing/wire/server policy change is mixed into the patch.

### Work package D — Requalify transport behavior

Intent:

Prove the upstream patch lines preserve EggPool's provider contract.

Required changes:

- execute `provider_transport` under default, `test-support`, and
  no-default profiles;
- pay special attention to chained routes, malformed routes, auth/refusal,
  route TLS, blackhole timeout, SSH, cancellation, pool reuse/isolation, and
  redaction tests;
- add a regression only when a 1.0.11 behavior change is not already covered.

Acceptance evidence:

- all existing supported route families remain green;
- configured routes remain fail closed;
- timeout/error categories remain stable;
- cancellation recovery and account isolation remain green;
- no secret-bearing diagnostics appear.

### Work package E — Requalify consumers and dependency boundaries

Intent:

Show that provider dependency changes do not leak into coordinator/wire policy
or production ownership.

Required changes:

- run focused coordinator C008/C009/C011, boundary, finalization, publication,
  and `wire_runtime` suites;
- inspect production Cargo feature/no-dev trees;
- prove `eggress-embed` and production `eggress-server` remain absent;
- inspect duplicate versions and `cargo deny`.

Acceptance evidence:

- one-attempt/one-submission semantics unchanged;
- M004 diagnostic classification remains policy-neutral;
- no unexpected Eggfetch/Eggress capability appears in production;
- dependency policy is green.

### Work package F — Measure, document, and close

Intent:

Turn the dependency candidate into auditable closure evidence.

Required changes:

- build the locked release artifact and compare package count, non-dev tree
  size, and binary bytes against A;
- explain any material delta;
- update current-authority version/feature documentation;
- write `plans/closure/provider-transport/005-status.md`;
- move M005 through `closing` to `closed` only after local and hosted
  evidence is available;
- audit M002 and leave it blocked unless a separately published upstream API
  now satisfies its stated blocker.

Acceptance evidence:

- closure matrix covers every acceptance criterion;
- hosted CI/dependency-audit results are recorded truthfully;
- roadmap/registry match the final disposition.

## 8. Failure, cancellation, restart, contention semantics

This milestone must not create new runtime owners, tasks, retries, locks,
queues, or persistence.

Dependency or compatibility failure must fail the build/test candidate rather
than trigger fallback to old source adapters. Runtime proxy establishment
continues to fail closed. A failed route must not retry direct networking.

Cancellation while waiting on physical capacity, proxy establishment, SSH,
origin TLS, request write, or response read must preserve existing bounded
cleanup and client recovery. Tests must synchronize on observable fixture
boundaries rather than fixed sleeps/yield counts.

No restart/reload semantics change. Runtime generation publication and client
pool ownership remain as currently documented.

## 9. Compatibility and migration

No data, database, config, API, or wire migration is expected.

Existing proxy URI/configuration syntax remains a compatibility surface for
EggPool. If Eggress 1.0.11 intentionally rejects input that EggPool currently
documents/supports, determine whether the input was previously invalid or
whether this is a consumer regression. Stop rather than silently weakening
validation or adding a local parser fork.

Exact pins are retained. Do not loosen versions to caret/range dependencies as
part of this milestone.

EggServe is a deliberate no-op in M005. If a newer EggServe server release is
published before implementation begins, stop and create/reconcile a separate
server-transport milestone rather than silently folding it into M005.

## 10. Required tests

Focused provider transport:

```bash
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --no-default-features --test provider_transport -- --test-threads=1
```

Focused consumers:

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
```

Reduced capability/full matrices:

```bash
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

Add or strengthen targeted regressions only for demonstrated gaps, especially
around 1.0.11 chain parsing, timeout classification, SSH/session isolation,
redaction, or fail-closed behavior. Do not duplicate already adequate fixture
coverage.

## 11. Required verification commands

Run from repository root and record actual results in closure:

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

For immediate pre/post evidence, also record:

- `rust/Cargo.lock` package-entry count;
- `cargo tree -e no-dev` line count;
- release `eggpool` binary bytes on the same host/toolchain/profile;
- resolved Eggfetch/Eggress/EggServe package/version/source set.

An inverse-tree command returning "package ID specification did not match any
packages" is the expected proof for an intentionally absent production package;
record it as such rather than treating it as a test failure.

## 12. Documentation updates

After qualification:

- update live Eggfetch/Eggress version literals in `AGENTS.md`,
  architecture/development skills, and current provider architecture docs;
- search README/docs/current guidance for stale 0.2.0/1.0.10 claims and update
  only live authority text;
- leave historical closure/legacy evidence unchanged;
- update the provider-transport roadmap current-state/status table;
- update `plans/registry.md` during status transitions;
- create `plans/closure/provider-transport/005-status.md`.

No documentation should imply that EggServe changed in this pass.

## 13. Acceptance criteria

1. `rust/Cargo.toml` exact-pins `eggfetch-core =0.2.1` with the same
   feature recipe and all direct Eggress packages at `=1.0.11`.
2. `rust/Cargo.lock` resolves `eggfetch-core 0.2.1`,
   `eggfetch-http-connect 0.2.1`, and every present `eggress-*` package at
   1.0.11, with no remaining 1.0.10 Eggress package.
3. `eggserve-server 0.4.0` remains unchanged.
4. Production remains on listener-free `eggress-outbound`; no
   `eggress-embed`/runtime/listener ownership enters the default graph.
5. Eggfetch remains native HTTP/1/custom-dialer with high-level
   retry/redirect/auth/proxy and HTTP/2/3 disabled.
6. Default/`test-support`/no-default provider transport suites pass, including
   route authentication/refusal, chain target integrity, TLS separation,
   timeout classification, cancellation recovery, redaction, and account
   isolation.
7. Focused coordinator/wire suites preserve one-attempt/one-submission,
   diagnostic policy neutrality, and downstream wire behavior.
8. Strict default/no-default Clippy/check, serial full workspace matrices,
   `cargo deny`, locked release build, tooling, and documentation/package
   validators pass.
9. Dependency graph and release artifact deltas are measured on a comparable
   baseline/candidate and any material increase is explained.
10. Current-authority docs describe the final versions accurately while closed
    M003/legacy records remain historical.
11. Hosted CI and dependency-audit evidence pass before M005 is marked closed.
12. M002 remains blocked unless a separate published Eggfetch API independently
    satisfies its taxonomy requirement.

## 14. Stop conditions

Stop and report rather than improvise if:

- Eggress 1.0.11 removes or materially changes the semantics of
  `OutboundConnector::connect_tcp_detailed` used by EggPool;
- the update requires production `eggress-embed`, server/runtime listener
  ownership, or a new proxy implementation in EggPool;
- fail-closed routing, typed route-error categories, route/origin TLS
  separation, SSH/no-default capability behavior, or account isolation cannot
  be preserved;
- the Eggfetch 0.2.1 consumer profile requires enabling high-level policy or a
  new protocol;
- compatibility requires parsing upstream error `Display` text;
- a supported current proxy expression regresses and the upstream change cannot
  be shown to reject previously invalid input;
- unrelated lockfile churn cannot be isolated from the requested patch refresh;
- `cargo deny` reveals an unresolved high-severity advisory requiring a
  separate dependency/security decision;
- a newer EggServe release appears and would need qualification; create a
  separate server-transport plan rather than broadening M005;
- source changes expand into coordinator routing/retry/health policy or wire
  adaptation.

## 15. Closure evidence required

`plans/closure/provider-transport/005-status.md` must contain:

- baseline and implementation/final commit SHAs;
- exact upstream release/tag facts used for Eggfetch 0.2.1, Eggress 1.0.11,
  and the unchanged EggServe 0.4.0 decision;
- exact resolved Eggfetch/Eggress/EggServe package/version/source set;
- manifest and lockfile diff characterization, including any non-target package
  changes and why they were necessary;
- default/`test-support`/no-default feature graph evidence;
- proof that production remains listener-free `eggress-outbound` and that
  `eggress-embed`/production server ownership is absent;
- provider transport results covering direct/TLS/pool/cancellation,
  HTTP CONNECT/SOCKS/chaining, encrypted proxies, Trojan, SSH,
  authentication/refusal, timeout, redaction, and account isolation;
- focused coordinator/wire results and one-attempt/one-submission evidence;
- default/no-default strict Clippy/check, serial workspace matrices,
  `cargo deny`, locked release build, tooling, release-doc and package-boundary
  validator results;
- immediate pre/post package count, non-dev tree line count, release binary
  bytes, and material-delta explanation;
- current-authority documentation updates;
- hosted CI and dependency-audit run identifiers/results;
- deviations and unresolved findings with severity;
- explicit disposition;
- unblock audit confirming whether M002 remains blocked.

## 16. Handoff notes

Start with dependency-only edits and compile before touching source. Preserve
unrelated user changes. Keep all Rust integration tests serial.

Eggfetch 0.2.1 should be low-risk by upstream contract, but do not treat that
as substitute evidence. Eggress 1.0.11 has a meaningful runtime delta and must
receive the full route/timeout/redaction/SSH qualification above.

Do not mechanically remove the test-support `eggress-server/ssh` alignment
because upstream internals changed; remove or alter it only if the resolved
feature graph and tests prove the existing cfg coupling is no longer needed.

Do not use M005 as a reason to unblock M002. The missing typed Eggfetch
classification API is a separate interface requirement.
