# Provider Transport Milestone 006 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/provider-transport/006-eggress-1.0.12-dependency-refresh-and-requalification.md`

Source subsystem roadmap:

- `plans/subsystems/provider-transport-roadmap.md#milestone-006--eggress-1012-dependency-refresh-and-requalification`

Repository baseline reviewed: `087366af4f9d89738715936a8b27026e2fad8c1a`

Implementation commits:

- `d8be1599` — refresh the exact-pinned Eggress family and lockfile; update current dependency authority; clear the strict-Clippy finding encountered during qualification.
- `3275bf96` — register M006 as active.
- `0562219d` — move M006 to closing while hosted evidence was collected.
- This closure/status commit — mark M006 and its roadmap closed.

## 1. Executive finding

M006 is complete. All 16 Eggress packages resolved from the crates.io registry
at 1.0.12, with no mixed 1.0.11 entries. Eggfetch core and its HTTP CONNECT
dependency remain 0.2.2; EggServe remains 0.4.0. Provider, coordinator, wire,
workspace, dependency-policy, release-build, feature-containment, and hosted
checks passed. No transport adapter or runtime policy change was required.

The release artifact grew by 520 bytes on the same host and release profile
(31,384,976 to 31,385,496 bytes, about 0.0017%). The lockfile retained 384
package entries and the production non-dev tree retained 975 lines. No
unrelated package version was upgraded.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| All direct and transitive Eggress crates resolve at 1.0.12 | `rust/Cargo.toml`, `rust/Cargo.lock`, crates.io `cargo info` for all eight direct declarations, lockfile audit | pass | All 16 locked `eggress-*` packages use 1.0.12 and registry sources; no 1.0.11 residue or path/git override. |
| Eggfetch and EggServe remain pinned | Manifest/lockfile and default production tree | pass | `eggfetch-core`/`eggfetch-http-connect` 0.2.2; `eggserve-server` 0.4.0. |
| Production transport remains listener-free and feature-bounded | Default/no-default/test-support feature trees and inverse trees | pass | `eggress-outbound` is present; `eggress-server` is absent from the default graph and appears at 1.0.12 only with `test-support`; `eggress-embed` is absent. No QUIC feature is enabled. The existing test-support extended server graph remains test-only. |
| Default, test-support, and no-default provider behavior | `provider_transport` in each feature profile | pass | 35, 41, and 36 tests passed. Existing tests cover supported routes, fail-closed errors, TLS separation, cancellation/recovery, redaction, and account-pool isolation. |
| Focused coordinator and wire consumers remain compatible | C008, C009, C011, boundaries, finalization, publication, `wire_runtime` | pass | 97 tests passed. |
| Full local workspace suites | Default and no-default serial workspace suites | pass | Default: 930 passed, 1 ignored. No-default: 931 passed, 1 ignored. Both covered 72 suites. |
| Strict formatting, Clippy, checks, and dependency policy | Local fmt, default/no-default Clippy, no-default check, `cargo deny` | pass | Clippy reported no issues after the small `ProviderAuthMode` derived-`Default` cleanup. `cargo deny` passed advisories, bans, licenses, and sources; it reported duplicate-version warnings for review. |
| Lockfile, dependency tree, and release footprint | Baseline worktree at `087366a` and candidate, both built with `--locked --release` on this host | pass | 384 lock entries before/after; 975 production non-dev tree lines before/after; binary +520 bytes (+0.0017%). The only non-Eggress lock diff was dependency-reference selection: `hyper-util` uses already-locked `socket2 0.6.5`, and target-only edges for `errno`, `rustix`, and `tempfile` use already-locked `windows-sys 0.61.2`. Their package versions did not change. |
| Hosted CI and dependency audit | [CI run 37810352765](https://github.com/eggstack/eggpool/actions/runs/37810352765), [Dependency audit 37810352762](https://github.com/eggstack/eggpool/actions/runs/37810352762) | pass | Both passed on pushed `main` head `0562219d`. CI included fmt, strict Clippy, no-default check/Clippy, serial workspace tests, Ruff, Pyright, and tooling pytest. |
| Current dependency authority and unblock audit | `AGENTS.md`, provider architecture/proxy/Rust docs and development skill; registry and roadmap dependency search | pass | Current Eggress references now say 1.0.12; historical M003/M005 evidence remains historical. No registered plan depends on M006. |

## 3. Production implementation evidence

All six runtime/optional and both development Eggress declarations in
`rust/Cargo.toml` now pin `=1.0.12`, preserving optionality, defaults, and
feature selectors. Cargo's targeted update resolved the complete 16-package
family in `rust/Cargo.lock`. No Eggress 1.0.11 package remains. Eggfetch,
EggServe, and all unrelated package versions are unchanged.

No provider transport source behavior needed adaptation. The only Rust source
change outside the dependency declarations is the semantics-preserving derived
`Default` for `ProviderAuthMode`: the required strict-Clippy gate surfaced this
current-tree cleanup during M006 qualification. Its crate suite passed 27
tests, followed by both full workspace suites on the final source tree.

The lockfile's non-Eggress diff changes four dependency references, not package
versions: `hyper-util` selects `socket2 0.6.5`; `errno`, `rustix`, and
`tempfile` select `windows-sys 0.61.2` for target-specific edges. These
already-locked compatible entries were selected by Cargo's targeted graph
resolution; lock entry count and the production non-dev tree line count stayed
constant. The release binary size increase is 520 bytes and is attributable
to the refreshed graph within the existing release profile; no feature or
transport redesign was made.

## 4. Verification executed

### Commands run

```bash
cargo info eggress-outbound@1.0.12
cargo info eggress-core@1.0.12
cargo info eggress-config@1.0.12
cargo info eggress-pproxy-compat@1.0.12
cargo info eggress-server@1.0.12
cargo info eggress-uri@1.0.12
cargo info eggress-protocol-shadowsocks@1.0.12
cargo info eggress-protocol-trojan@1.0.12
cargo update --manifest-path rust/Cargo.toml -p eggress-outbound --precise 1.0.12

cargo check --manifest-path rust/Cargo.toml --workspace --all-targets
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --features test-support
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo deny --manifest-path rust/Cargo.toml check

cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --no-default-features --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml -p eggpool-provider-profile
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1

cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml -e features --no-default-features
cargo tree --manifest-path rust/Cargo.toml -e features --features test-support
cargo tree --manifest-path rust/Cargo.toml -e no-dev
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo tree --manifest-path rust/Cargo.toml -i eggress-outbound
cargo tree --manifest-path rust/Cargo.toml --features test-support -i eggress-server
cargo tree --manifest-path rust/Cargo.toml -i eggress-server  # expected absent in default production graph
cargo tree --manifest-path rust/Cargo.toml -i eggress-embed   # expected absent
cargo build --manifest-path rust/Cargo.toml --locked --release
```

### Results

- All eight direct crates were downloaded from crates.io at 1.0.12. Cargo
  resolved 16 Eggress packages, all at 1.0.12. The targeted update reported 63
  unchanged dependencies; only the Eggress family and the four lockfile edge
  selections described above changed.
- Default, no-default, and `test-support` workspace checks passed. Formatting,
  default/no-default strict Clippy, and local `cargo deny` passed.
- Provider transport passed 35 default, 41 `test-support`, and 36 no-default
  tests. The provider-profile crate passed 27 tests. Focused C008/C009/C011,
  boundary, finalization, publication, and wire-runtime targets passed 97 tests.
- The final-source default workspace suite passed 930 tests with 1 ignored;
  the no-default suite passed 931 with 1 ignored. Both ran serially across 72
  suites.
- The locked candidate release build passed. Compared with a separate
  baseline worktree at `087366a`, the package count stayed 384 and the
  production non-dev tree stayed 975 lines. The binary grew 520 bytes
  (31,384,976 to 31,385,496 bytes, +0.0017%).
- Default inverse trees confirmed `eggress-server` and `eggress-embed` absent;
  `test-support` includes `eggress-server 1.0.12`. The no-default tree omits
  SSH while retaining non-SSH proxy construction. No QUIC feature is enabled;
  the extended server/UDP shadowsocks edge remains confined to test-support.
- Hosted CI run 37810352765 and Dependency audit run 37810352762 both passed
  at `0562219d`. The dependency audit passed its advisory, license, ban, and
  source checks.

## 5. Invariant review

- One upstream submission, Eggfetch HTTP/1 ownership, and disabled high-level
  retry/redirect/proxy policy are unchanged; coordinator and provider suites
  pass.
- Eggress remains the listener-free route owner; proxy failures remain
  fail-closed and typed; default/no-default SSH behavior is covered in the
  three provider profiles.
- Proxy-hop TLS and destination/origin TLS remain separate; existing route and
  origin trust tests passed.
- Account-scoped pooling, timeouts, cancellation cleanup/recovery, response
  streaming, and redaction remain covered by the passing provider fixtures.
- No configuration, database, protocol, API, process lifecycle, or migration
  changed. Secret-bearing data is absent from the dependency and closure
  evidence.

## 6. Failure and recovery review

This patch does not change runtime failure paths. Provider qualification passed
the existing authentication/refusal, timeout, cancellation/recovery,
fail-closed, and pool isolation cases. No new retry, fallback, buffer, lock,
task, or connection owner was added.

## 7. Migration and compatibility review

No schema, config, API, wire, release identity, or migration change. The exact
pins, optionality, feature selectors, Eggfetch 0.2.2 profile, and EggServe 0.4.0
dependency remain intact. Rust 1.89 MSRV remains declared and each direct
Eggress crate reports Rust 1.89 on crates.io.

## 8. Security review

Local and hosted Cargo policy checks passed advisories, licenses, bans, and
sources. Existing duplicate-version warnings were reviewed; no package
versions outside the Eggress family changed. Route redaction and no-secret
diagnostic tests passed in each provider feature profile. No new permission,
listener, or protocol surface was enabled in production.

## 9. Documentation and operations

Updated current Eggress version references in `AGENTS.md`,
`architecture/deep-dive-providers.md`, `docs/proxy.md`, `rust/README.md`, the
development skill, and the provider transport source comment. The provider
roadmap now reflects 1.0.12 and its closed disposition. Historical M003/M005
plans and closure records remain unchanged.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No unresolved M006 findings | None | None |

## 11. Roadmap disposition

Milestone closed. The Provider Transport roadmap is terminal with M001–M006
closed and no registered successor. The unblock audit searched provider
implementation plans, subsystem dependency declarations, and the registry's
ready/active/blocked tables: no future plan depends on M006, and the blocked
table is empty. Nothing was promoted or newly unblocked. Persistence M011
remains ready on its independent M009/M010 dependencies; all other statuses
remain unchanged.

## 12. Registry updates

- M006 implementation status is `implemented`; closure status is `closed`.
- The provider transport roadmap is `closed`, with M001–M006 closed.
- M006 is removed from the active implementation table and listed in recently
  closed; no provider transport successor is registered.
- The blocked-work table remains empty. Persistence M011 remains independently
  dependency-ready; no plan was promoted by M006.
