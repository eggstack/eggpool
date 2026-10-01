# Provider Transport Milestone 005 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/provider-transport/005-eggfetch-0.2.1-eggress-1.0.11-refresh.md`

Source subsystem roadmap:

- `plans/subsystems/provider-transport-roadmap.md#milestone-005--eggfetch-021-and-eggress-1011-dependency-refresh`

Repository baseline reviewed: `c79d21e0c2b9d851124d4ecfad6fd37eab592d0d`

The plan's product baseline `869964c236cfd985a771beeb0ee815df3e1cf601` has
the same Rust manifest, lockfile, and runtime source; the intervening commits
only registered M005 and updated its planning documents. Baseline measurements
were captured from the reviewed checkout with Rust 1.98.1 / Cargo 1.98.1 on
the same host and release profile as the candidate.

Implementation commits:

- `5ada181` — mark Provider Transport M005 active.
- `e08c0e25dfeafac3b243bf114a0cd19f0107ddf8` — refresh dependencies, update
  current-authority documentation, and enter closing.
- `7715a448d579d2e824d0111d47f9baa113536cc6` — make the SQLite overflow test
  assert its typed source instead of toolchain-specific display text.
- Closure/status transition is recorded by the commit containing this record.

## 1. Executive finding

M005 is complete. Eggfetch core and its HTTP CONNECT primitive resolve at
0.2.1; the complete resolved Eggress family is 1.0.11; EggServe remains
0.4.0. The refresh required no runtime adapter or policy changes. Provider
transport, focused consumers, both serial workspace matrices, dependency
policy, release build, tooling, validators, hosted CI, and hosted dependency
audit passed. The existing listener-free outbound, feature, fail-closed,
typed-error, TLS-ownership, cancellation, and account-isolation contracts
remain in place.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Exact pins and resolved versions | `rust/Cargo.toml`, `rust/Cargo.lock`, Cargo inverse trees | pass | Eggfetch core and CONNECT 0.2.1; every resolved `eggress-*` package 1.0.11. |
| EggServe remains unchanged | `rust/Cargo.toml`, lock entry, provider graph | pass | `eggserve-server 0.4.0`, `tower` feature. |
| Production remains listener-free | default `cargo tree -e features`, inverse trees | pass | `eggress-outbound` is production owner; no `eggress-embed` package; `eggress-server` is absent from the default graph and appears only with `test-support`. |
| Eggfetch feature containment | default, `test-support`, and no-default feature trees | pass | `native-http1,tls-rustls` plus required `advanced-routing`; no high-level `http1`, `standard-http1`, retry, redirect, Basic auth, built-in proxy, compression, HTTP/2, or HTTP/3 feature. |
| Provider transport behavior | `provider_transport` under default, `test-support`, and no-default | pass | 35, 41, and 36 tests passed, respectively. Existing fixtures cover direct/TLS, HTTP CONNECT/SOCKS/chains, encrypted routes, Trojan/SSH, authentication/refusal, timeouts, cancellation/recovery, redaction, pool reuse/isolation, and fail-closed behavior. |
| Coordinator and wire invariants | C008/C009/C011, boundaries, finalization, publication, `wire_runtime` | pass | 89 focused tests passed; no retry, routing, diagnostic-policy, or wire behavior changes. |
| Strict build/lint/test gates | fmt, default Clippy, no-default check/Clippy, full default/no-default workspace suites | pass | 801 default tests and 802 no-default tests passed across 65 suites each. |
| Dependency policy | local `cargo deny`; hosted Dependency audit run | pass | Existing duplicate-version warnings were reported by local `cargo deny`; advisories, bans, licenses, and sources passed. Hosted all-features dependency audit passed. |
| Comparable dependency/artifact measurements | baseline and candidate Cargo trees, lockfiles, release binaries | pass | Lock package entries 383 → 383; non-dev tree lines 970 → 970; binary 30,253,544 → 30,214,616 bytes (−38,928 bytes, about −0.13%). |
| Current-authority documentation | `AGENTS.md`, skills, architecture, proxy docs, Rust README, provider roadmap | pass | Live versions now state Eggfetch 0.2.1 / Eggress 1.0.11; historical M003/legacy evidence remains historical. |
| Hosted gates | CI run `36877677852`; Dependency audit run `36872691423` | pass | CI passed on test-portability follow-up `7715a448`; dependency audit passed for the unchanged manifest/lockfile on `e08c0e25`. |
| Blocked-plan audit | provider roadmap and `plans/registry.md` | pass | M002 remains blocked on the still-unpublished typed Eggfetch classification API; no future plan was unblocked by this release. |

