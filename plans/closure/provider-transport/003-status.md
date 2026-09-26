# Provider Transport Milestone 003 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/provider-transport/003-eggress-1.0.10-adoption-and-requalification.md`

Source subsystem roadmap:

- `plans/subsystems/provider-transport-roadmap.md#milestone-003--eggress-1010-adoption-and-requalification`

Repository baseline: `b26c5c2e094fe1728159b5546914f69c38cd7afa`.

Implementation commit: `a87790ad8815f3f39b09c39003ff6a73f23904f1`.
Provider M001 closed in `12b846de87564961d97b56e5bdf3f39d91e5307e`.

## 1. Executive finding

Eggress 1.0.10 adoption is locally qualified. Every live Eggress package is
exact-pinned to registry version 1.0.10. Production continues to use the
listener-free `eggress-outbound` adapter, and no source compatibility patch was
needed. Default, `test-support`, and no-default provider transport coverage,
both serial workspace matrices, strict Clippy, dependency policy, release
builds, and tooling pass locally. The hosted dependency audit passed. Final
closure is pending the hosted CI run linked in §4.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Exact-pin the full live Eggress family to 1.0.10 | `rust/Cargo.toml`; all resolved Eggress package entries in `rust/Cargo.lock` are 1.0.10 from crates.io | pass | Includes outbound, compat, protocol, config, routing, SSH/TLS packages |
| Preserve listener-free production boundary | `cargo tree -e no-dev` includes `eggress-outbound`; inverse `-i eggress-embed` and `-i eggress-server` find no production package | pass | No runtime/server facade or listener ownership introduced |
| Preserve SSH/default and no-default contracts | `provider_transport` default/test-support/no-default; no-default full workspace; strict no-default check/Clippy | pass | `test-support` retains the `eggress-server/ssh` alignment needed by the test-only chain executor |
| Preserve proxy/origin TLS separation, fail-closed routing, route errors, cancellation/recovery, account isolation | Provider transport real-socket suite (35 default, 41 test-support, 36 no-default); full coordinator/wire workspace | pass | Existing supported route corpus remains green |
| No accidental UDP/QUIC capability enablement | Eggress outbound feature tree retains only the existing legacy-crypto/pproxy/SSH needs; no `udp`/`quic` outbound feature enabled | pass | The `eggress-udp` crate remains an upstream transitive dependency through `eggress-config`; it is present at 1.0.8 too and is not enabled as a runtime capability here |
| Dependency/security policy | `cargo deny --manifest-path rust/Cargo.toml check`; hosted Dependency audit run `36216728024` | pass | No policy failure |
| Immediate dependency-only footprint comparison | Same-host/toolchain release builds, package counts and non-dev tree line count in §3 | pass | Small 0.17% dependency-only binary increase; explained below |
| Default/no-default lint and test gates | Local results in §4; hosted CI run `36216728012` | pass | All hosted CI steps succeeded |
| Current-authority docs updated; history remains historical | README, Rust README, provider architecture/overview, AGENTS and relevant skills; historical Plan 243 section retained as history | pass | Current live version statements are 1.0.10 or version-neutral |

## 3. Dependency and artifact evidence

Resolved live Eggress crates come from `registry+https://github.com/rust-lang/crates.io-index` and are all `1.0.10`, including:
`eggress-config`, `eggress-core`, `eggress-outbound`,
`eggress-pproxy-compat`, HTTP/Shadowsocks/SOCKS/Trojan/WebSocket protocols,
relay, routing, server (optional test-support), SSH/TLS transports, UDP, and
URI.

| Measurement | 1.0.8 immediate baseline | 1.0.10 dependency-only candidate | Delta |
|---|---:|---:|---:|
| Cargo.lock package entries | 380 | 380 | 0 |
| `cargo tree -e no-dev` lines | 958 | 958 | 0 |
| Release `eggpool` bytes | 27,264,192 | 27,311,376 | +47,184 (+0.17%) |

The dependency-only candidate used the exact upgraded `rust/Cargo.toml` and
`Cargo.lock` copied into a detached worktree at the immediate baseline, with
source otherwise unchanged. The final combined release binary is 27,330,224
bytes; it includes provider diagnostics and request-admission runtime changes,
so it is not used to attribute the Eggress-only delta.

The 1.0.10 graph contains `eggress-udp` transitively through the pre-existing
`eggress-config`/pproxy compatibility path. The same crate existed under 1.0.8;
the feature graph enables no new outbound UDP or QUIC behavior. `eggress-embed`
and `eggress-server` are absent from the normal non-dev runtime graph.

## 4. Verification executed

Toolchain: rustc/cargo 1.89.0; same macOS ARM host and release profile for
immediate baseline and candidate artifacts.

Local commands and outcomes:

- `cargo fmt --manifest-path rust/Cargo.toml --all -- --check` — pass.
- `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings` — pass.
- `cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features` — pass.
- `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings` — pass.
- `cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1` — 35 passed.
- `cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1` — 41 passed.
- `cargo test --manifest-path rust/Cargo.toml --no-default-features --test provider_transport -- --test-threads=1` — 36 passed.
- `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1` — 727 passed across 61 suites.
- `cargo test --manifest-path rust/Cargo.toml --no-default-features -- --test-threads=1` — 614 passed across 57 suites.
- `cargo build --manifest-path rust/Cargo.toml --locked --release` — pass; final combined artifact 27,330,224 bytes.
- `cargo deny --manifest-path rust/Cargo.toml check` — pass.
- `cargo tree --manifest-path rust/Cargo.toml -e no-dev` — pass; 958 lines.
- `cargo tree --manifest-path rust/Cargo.toml -e features` and `cargo tree --manifest-path rust/Cargo.toml --duplicates` — inspected; no unintended Eggress outbound feature and no unexplained new dependency family.
- `uv run python scripts/validate_release_docs.py` — pass.
- `uv run python scripts/validate_runtime_package_boundary.py` — pass.
- Ruff format/check, Pyright and Python tooling pytest — pass; 107 passed, 2 skipped.

Hosted evidence:

- Dependency audit run `36216728024`: pass.
- CI run `36216728012`, head `12b846de87564961d97b56e5bdf3f39d91e5307e`: pass; all hosted steps succeeded, including strict Clippy, no-default check/Clippy, serial workspace tests, Ruff, Pyright and tooling pytest.

## 5. Compatibility, security, and deviations

No Eggress API adaptation was required. The M001 provider adapter changes and
the manifest/lock requalification share implementation commit `a87790a`; the
M003 package-only release build was separately performed in a detached
baseline worktree so its footprint evidence is not confounded by those source
changes. No concurrent implementation or merge conflict occurred.

The `test-support` `eggress-server/ssh` feature alignment remains necessary for
the test-only Eggress chain-executor seam; it does not add Eggress server code
to production. Default root `ssh` continues to enable outbound and compat SSH;
no-default retains direct/non-SSH paths and rejects SSH configuration before
dialing.

Findings: none at medium severity or above. No credential, provider request,
proxy URI, or source error text was added to logs or diagnostics.

## 6. Unblock audit

M001 and M003 are closed. Provider M004's two hard dependencies are now
satisfied; this closure transition promotes M004 to ready for its final
implementation/closure audit. M002 remains blocked on the absent upstream
Eggfetch typed error taxonomy and is not made eligible by this adoption.
