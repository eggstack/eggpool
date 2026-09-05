# Phase 10 — Control-Plane and XDG Runtime Hardening

Date: 2026-07-19
Status: complete (2026-09-05)
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`
Prerequisites: Phase 1; coordinate with Phases 2, 6, and 11.
Implementation commit: `c5ab0100001beb7cb16bc138b7e4ddda725cf78e`

## Objective

Harden the Unix-domain control plane used by rehash and related commands. The server must fail closed when owner-only permissions cannot be established, strictly validate protocol messages before dispatch, handle stale sockets safely, and honor standard XDG runtime/state locations so isolated instances do not collide.

## Problems addressed

The current control server can continue running after a socket `chmod` failure, which is a fail-open security posture. Protocol parsing assumes decoded JSON is an object, allowing valid non-object JSON to reach internal-error handling. Unknown command strings can reach the reload handler. Runtime path handling also ignores `XDG_STATE_HOME` for some state, and existing isolation coverage is skipped because separate environments can share the same control socket.

## Non-goals

- Do not expose the control socket over TCP.
- Do not add a broad remote administration protocol.
- Do not implement multi-user authorization beyond same-user local control unless explicitly required by future work.
- Do not make permission failures warnings.
- Do not weaken stale-socket safeguards to make tests pass.

## Workstream A — Secure socket directory and file lifecycle

### Directory selection

Use the following precedence:

1. `XDG_RUNTIME_DIR` when set, owned by the current UID, and suitable for Unix sockets.
2. A private owner-only fallback under the user’s state/cache hierarchy where platform constraints require it.

Persistent operational state should use `XDG_STATE_HOME`, falling back to `~/.local/state` only when unset.

Separate APIs should represent:

- runtime directory and socket path;
- persistent state directory;
- PID/lock path;
- logs or cache paths if present.

Do not treat all state as one hardcoded directory.

### Directory permissions

Before binding:

- create the EggPool runtime directory with owner-only mode, normally `0700`;
- verify it is a directory, not a symlink or unexpected special file;
- verify ownership matches the effective UID where supported;
- reject group/world-writable locations unless platform policy explicitly permits a sticky parent and private child;
- avoid following attacker-controlled symlinks.

### Socket permissions

After bind and before accepting commands:

- apply mode `0600`;
- verify final mode and ownership;
- if chmod/stat verification fails, close the server and remove only the socket created by this process;
- surface startup failure rather than logging a warning and serving anyway.

Where the server API begins accepting immediately after bind, ensure command handling remains gated until permission verification completes.

### Safe stale-socket cleanup

Retain recent symlink-safety fixes and add explicit checks:

- use `lstat` where needed;
- never follow symlinks during cleanup;
- probe a real socket before removal;
- remove only stale sockets owned by the expected UID and located in the private runtime directory;
- avoid deleting a live server’s socket during startup races;
- tolerate already-removed paths;
- handle `EADDRINUSE` with bounded diagnostics.

Consider a PID/instance token or inode verification if needed to disambiguate cleanup ownership.

## Workstream B — Strict protocol envelope

Define a versioned request object. Suggested minimum fields:

- `version`;
- `request_id`;
- `command`;
- command-specific `params` object.

For compatibility, version may initially default to the current protocol only when the request shape is otherwise valid.

Reject before dispatch:

- non-object JSON values;
- missing or malformed request IDs;
- unknown command names;
- non-object params;
- unsupported versions;
- invalid digest format/length;
- unexpected fields if strict mode is chosen;
- oversized lines;
- invalid UTF-8;
- multiple requests in one frame if unsupported;
- trailing bytes after the allowed message.

Validate command names against a closed enum such as `status`, `rehash`, or currently supported commands. Unknown commands must never reach the reload handler.

## Bounded input handling

Set a documented maximum request size and enforce it while reading, not only after buffering an unlimited line. On overflow:

- return a bounded protocol error when safe;
- close the connection;
- do not log the full payload.

Set bounded read and write timeouts so a local client cannot hold a connection indefinitely with a partial message.

## Error response contract

Return stable classes:

- parse error;
- invalid request;
- unsupported version;
- unknown command;
- busy;
- validation failure;
- internal error.

Responses should include the request ID only when it was parsed safely. Internal responses must not include raw exception traces, secrets, or complete configuration content.

## Optional Linux peer credentials

On Linux, investigate `SO_PEERCRED`/equivalent support. If practical and portable within the current async server abstraction:

- require peer UID to match server UID;
- record mismatch as denied;
- avoid making unsupported platforms fail.

Socket file permissions remain mandatory even if peer credentials are added.

## Multiple-instance isolation

Define intended behavior:

- one default EggPool instance per user; or
- multiple named/profile instances.

If multiple profiles are supported, derive the socket filename from a stable, non-secret profile/config identity and keep it within a private directory. Avoid raw config paths that exceed Unix socket path limits.

At minimum, two processes with distinct `XDG_RUNTIME_DIR` values must never collide.

## Client alignment

Update control clients and CLI commands to use the same runtime-path resolver and protocol envelope. Avoid separate path logic in server and client.

Client behavior should distinguish:

- socket absent;
- permission denied;
- protocol mismatch;
- server busy;
- stale socket/connect refused;
- timeout.

## Tests

### Permission failure

Inject chmod/stat failure. Assert server closes, removes its socket safely, and startup fails. No command is accepted.

### Path safety

Cover:

- private directory creation;
- wrong owner/mode;
- symlink directory/file;
- stale socket;
- active socket;
- concurrent startup;
- path already removed;
- socket path length handling.

### Protocol validation

Table-test null, list, number, string, malformed JSON, invalid UTF-8, missing fields, unknown command, bad params, unsupported version, oversized input, and partial-read timeout.

Every case must produce a bounded result without internal traceback or server hang.

### XDG isolation

Start two servers with distinct temporary `XDG_RUNTIME_DIR` and `XDG_STATE_HOME` values. Assert distinct paths and successful independent client commands. Convert the existing skipped isolation test into a strict passing test.

### Fuzz/property coverage

Use lightweight generated JSON shapes and bounded byte inputs. Assert no crash, unbounded allocation, or task leak.

### Reload integration

Ensure valid rehash requests retain current semantics and Phase 2 busy behavior through the hardened protocol.

## Implementation sequence

1. Centralize runtime/state path resolution.
2. Add XDG tests and remove hardcoded path assumptions.
3. Harden directory creation and verification.
4. Gate server availability on verified socket permissions.
5. Define typed protocol request/response models.
6. Add bounded read/write and size limits.
7. Reject unknown commands before handlers.
8. Align client path and protocol handling.
9. Add peer credentials if practical.
10. Run integration, concurrency, and fuzz tests.

## Acceptance criteria

- Socket startup fails closed when `0700` directory or `0600` socket guarantees cannot be established.
- Stale cleanup never follows symlinks or removes an active foreign socket.
- Non-object JSON and unknown commands return protocol errors, not internal errors.
- Input size and read duration are bounded.
- Server/client share one XDG-aware path resolver.
- Distinct XDG environments do not collide.
- The existing XDG skip is removed and the test passes strictly.
- Valid rehash/status commands remain compatible.
- Protocol fuzz tests produce no crash, hang, unbounded task, or secret-bearing error.

## Handoff evidence

Provide path-resolution examples, permission-failure test output, protocol validation matrix, XDG dual-instance results, any peer-credential decision, and confirmation that no broad permission warning remains in server startup.

## Closure record

### Implementation completed

- `runtime_paths.runtime_dir()` now prefers an explicit runtime override,
  then a suitable private `XDG_RUNTIME_DIR`, then a private state/runtime
  fallback, with a UID-scoped `/tmp` path only as a last resort. The control
  client and server continue to use the same `control_socket_path()` resolver.
- Control startup creates the runtime directory with `0700` intent and fails
  closed unless the final path is an owner-only, owner-owned directory and
  not a symlink or special file. Socket setup verifies owner, socket type, and
  `0600` mode before the server is considered available.
- Stale cleanup uses `lstat`, refuses symlinks, regular files, active sockets,
  and foreign-owned sockets, and checks the observed device/inode before
  unlinking. Startup-failure and shutdown cleanup are scoped to the socket
  identity created by that server instance.
- The v1 envelope rejects non-object JSON, non-object `params`, malformed
  request IDs, unknown commands, unsupported versions, invalid digests,
  invalid UTF-8, oversized input, and multiple frames before dispatch. Reads
  and writes use bounded timeouts; protocol errors have a non-internal
  `parse_error` category. The client applies the same bounded response limit
  and validates response object shape.
- Linux `SO_PEERCRED` enforcement remains enabled where available and fails
  closed on missing, malformed, or unreadable peer credentials. Unsupported
  platforms retain the mandatory socket-permission gate.

### Verification evidence

The exact implementation was verified with:

```text
rtk uv run pytest tests/unit/test_control_server.py tests/unit/test_runtime_paths.py tests/unit/test_reload_security.py -q --tb=short --maxfail=1
121 passed