## 3. Production implementation evidence

`rust/Cargo.toml` exact-pins `eggfetch-core =0.2.1` with the unchanged
`native-http1,tls-rustls` profile and all direct Eggress runtime/test packages
to `=1.0.11`. `rust/Cargo.lock` resolves `eggfetch-http-connect 0.2.1` through
Eggress HTTP CONNECT support, all present Eggress packages to 1.0.11, and
`eggserve-server 0.4.0` unchanged. No API or runtime implementation changed;
the only Rust source edit updates the provider module's dependency-version
comment.

The manifest and lock diff is limited to the intended Eggfetch/Eggress package
families plus Cargo's resolution of `socket2 0.5.10` and `windows-sys
0.52.0`. Eggress 1.0.11's `eggress-core` newly depends on `socket2 0.5`; Cargo
coalesced Hyper-util onto that compatible version and selected the matching
Windows target dependency for existing broad `windows-sys` ranges. These are
resolver edges attributable to the new Eggress graph, not unrelated package
upgrades. The lockfile package-entry count did not change.

Upstream release facts:

- Eggfetch [v0.2.1](https://github.com/eggstack/eggfetch/releases/tag/v0.2.1)
  states public APIs, runtime behavior, feature graph/defaults, and Rust 1.89
  MSRV are unchanged from 0.2.0. It publishes `eggfetch-core` and
  `eggfetch-http-connect` 0.2.1.
- Eggress [v1.0.10 to v1.0.11 comparison](https://github.com/eggstack/eggress/compare/v1.0.10...v1.0.11)
  contains 23 commits, including the parser/validation, timeout, redaction,
  HTTP proxy, and SSH/session changes called out in the plan and qualified by
  EggPool's provider fixtures.
- EggServe's [latest release listing](https://github.com/eggstack/eggserve/releases)
  still identifies 0.4.0 as latest; no downstream server dependency was
  changed.

## 4. Verification executed

### Commands run

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
cargo test --manifest-path rust/Cargo.toml --test database_compatibility journal_size_limit_rejects_values_outside_sqlite_integer_range -- --test-threads=1

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
cargo tree --manifest-path rust/Cargo.toml -i eggress-server
cargo tree --manifest-path rust/Cargo.toml -i eggress-embed

uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
```

### Results

- Formatting passed; default Clippy passed without warnings; no-default check
  and strict Clippy passed.
- Provider transport passed with 35 default, 41 `test-support`, and 36
  no-default tests.
- Focused C008/C009/C011, boundary, finalization, publication, and wire-runtime
  suites passed (89 tests total).
- Full default workspace passed 801 tests across 65 suites. Full no-default
  workspace passed 802 tests across 65 suites. Both used serial test threads.
- `cargo deny check` passed advisories, bans, licenses, and sources. Its output
  also listed duplicate-version warnings, reviewed by the lockfile/duplicate
  tree inspection; no new Eggress release-family split remains.
- Locked release build passed. `cargo tree -i eggress-server` and
  `cargo tree -i eggress-embed` returned the expected absent-package result in
  the default production graph; the `test-support` feature tree contains
  `eggress-server 1.0.11`.
- Tooling passed: Ruff format/check and Pyright were clean; pytest reported
  126 passed, 1 skipped; both release documentation and runtime package
  boundary validators passed.
