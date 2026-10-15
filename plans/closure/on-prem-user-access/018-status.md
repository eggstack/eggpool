# On-Prem User Access M018 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/018-local-operator-status-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m018--local-operator-status-corrective`

Repository baseline reviewed: post-merge `88f58bbf`; implementation candidate
`8e2b66a` with CLI regression fixture `3be5d83`.

## 1. Executive finding

The local operator can read status and runtime status through the existing
owner-protected Unix control socket after identity cutover. The CLI no longer
depends on the retired shared API key for these read-only projections. The
HTTP status routes retain their managed-mode authorization; personal inference
credentials do not gain browser or operator status rights. The control channel
uses bounded, versioned requests and bounded JSON payloads, and the local
listener remains restricted to a private runtime directory.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Existing OS-local authority is used without network privilege | `rust/src/operations/control.rs`; private runtime directory checks; `deep-dive-control.md` | pass | No TCP listener, new process, or HTTP auth exception. |
| Local status and runtime status work without a configured server key | `rust/src/runtime.rs`; `rust/tests/status_command.rs::status_cli_uses_private_control_readback_without_a_server_key` | pass | Invokes both actual CLI commands against a private local control listener. |
| Response is bounded and protocol failures remain errors | `control.rs` readback request/response cap and timeout; `operations_o002` | pass | Existing control protocol limits are retained for these operations. |
| Managed HTTP privilege remains isolated | `rust/src/server/middleware.rs`; `on_prem_sessions` and server tests | pass | No personal-token scope or managed loopback bypass was added. |
| Operator documentation explains authority and failure modes | `docs/cli-reference.md`, `docs/deployment.md`, `architecture/deep-dive-control.md` | pass | No provider probe is performed by status readback. |

## 3. Implementation evidence

- `rust/src/operations/control.rs` adds read-only status and runtime-status
  commands on the private control protocol and a typed client readback method.
- `rust/src/server/mod.rs` serves snapshots from the local control handler;
  status aggregation remains owned by the status/health projection.
- `rust/src/runtime.rs` routes both CLI commands through local control and
  reports absent, stale, malformed, timeout, and permission errors without an
  HTTP/API-key fallback.
- `rust/tests/operations_o002.rs` covers the bounded control readback contract;
  `rust/tests/status_command.rs` runs both real CLI commands with a keyless
  config and private socket.
- No migration, new database writer, provider call, or public HTTP route was
  added.

## 4. Verification executed

- `cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1` — 9 passed.
- `cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1` — 14 passed, including the keyless CLI readback fixture.
- `cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1` — 10 passed.
- Default workspace serial suite — 1,028 passed, 1 ignored across 79 suites (before the additive CLI fixture).
- No-default workspace serial suite — 1,029 passed, 1 ignored across 79 suites (before the additive CLI fixture).
- Strict default and no-default Clippy, formatting, release-document validation, and `git diff --check` passed with the implementation present. The additive fixture passed its focused target.
- These are local results; hosted CI and deployed systemd integration were not run in this closure pass.

## 5. Invariant and recovery review

The privileged surface remains a local filesystem socket in an OS-private
runtime directory. No personal token is accepted by the status command, and
managed HTTP authorization remains unchanged. The CLI fails closed when the
socket is unavailable or its response is invalid; it does not infer readiness
from process existence or an offline snapshot. Readback is bounded and does
not hold the SQLite writer gate while waiting on provider or network work.

## 6. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No unresolved M018 implementation finding | — | — |

## 7. Roadmap and unblock disposition

M018 is closed. The dependency audit searched the on-prem implementation
plans, roadmap graph, and ready/active/blocked registry tables. No registered
plan has a hard dependency on M018, so no blocked work is promoted. M019 and
M022 remain ready; M020 remains blocked on M019 (M017 is already closed),
M021 remains blocked on M020, and M023 remains blocked on M022 plus ordinary-
server operational evidence. No 49-user, concurrent-capacity, or generated
output-cap claim is introduced.

The registry and roadmap record M018 closed. Historical M001–M017 closure
records were not modified.