rtk uv run pytest tests/integration/test_rehash_cli_edge_cases.py tests/integration/test_rehash_acceptance.py -q --tb=short --maxfail=1
13 passed, 1 skipped

rtk uv run ruff format --check src/ tests/ scripts/
728 files already formatted
rtk uv run ruff check src/ tests/ scripts/
All checks passed!
rtk uv run pyright src/ scripts/
0 errors, 0 warnings, 0 informations
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1
14 passed
rtk git diff --check
passed
```

The focused unit coverage includes the permission-failure cleanup path,
private-directory and symlink rejection, owner-scoped stale cleanup, bounded
protocol-shape matrix, multiple-frame rejection, and two simultaneously
available servers using distinct `XDG_RUNTIME_DIR` and `XDG_STATE_HOME`
values. The one integration skip is the pre-existing subprocess retirement
timeout scenario; it is not XDG or control-protocol coverage, and the strict
XDG isolation test passes.

### Peer-credential decision

No new cross-platform dependency was introduced. On Linux, the existing
`SO_PEERCRED` check is mandatory when the platform exposes it; missing or
malformed kernel data rejects the connection. On platforms without that
socket option, owner-only directory and socket permissions remain mandatory.

### Dependency review

Phase 11 (`plans/012-phase-11-reload-diagnostics.md`) was already in
`implementation handoff` and is now unblocked with respect to its Phase 10
coordination point; no status transition was needed. Phase 12
(`plans/013-phase-12-ci-soak-and-performance-closure.md`) remains in
`implementation handoff` because it still depends on Phase 11 and is the
roadmap-wide closure gate. The corrective Plan 014 is also already available
as an implementation-handoff plan and is not blocked by C011. No future plan
was explicitly marked `blocked` by C011, so no other plan status required
changing.

There is no broad permission-failure warning path in control-server startup:
permission or ownership verification raises `ControlServerError`, closes the
listener, and removes only the instance-owned socket when its identity is
known.
