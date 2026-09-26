# Provider Transport Milestone 001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/provider-transport/001-eggfetch-adapter-contract-hardening.md`

Source subsystem roadmap:

- `plans/subsystems/provider-transport-roadmap.md#milestone-001--eggfetch-adapter-contract-hardening`

Repository baseline reviewed: `b1896813caa1ebb5382939d9a29788d987c01843`

Implementation commit: `a87790ad8815f3f39b09c39003ff6a73f23904f1` — provider
adapter hardening and request admission landed together; M003 was independently
compiled against the exact 1.0.10 manifest/lock in an isolated worktree.

## 1. Executive finding

M001 is complete. Provider response DATA remains incremental and DATA-only;
consumed trailer frames are retained separately, preserve duplicate values, and
are available through a non-draining accessor. Redacted `Debug` output exposes
only whether trailers exist. The residual Eggfetch pool error no longer parses
diagnostic strings: the typed physical-admission predicate is the only runtime
pool-timeout signal, and residual pool errors fail closed as configuration
faults. The exact 0.2.0 source audit confirmed physical admission timeout
construction is distinguishable through the public predicate and found no
other production pool error that should be mapped as request contention.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| DATA-only compatibility and incremental response consumption | `ProviderBody::next` continues returning only DATA `Bytes`; `provider_transport` passes in default, test-support, and no-default profiles | pass | No body buffering was added |
| Preserve consumed HTTP trailers, including duplicate values | `response_trailers_are_retained_without_becoming_data_or_debug_values` real-socket chunked fixture | pass | Trailer metadata remains private to provider transport |
| Accessor does not read/drain body | `take_trailers` only takes the stored `Option<HeaderMap>` | pass | No I/O or body lease behavior change |
| Redact trailer values in Debug | Manual `ProviderBody` Debug prints `proxy_transport` and `has_trailers` only | pass | No credential/body/trailer value output |
| Remove message-text pool classification | `residual_eggfetch_pool_errors_are_configuration_not_text_classified`; source audit of exact `eggfetch-core 0.2.0` pool constructors | pass | Physical admission predicate maps to `PoolTimeout`; residual pool faults map to `Configuration` |
| Preserve coordinator and public wire semantics | Full default/no-default serial suites and focused provider/coordinator/wire tests | pass | No downstream trailer forwarding added |
| Current docs describe contract | Provider deep dive, overview, development/documentation skills, README and AGENTS updated | pass | Historical plans unchanged |
| Preserve M002 boundary | Roadmap/registry retain M002 as upstream-blocked | pass | Hyper/Rustls source-chain taxonomy work remains out of scope |

## 3. Implementation evidence

Changed production files: `rust/src/providers/transport.rs`. The adapter owns
`Option<HeaderMap>` per body; each consumed trailer frame appends all named
values to that map. Unknown/non-DATA/non-trailer frames remain ignored. Body
drop still owns Eggfetch body/pool release. No coordinator, routing, retry,
wire, schema, dependency, or public config behavior was added by M001.

Exact Eggfetch source audit found these pool origins: invalid policy,
physical-admission timeout, and closed lifecycle gate in `transport/lifecycle.rs`;
closed or poisoned semaphore paths in `pool.rs`. EggPool validates its physical
connection limit before client construction. Only the explicit
`is_physical_connection_admission_timeout()` helper is classified as pool
contention; untyped residual pool faults conservatively map to configuration.

## 4. Verification executed

Commands and results:

- `cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1` — 35 passed.
- `cargo test --manifest-path rust/Cargo.toml --features test-support --test provider_transport -- --test-threads=1` — 41 passed.
- `cargo test --manifest-path rust/Cargo.toml --no-default-features --test provider_transport -- --test-threads=1` — 36 passed.
- `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1` — 727 passed across 61 suites.
- `cargo test --manifest-path rust/Cargo.toml --no-default-features -- --test-threads=1` — 614 passed across 57 suites.
- `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings` — pass.
- `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings` — pass.
- `cargo fmt --manifest-path rust/Cargo.toml --all -- --check` — pass.
- `cargo deny --manifest-path rust/Cargo.toml check` — pass.
- Python tooling: Ruff format/check and Pyright pass; pytest reports 107 passed, 2 skipped.

The 12-test `server_transport` suite passes in both default and no-default
profiles after its final streaming-reservation regression was added. The full
workspace runs above preceded that test-only addition; no production code
changed afterward.

## 5. Compatibility, security, and residual findings

No public wire/header behavior, transport error string, persisted schema, or
configuration contract changed. Trailer values and provider error source text
are not emitted in diagnostics. Retry/failover/health behavior remains driven by
existing typed source/category rules.

Findings: none at medium severity or above. M002 remains blocked on an upstream
Eggfetch general-purpose typed error-classification API; this is a named
deferred dependency and not an M001 defect.

## 6. Disposition and unblock audit

M001 is closed. M003 was eligible independently and is the next provider plan.
M004 remains blocked until M001 and M003 both close. M002 remains blocked on
the upstream Eggfetch classification surface. No other provider plan is
promoted by this closure.
