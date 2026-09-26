# Request Admission and Wire Milestone 001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/request-admission-wire/001-inference-body-resource-admission-hardening.md`

Source subsystem roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-001--inference-body-resource-admission-hardening`

Repository baseline: `04f447a4fa459385fddd58ac2cd58f29320725b5`.

Implementation commit: `a87790ad8815f3f39b09c39003ff6a73f23904f1`.

## 1. Executive finding

M001 implements a process-local aggregate bound for retained raw inference
bodies, early rejection of declared oversize requests, live-generation-aware
reservation sizing, and a five-minute downstream body-read deadline. Every
inference endpoint retains the RAII reservation through endpoint execution and
streaming handoff, then releases it as the handler returns. Real-socket tests
cover declared and chunked oversize, aggregate contention/recovery, a body
disconnect, live limit increase/decrease, compact admission, and release at
streaming handoff. No public config, request type, wire behavior, dependency,
or schema change was introduced.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Explicit aggregate raw-body budget | `request/resource_budget.rs`: process-local atomic reservation; effective ceiling `max(64 MiB, leased live limit)`; overflow-safe compare/exchange; RAII drop | pass | Bounds retained ingress raw bytes, not RSS or decoded JSON allocation |
| Known declared length reserves exact bytes and rejects above live limit before collection | `eggpool_live_body_limit_applies_to_content_length_and_chunked_bodies`; `admit_inference_body` preflight | pass | Real-socket over-limit case sends headers without the declared body and receives 413 |
| Unknown-length body reserves full live limit before collection | `aggregate_unknown_body_admission_rejects_without_reading_and_recovers` | pass | Second unknown request receives generic 503 while first holds 64 MiB reservation |
| Aggregate overload has no resource detail and listener recovers | Same test asserts no `67108864` leak, drops stalled first connection, and sends later request | pass | 503 body uses existing generic service-unavailable shape |
| Reservation survives request ownership and releases at stream handoff | `unknown_body_reservation_releases_at_stream_handoff` starts two chunked streaming calls; second succeeds while first downstream stream remains open | pass | Uses two independent fixture accounts and provider streams |
| Cancellation/disconnect release and service recovery | Incomplete Content-Length disconnect in `eggpool_live_body_limit_applies_to_content_length_and_chunked_bodies`; aggregate contention/recovery test | pass | Health/later inference succeeds; fixture has no sleep-based ordering |
| Live generation increase/decrease semantics | `body_admission_uses_each_requests_leased_generation_limit` | pass | 32→64 permits new 40-byte request; stalled request leased at 64 completes after 64→32; new 33-byte request is rejected |
| Compact shares admission path | `compact_route_enforces_live_generation_body_ceiling` | pass | Existing compact protocol and request shape unchanged |
| Five-minute body-read / 24-hour handler timeout | `eggserve_policy_defaults_remain_eggserve_owned` exact assertions | pass | EggServe still owns transport policy; application limit remains generation-owned |
| Public compatibility and no new persistent/config state | `FiniteRequest`, `CompactAdmittedRequest`, wire tests and manifest/schema diff inspection | pass | No public server-budget field, Cargo dependency, or migration |
| Default/no-default local gates | 12 `server_transport` tests pass in each profile; full default 727 and no-default 614 passed before the final added streaming-only regression; that final test also passes in both profiles | pass | Hosted final default workspace run reports 729 passed |
| Tooling/docs and hosted dependency audit | Ruff/Pyright/pytest, docs and package-boundary validators; Dependency audit run `36216728024` | pass | Hosted CI and dependency audit both pass |

## 3. Final budget and ownership

`MIN_RAW_BODY_BUDGET_BYTES = 64 * 1024 * 1024`. For a request using its
already-acquired `GenerationLease`, the process-wide effective ceiling is
`max(64 MiB, generation.server.max_request_body_bytes)`. Valid declared lengths
reserve the exact declared number of bytes; no trustworthy length reserves the
full per-request live limit. A declared length greater than that live limit
returns 413 before polling/collecting the body. An aggregate reservation that
does not fit returns 503 before collection. `Limited` remains the authoritative
observed-byte cap.

The reservation guard is held in middleware/request extensions and explicitly
extracted by all four inference handlers. It spans coordinator work while
ingress `Bytes` remains owned and drops when the handler returns after finite
completion or streaming handoff. RAII covers early errors, cancellation,
disconnect, and unwind. Existing admitted requests retain their generation's
limit; new requests use the newly leased generation. Process-wide in-use bytes
remain counted across a live limit decrease until old requests release.

## 4. Verification executed

- `cargo fmt --manifest-path rust/Cargo.toml --all -- --check` — pass.
- `cargo test --manifest-path rust/Cargo.toml --lib request::resource_budget -- --test-threads=1` — 3 passed.
- `cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1` — 12 passed.
- `cargo test --manifest-path rust/Cargo.toml --no-default-features --test server_transport -- --test-threads=1` — 12 passed.
- `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings` — pass.
- `cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features` — pass.
- `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings` — pass.
- Default full workspace serial suite — 727 passed across 61 suites before the final test-only streaming handoff case; that added case passes in both profiles above.
- No-default full serial suite — 614 passed across 57 suites before the final test-only streaming handoff case; that added case passes in both profiles above.
- `uv sync --frozen`, Ruff format/check, Pyright, tooling pytest — pass; 107 passed, 2 skipped.
- `uv run python scripts/validate_release_docs.py` and `uv run python scripts/validate_runtime_package_boundary.py` — pass.
- Hosted dependency audit run `36216728024` — pass.
- Hosted CI `36216728012`, head `12b846de87564961d97b56e5bdf3f39d91e5307e` — all steps passed; default serial workspace suite reports 729 tests passed.

## 5. Compatibility, privacy, and residual findings

No public request structures, canonical IR, endpoints, persisted values,
configuration keys, dependency graph, or migrations changed. The 1 GiB
EggServe/Tower ceiling and 24-hour handler timeout remain intact. The five-minute
deadline applies only while reading the downstream request body. Rejections
contain no configured byte count, usage counter, prompt, credential, or body
content. Documentation separates transport ceiling, generation-owned request
limit, aggregate raw-body admission, and upload deadline.

Findings: none at medium severity or above. The bound is for aggregate raw
request body bytes; it is not an exact process RSS guarantee.

## 6. Disposition and unblock audit

M001 is closed. The implementation consumes the already-closed
server-transport interface and creates no new downstream transport milestone.
No blocked plan is promoted by this request-admission closure.