- Hosted CI run
  [36877677852](https://github.com/eggstack/eggpool/actions/runs/36877677852)
  passed on the test portability correction. The earlier run and its retry
  ([36872691576](https://github.com/eggstack/eggpool/actions/runs/36872691576),
  attempts 1 and 2) exposed the display-text-dependent SQLite assertion and
  failed; the failure was corrected by `7715a448`. Hosted dependency audit
  [36872691423](https://github.com/eggstack/eggpool/actions/runs/36872691423)
  passed on the dependency implementation commit; the follow-up changed tests
  only and left the audited manifest and lockfile unchanged.

Comparable footprint (Rust/Cargo 1.98.1, same host, optimized release):

| Measurement | Baseline | Candidate | Delta |
|---|---:|---:|---:|
| Cargo.lock package entries | 383 | 383 | 0 |
| `cargo tree -e no-dev` lines | 970 | 970 | 0 |
| `rust/target/release/eggpool` bytes | 30,253,544 | 30,214,616 | −38,928 (−0.13%) |

The binary is slightly smaller; there is no material dependency-tree or
artifact increase to explain.

## 5. Invariant review

- One attempt/one submission and policy-neutral failure handling: focused
  coordinator suites passed; no coordinator source changed.
- HTTP/1.1 native Eggfetch profile and custom Eggress dialer: feature trees
  show `native-http1,tls-rustls` and required `advanced-routing`; prohibited
  high-level features are absent.
- Fail-closed proxy behavior, typed kind/stage classification, route/origin
  TLS separation, timeouts, cancellation recovery, and auth/refusal classes:
  provider real-socket suites passed in all three profiles.
- Account/client pool isolation and response framing: provider suite and wire
  runtime passed; no pool or body implementation changed.
- SSH capability split: default and `test-support` passed; full no-default
  matrix passed with reduced feature set.
- Listener-free production boundary: default dependency graph includes
  `eggress-outbound`; it excludes `eggress-embed` and optional test-only
  `eggress-server`.
- Credentials and dynamic route material remain excluded from diagnostics;
  existing redaction fixtures passed.

## 6. Failure and recovery review

No runtime owner, task, retry, lock, queue, persistence, or restart/reload
behavior was introduced. Provider fixtures passed authentication/refusal,
malformed and unsupported route fail-closed behavior, route/origin TLS
separation, connect/DNS/blackhole timeout classification, cancellation and
client recovery, pool reuse/isolation, and diagnostic redaction. No direct
fallback was added after a configured route failure.

## 7. Migration and compatibility review

No database, config, API, wire, or persisted-data migration is required. Exact
pins remain exact; EggServe 0.4.0 and downstream HTTP ownership are unchanged.
The new registry crates compiled against the existing adapter without source
compatibility changes.

The only non-transport code deviation was the test-only typed-error assertion
correction described above. No runtime or database behavior changed.

The first hosted CI run and its retry on implementation commit `e08c0e25`
failed the existing `database_compatibility::journal_size_limit_rejects_…`
assertion because it matched `TryFromIntError` display text. The test passed
on the prior hosted baseline and in local default and no-default profiles.
Follow-up commit
`7715a448` changed only the test assertion to downcast the preserved
`TryFromIntError` source, retaining the same overflow behavior check while
removing compiler/toolchain-specific display text. The targeted test and full
local default/no-default suites pass with that correction. Hosted CI run
[36877677852](https://github.com/eggstack/eggpool/actions/runs/36877677852)
passed all configured steps on `7715a448`.

## 8. Security review

The Eggfetch feature recipe remains bounded to native HTTP/1 and Rustls TLS;
the Eggress production dependency remains listener-free outbound. Real-socket
tests passed proxy authentication/refusal, malformed-route fail-closed,
timeout, route TLS, and redaction cases. Local and hosted dependency policy
checks passed. No credentials, raw bodies, or route secrets were added to
logs, persistence, or diagnostics.

## 9. Documentation and operations

Updated live-version statements in `AGENTS.md`, the architecture,
development, and documentation skills, `architecture/overview.md`,
`architecture/deep-dive-core.md`, `architecture/deep-dive-providers.md`,
`docs/proxy.md`, `rust/README.md`, and the provider-transport roadmap. The
provider module comment now names the qualified dependency versions. Historical
Plan 241/243 and closed M003 evidence remain unchanged. Release documentation
and package-boundary validators passed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No unresolved M005 findings. | None. | None. |

## 11. Roadmap disposition

**Milestone closed; no next dependency is promoted.** Provider Transport M002
remains blocked because Eggfetch 0.2.1 explicitly preserves its public API and
does not publish the general typed error-classification interface M002 needs.
M005 does not satisfy or weaken that separate blocker. The provider-transport
roadmap remains active only for this independently blocked M002 workstream.

## 12. Registry updates

`plans/registry.md` marks M005 closed, removes it from dependency-ready work,
records it as recently closed, and retains M002 in the blocked-work table. The
roadmap milestone table marks M005 closed with this closure record. No blocked
future plan became ready, so no plan was promoted.
