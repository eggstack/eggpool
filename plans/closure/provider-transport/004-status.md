# Provider Transport Milestone 004 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/provider-transport/004-typed-transport-diagnostic-evidence.md`

Source subsystem roadmap:

- `plans/subsystems/provider-transport-roadmap.md#milestone-004--typed-transport-diagnostic-evidence`

Prerequisite closures:

- Provider transport M001: `plans/closure/provider-transport/001-status.md`
- Provider transport M003: `plans/closure/provider-transport/003-status.md`

Refreshed execution baseline: `a87790ad8815f3f39b09c39003ff6a73f23904f1`;
M001 and M003 closure transition: `a0f0b90`.

## 1. Executive finding

M004 is complete. `TransportError::diagnostic_class()` is the sole exhaustive
authority for bounded, static, secret-free transport labels. Finite submit and
provider-body-read errors, streaming pre-handoff submission errors, and
post-handoff streaming body errors preserve that label on the transient
`FailureObservation`. Existing phase fields, source/category, retry and
health-related effects, persistence values, and terminal behavior remain
unchanged. M004 does not depend on blocked M002.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| One exhaustive mapping for every `TransportError` | `transport_diagnostic_labels_are_exhaustive_static_and_policy_neutral` covers all 19 enum variants | pass | Static lowercase/underscore labels, each at most 32 bytes |
| Finite transport submission and response-body-read propagation | `coordinator/finite.rs` labels actual `AttemptError::Transport` and `read_to_bytes` errors | pass | Local/wire errors do not receive a transport class |
| Streaming pre-handoff and post-handoff propagation | `streaming/coordinator.rs` labels transport submission errors; `execution.rs` passes the static class through `PullBody::Transport`; `terminal.rs` attaches it to terminal observation | pass | Post-handoff failures remain terminal/non-replayable |
| Same authority across finite and streaming paths | All producer sites call `TransportError::diagnostic_class()` | pass | No duplicated mapping or Eggress/Eggfetch internal match |
| Policy invariance | Unit test compares complete `FailureEffects` for generic transport evidence and each of the 19 specific labels | pass | Classifier output is identical for every variant |
| Preserve `transport_phase`/`dispatch_phase` semantics | Diff and producer inventory show only `error_class` changed at transport observations | pass | Existing phase values remain unchanged |
| Preserve special body-too-large and cancellation behavior | Existing exceptional source/category branches and terminal/cancellation handling retained; full default/no-default suites pass | pass | New labels do not drive retry/replay decisions |
| Complete consumer/persistence compatibility audit | Search of all `FailureObservation`, `error_class`, and `transport_phase` consumers; see §3 | pass | Observation label is transient; durable fields continue using `FailureEffects.evidence_class` |
| Secret-free labels | Static enum mapping contains no route/provider/source data; no formatting of raw errors | pass | Existing redaction behavior remains |
| Documentation and verification | Provider deep dive updated; hosted CI/dependency audit and local full matrices pass | pass | No manifest, lockfile, DB schema or API projection change from M004 |

## 3. Consumer and persistence inventory

| Surface | Use | Compatibility classification |
|---|---|---|
| `FailureObservation.error_class` | Set by finite coordinator, streaming coordinator, and streaming terminal observation helpers; passed to in-process `classify()` / `FailureDecisionEngine` | Ephemeral internal evidence; not serialized, persisted, or externally projected |
| `FailureObservation.transport_phase` | Existing generic phase labels set by those same observation helpers | Ephemeral internal evidence; unchanged |
| `FailureEffects.evidence_class` | Result of established policy classification; used in finalization data | Durable policy evidence; unchanged by M004 |
| `request_attempts.error_class` and request `error_class` columns | Written by finalization from `FailureEffects.evidence_class` or existing explicit lifecycle error values | Persisted contract remains unchanged; new transport labels do not reach these columns |
| Health/backoff/circuit and retry consumers | Driven by classified effects/source/category; `classify()` does not read observation `error_class` | Policy input remains unchanged |
| API projections, metrics, event sinks, schema/migrations | No consumer of `FailureObservation.error_class` or `transport_phase` found | No external field, migration, or metric cardinality change |

## 4. Final diagnostic map

| `TransportError` | Label |
|---|---|
| `Configuration` | `configuration` |
| `ProxyConfiguration` | `proxy_configuration` |
| `InvalidTarget` | `invalid_target` |
| `RequestBodyTooLarge` | `request_body_too_large` |
| `PoolTimeout` | `pool_timeout` |
| `ConnectTimeout` | `connect_timeout` |
| `Connect` | `connect` |
| `ProxyConnectTimeout` | `proxy_connect_timeout` |
| `ProxyConnect` | `proxy_connect` |
| `ProxyAuthentication` | `proxy_authentication` |
| `ProxyTargetConnect` | `proxy_target_connect` |
| `Tls` | `tls` |
| `WriteTimeout` | `write_timeout` |
| `Write` | `write` |
| `ReadTimeout` | `read_timeout` |
| `Read` | `read` |
| `ResponseBodyTooLarge` | `response_body_too_large` |
| `Protocol` | `protocol` |
| `Cancelled` | `cancelled` |

## 5. Verification executed

- `cargo test --manifest-path rust/Cargo.toml --lib transport_diagnostic_labels_are_exhaustive_static_and_policy_neutral -- --test-threads=1` — pass.
- `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1` — 727 passed in local serial run; hosted CI for the shared candidate passed all steps.
- `cargo test --manifest-path rust/Cargo.toml --no-default-features -- --test-threads=1` — 614 passed in local serial run.
- Provider transport integration passed with default (35), `test-support` (41), and no-default features (36).
- Both strict default/no-default Clippy, no-default check, and `cargo fmt --check` — pass.
- `cargo deny check`, release build, and dependency graph checks — pass; M004 adds no dependency.
- `uv run python scripts/validate_release_docs.py` and `uv run python scripts/validate_runtime_package_boundary.py` — pass.
- Hosted CI run `36216728012` on `12b846de87564961d97b56e5bdf3f39d91e5307e` — pass.
- Hosted Dependency audit run `36216728024` on the same head — pass.

## 6. Compatibility, security, and residual findings

No retry/failover/health decision branches on `error_class`. The mapper accepts
only an enum and returns static labels; it cannot include proxy URIs, hosts,
provider/account identity, credentials, request content, or upstream error
text. Existing finalization and durable `error_class` behavior remains intact.
No public API, DB schema, or migration changed.

Findings: none at medium severity or above. Blocked provider M002 remains a
separate upstream Eggfetch API dependency and is unaffected by M004.

## 7. Disposition and unblock audit

M004 is closed. Provider M002 remains blocked on publication of a stable,
general-purpose Eggfetch typed error taxonomy; no other provider milestone is
made eligible. Request-admission-wire M001 is independently ready and remains
the next requested plan.
