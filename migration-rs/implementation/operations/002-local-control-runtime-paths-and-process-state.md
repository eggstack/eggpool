# O002 — Local Control, Runtime Paths, and Process-State Boundary

Status: queued behind O001

Source roadmap: `migration-rs/subsystems/operational-cli-lifecycle-roadmap.md`

Primary class: infrastructure/invariant

Hard dependency: accepted O001.

## Objective

Implement the small process-local substrate needed by M9 commands: canonical runtime paths, PID/process-state helpers, and the bounded Unix-domain control protocol/client/server that adapts operator requests to the closed M8 reload/runtime lifecycle.

O002 does not yet implement the user-facing lifecycle commands. O003 consumes these APIs.

## Design constraints

- exactly one process-owned control listener;
- Unix-domain filesystem socket only for the current supported local control contract;
- no localhost TCP management port;
- no tonic/gRPC/JSON-RPC framework;
- no Python subprocess fallback;
- no second reload state machine;
- no generic daemon supervisor framework.

Use Tokio Unix/process/fs primitives and Serde JSON already present. Platform-gate Unix-only code with a typed unsupported-platform result rather than dragging in a portability crate during M9.

## Part A — runtime paths

Create a narrow Rust runtime-path module that matches O001 observations for:

- PID file;
- control socket;
- daemon log file;
- XDG config/data/state directories;
- personal vs production/deploy-user paths where command behavior depends on them.

Path resolution must:

- honor reviewed environment precedence;
- avoid creating directories during read-only resolution;
- create parent directories only in explicit mutating helpers;
- reject non-directory collisions and unsafe ownership/mode states where the Python contract does;
- never interpolate secrets into paths or diagnostics.

## Part B — PID/process state

Implement typed helpers for:

- read PID;
- atomic/best-effort-safe PID write;
- clear stale PID;
- process existence probe;
- signal TERM where supported;
- bounded wait-for-exit;
- health probe as a separate observation;
- combined `Running`, `Stopped`, `StalePid`, `PortOccupied/HealthyUnknownOwner`, and error classifications needed by O003.

Do not treat `kill(pid, 0)` or a PID file alone as proof that the target is the current EggPool process. Preserve the Python duplicate-instance policy using PID plus health/socket evidence. Reused PID risk must fail conservatively rather than signaling an unrelated process.

Where process identity cannot be proven safely from the current contract, prefer health/control evidence and explicit refusal over broad `/proc` parsing or platform-specific process libraries.

## Part C — control protocol

Port the O001 frozen local-control wire contract into small typed structures. At minimum preserve:

- protocol version;
- request id;
- command name;
- validated config digest for `reload_config`;
- structured response fields used by `rehash`;
- bounded line size;
- one request / one response / close lifecycle;
- request-id echo;
- timeout and malformed/protocol mismatch categories.

The server dispatch for `reload_config` must call the existing `ReloadService` and project its result. It must not reproduce config diff, generation publication, wire-policy staging, or diagnostic bookkeeping.

Only add other control commands if O001 proves current Python requires them. `runtime-status` currently uses authenticated local HTTP and stop/restart use process lifecycle helpers, so do not broaden the socket merely for architectural neatness.

## Part D — socket lifecycle/security

The process-owned listener must:

- create its parent state directory with reviewed mode;
- bind to the canonical local path;
- set restrictive socket permissions;
- refuse to overwrite a socket owned by a live EggPool instance;
- remove a stale socket only after a bounded ownership/connect probe;
- close/unlink on graceful shutdown;
- tolerate stale-file cleanup on next startup after crash;
- enforce connection/request bounds so a local malformed client cannot exhaust the proxy;
- isolate per-connection parse/write errors from the accept loop;
- avoid logging raw request frames.

A client disconnect while reload is retained must not cancel/strand the M8 reload worker or diagnostics; R012/R013 semantics remain authoritative.

## Part E — integration with M8 startup/shutdown

Wire listener construction into process startup after configuration/runtime readiness is sufficient to serve the command safely, and ensure shutdown owns listener close/unlink.

If control listener startup fails because of permissions/path collision, follow the O001/Python contract: either startup-fatal or explicitly degraded. Do not silently report control available when it is not.

No request-admission or provider work occurs inside control framing code.

## Tests

Use temp XDG roots and actual local Unix sockets. Cover:

- path precedence and no-create reads;
- PID create/read/clear/stale/reused-PID safety;
- healthy server without matching PID;
- stale PID plus dead/no socket;
- socket mode and parent mode;
- stale socket recovery;
- live socket collision refusal;
- valid reload request/response parity;
- malformed JSON/object/type/version/request id;
- oversized frame/no newline/multiple commands;
- client connect/read/write timeout;
- client disconnect before retained reload finishes;
- concurrent clients with one M8 reload returning Busy appropriately;
- accept loop survives bad clients;
- shutdown closes/unlinks;
- no secret values in traces/errors.

Add a focused `rust/tests/operations_o002.rs` or equivalently scoped tests.

## Failure isolation

A malformed/local client must never crash the server, close provider pools, poison the runtime manager, leave admission gated, or require DB refresh/restart to recover. A control response serialization/write failure affects only that connection.

## Dependency posture

No new dependency is expected. If safe process signaling needs a tiny Unix crate because the standard library does not expose the required call without unsafe code (the crate has `#![forbid(unsafe_code)]`), evaluate an already-transitive safe wrapper first and justify any direct addition narrowly. Do not add a general process-management package.

## Non-goals

- daemon detach;
- CLI rendering;
- systemd/cron;
- backup/update;
- HTTP control API;
- Windows control transport;
- M10 platform characterization.

## Verification

Run fmt, Clippy `-D warnings`, focused O002 tests, R007/R009/R010/R012/R013 regressions, aggregate Rust tests, targeted Python control/runtime tests, migration oracle, Ruff/Pyright if Python fixtures changed, and `git diff --check`.

## Closure evidence

Write `migration-rs/closure/operations/002-status.md` with protocol/path parity matrix, socket permission/framing results, stale PID/socket cases, cancellation/Busy evidence, dependency review, and unresolved findings.

## Acceptance criteria

O002 closes only when Rust has a bounded secret-free process-local control path and reusable process/path primitives that preserve M8 authority and recover safely from stale/malformed local state without introducing a second runtime/control architecture.

Accepted O002 promotes only O003.