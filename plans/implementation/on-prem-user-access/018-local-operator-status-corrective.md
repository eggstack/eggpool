# On-Prem User Access M018 — Local operator status after identity cutover

Status: active

Repository baseline: `88f58bbf2e9095cf46437945edf83e580270bdb6` (`main`, post-PR #7).

Source roadmap: `plans/subsystems/on-prem-user-access-roadmap.md#m018--local-operator-status-corrective`

Long-term requirements: `plans/000-long-term-specification.md#2-end-state-invariants-normative`, `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`.

Applicable ADR: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted; new authorization boundary requires ADR evaluation).

Primary class: capability

## 1. Objective

Restore `eggpool status` and `eggpool runtime-status` for the local operator after administrator bootstrap without relaxing browser-only privileged HTTP authorization. Preserve solo/headless usability and both text/JSON contracts.

## 2. Readiness and dependencies

Hard: none. Interface: existing owner-protected local control socket, process lifecycle and status aggregation. Inspect actual peer/permission authority before extending the socket; if the proposed privilege boundary differs from ADR-0002, obtain an ADR before implementing.

## 3. Current evidence

`rust/src/runtime.rs::{fetch_local_json,status,runtime_status}` authenticates HTTP `/api/status` and `/api/stats/runtime` using `Config::resolved_server_api_key()` or no credential. `rust/src/server/middleware.rs` demands a browser admin session after bootstrap, so a healthy managed daemon can return 401. `rust/src/operations/control.rs` already owns local privileged rehash; `rust/src/operations/status.rs` owns the status contract. M006/M016 did not test this exact mode transition.

## 4. Invariants

A personal agent token cannot access privileged HTTP status. No unauthenticated loopback managed-mode exemption, no TCP admin backdoor, new daemon, second DB writer, or shared key revival. Preserve JSON schema, readiness semantics, status classifications, redaction and single-gate SQLite.

## 5. Scope

In: a bounded authorized local operator status/readback path and CLI adapter. Out: web/admin role changes, unrestricted local HTTP privilege, remote operator shell, provider health redesign or any automatic mutation when reading status.

## 6. Production changes

Prefer a narrow read-only request on the existing OS-owned control channel, with explicit socket ownership/peer validation, protocol version and bounded payload, reusing current status/runtimestat projection. Keep HTTP endpoints unchanged. Local CLI should accurately distinguish unauthorized, absent socket, stale PID and not-ready daemon; static offline snapshots must be labeled offline.

## 7. Ordered work packages

### A — Inventory OS authority and current status owner

Inspect control socket permissions, peer credentials, lifecycle/service users and transport reliability on Linux/macOS. Decide whether current OS-authorized local commands can obtain the status projection without new administrative reach. If new auth semantics require an ADR, block before code.

Acceptance: documented socket authorization and threat cases.

### B — Implement bounded local readback

Extend control status interface and `runtime.rs` CLI as appropriate; cap response bytes and timeouts; preserve `--json` schema and nonzero error exits. Keep control request handlers thin and do not tie the control loop to provider I/O.

Acceptance: local operator can query running admin_initialized/managed and legacy instances.

### C — Integrate failure and restart matrix

Exercise wrong-owner socket, missing/stale socket, malformed/truncated/oversized response, daemon down, restart, rehash, role isolation and readiness degradation. Include a systemd-like filesystem privilege fixture without touching a live deployment.

Acceptance: no error is silently treated as readiness, no unmanaged auth fallback.

## 8. Failure, cancellation, restart and contention

Bound requests; avoid holding the SQLite writer gate while waiting for network/provider activity. Stale runtime identity and process restart must not make a prior socket appear authoritative. Fail closed on unexpected peer/protocol, not by sending a shared key over loopback.

## 9. Compatibility and migration

No schema change anticipated. Preserve existing HTTP `/api/status` role checks and prior legacy flow. A read-only local status endpoint is not a user-facing inference credential or authorization scope. Escalate to an ADR if current socket file permissions cannot safely serve as operator authority.

## 10. Tests

Extend `rust/tests/cli_contract.rs`, `rust/tests/on_prem_sessions.rs`, `rust/tests/server_transport.rs` and existing control/lifecycle tests; new end-to-end status CLI fixture covers pre/post-bootstrap, no configured server key, malformed socket and restart. Assert unauthorized user tokens cannot read privileged HTTP admin stats.

## 11. Verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

## 12. Documentation

Update `docs/cli-reference.md`, `docs/deployment.md`, `architecture/deep-dive-control.md` and `architecture/deep-dive-security.md`; explain local operator permissions and failure modes.

## 13. Acceptance criteria

A local authorized operator sees live status/runtime diagnostics after fresh bootstrap and managed transition. JSON schema and exit behavior remain compatible; HTTP member/API-key access to privileged status remains forbidden.

## 14. Stop conditions

Stop if local socket peer identity/permissions cannot be verified, if operation would grant network privilege, or if correctness requires redefining the accepted auth ADR without review.

## 15. Closure evidence

Record exact SHA, permission/threat review, serialized Rust/no-default CI, process restart/negative-path evidence, stability of CLI JSON and unresolved findings in `plans/closure/on-prem-user-access/018-status.md`; audit blocked-work promotions.

## 16. Handoff

Independent P1 corrective; may proceed parallel with M017. Do not solve this by granting personal inference tokens the admin dashboard/stats scope.
