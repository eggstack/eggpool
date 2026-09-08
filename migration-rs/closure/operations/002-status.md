# O002 Closure — Local Control, Runtime Paths, and Process State

Status: closed

Recommendation: closed; O003 is dependency-ready.

Implementation commit: [`32f7e8c`](https://github.com/eggstack/eggpool/commit/32f7e8c220f3797223bc534afa3b620063dc6a83)

Plan: [O002 — local control, runtime paths, and process-state boundary](../../implementation/operations/002-local-control-runtime-paths-and-process-state.md)

## Requirement-to-evidence matrix

| Requirement | Evidence | Result |
|---|---|---|
| Canonical runtime paths and precedence | `rust/src/operations/paths.rs` provides a side-effect-free `RuntimePaths` snapshot, explicit environment/XDG/cwd precedence, and separate mutating directory helpers; `path_resolution_is_precedence_ordered_and_read_only` covers isolated XDG roots and no-create resolution | Pass |
| Private path and file safety | Runtime/state directories are owner-only and mode `0700`; PID files are atomically published as mode `0600`; the control socket is restricted to mode `0600`; the focused suite asserts these modes and rejects unsafe/non-directory collisions | Pass |
| PID/process-state boundary | `rust/src/operations/process.rs` separates PID existence, health, and control observations; stale/malformed PID cleanup is conservative; TERM requires PID-file plus independent EggPool evidence; reused/unproven PIDs are refused | Pass |
| Health and process classifications | `HealthProbe`, `ControlProbe`, and `ProcessState` distinguish healthy ownership, stale PID, healthy unknown owner, stopped, and ambiguous/error state; hostname and wildcard bind addresses are handled without treating a PID alone as proof | Pass |
| Frozen local-control protocol | `rust/src/operations/control.rs` preserves protocol version 1, `reload_config`, request-id echo, validated digest, structured reload fields, one request/response per connection, bounded frames, and typed timeout/protocol categories | Pass |
| Framing and malformed-client isolation | `operations_o002.rs` covers empty, missing newline, invalid object/JSON shape, multiple frames, bad digest/request ID/command/params/version, oversized input, and accept-loop survival; bad clients never invoke the handler | Pass |
| Socket lifecycle and collision safety | Listener startup validates the private parent, applies `0600`, refuses live sockets and regular-file collisions, removes only an identity-matching stale socket, and closes/unlinks on graceful shutdown | Pass |
| M8 reload authority | The server adapter calls the existing `ReloadService` and projects `ReloadResult`; it does not duplicate diff, staging, publication, policy, or diagnostic state. R007/R010/R012/R013 regressions remain green, including Busy, cancellation, authority, diagnostics, and shutdown behavior | Pass |
| Disconnect, timeout, and retained work | Per-connection response/write failures are isolated; client timeout is bounded; a disconnected client does not cancel the retained handler. The M8 reload worker remains the owner of retained reload work | Pass |
| Startup/shutdown integration | `server::run_with_digest` starts control only after database, generation, and initial task readiness; listener failure is startup-fatal and shutdown closes the control listener before M8 resources | Pass |
| Secret-free local boundary | Protocol errors and control failures use bounded generic displays; raw request frames are not logged or reflected; the malformed digest test includes a synthetic secret token without exposing it in the response | Pass |

## Protocol and path parity matrix

| Surface | Frozen O001 behavior | Rust implementation/evidence |
|---|---|---|
| Config path | Explicit `EGGPOOL_CONFIG`, then existing XDG config, then cwd config | `RuntimePaths::resolve_with`; isolated precedence test |
| State/runtime paths | XDG/home-derived state; explicit runtime override; no implicit creation during lookup | `RuntimePaths`, `ensure_state_dir`, `ensure_runtime_dir` |
| PID/log/socket files | Explicit overrides where defined, private state/runtime placement, stable filenames | `RuntimePaths` fields and process/socket tests |
| Control frame | One bounded newline-terminated JSON request, one response, then close | `MAX_REQUEST_BYTES`, `read_frame`, `read_response_frame`, malformed/framing tests |
| Request | Version, safe request ID, exact `reload_config`, optional validated SHA-256 digest | `ControlRequest::parse_frame` and request round-trip |
| Response | Echoed ID, `ok`, stage, generation, changed sections, warnings, restart-required paths, retirement state, message | `ControlResponse::from_reload` and M8 result projection |
| Local security | Private parent/socket, live-socket refusal, stale recovery, no TCP management port | Unix metadata, collision/recovery tests, no HTTP control surface added |

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check                         PASS
rtk run 'CARGO_TARGET_DIR=/tmp/eggpool-o002-cargo cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings' PASS
rtk run 'CARGO_TARGET_DIR=/tmp/eggpool-o002-cargo cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1' PASS (7 tests)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r007 -- --test-threads=1 PASS (6 tests)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1 PASS (5 tests)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r010 -- --test-threads=1 PASS (4 tests)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r012 -- --test-threads=1 PASS (10 tests)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r013 -- --test-threads=1 PASS (7 tests)
rtk run 'CARGO_TARGET_DIR=/tmp/eggpool-o002-cargo cargo test --manifest-path rust/Cargo.toml --all-targets' PASS (all targets; no failures)
rtk uv run pytest tests/unit/test_control_server.py tests/unit/test_runtime_paths.py tests/unit/test_runtime_daemon.py tests/unit/test_lifecycle_cli.py tests/unit/test_fastcli.py tests/unit/test_cli_rehash_format.py tests/unit/test_cli_rehash_helper.py tests/unit/test_cli_rehash_preflight.py tests/unit/test_cli_runtime_status.py tests/unit/test_api_runtime.py -q --tb=short --maxfail=1 PASS (236 passed, 1 warning)
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1 PASS (100 passed, 3 skipped)
rtk uv run ruff format --check src/ tests/ scripts/ PASS (732 files)
rtk uv run ruff check src/ tests/ scripts/ PASS
rtk uv run pyright src/ scripts/ PASS (0 errors, 0 warnings)
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1 PASS (14 passed)
rtk git diff --check PASS
```

The aggregate Rust run used an isolated `CARGO_TARGET_DIR` because another
workspace test process held the shared Cargo build lock. The isolated run
completed every target successfully, including O002 and all M8 regressions.

## Failing-before/passing-after evidence

No prior O002 Rust target existed, so there is no meaningful failing-before
test to report. The implementation adds the focused `operations_o002` target;
its final run passes all 7 tests, and the complete pre-existing Rust, Python,
and migration suites remain green after the new operations module and direct
`nix` dependency were added.

## Dependency, schema, and security review

O002 adds only the narrow direct dependency `nix 0.31.3` with `signal` and
`user` features. Rust's standard library does not expose safe arbitrary-PID
`kill` calls, and this crate forbids unsafe code; the already-transitive safe
wrapper was promoted directly rather than adding a process-management stack.
No database schema or migration changes were made. The control path is Unix
filesystem-socket-only, uses owner/private checks, bounds request and response
work, avoids raw-frame logging, and preserves the existing M8 authority and
retained-task ownership.

## Known limitations and non-goals

- The transport remains Unix-only by the accepted O002 contract; unsupported
  platforms return typed errors.
- `runtime-status` remains authenticated local HTTP and lifecycle commands are
  O003 concerns; the control socket exposes only `reload_config`.
- Caller-owned server helpers without a config-file path do not invent a
  reload source; O003's lifecycle entrypoints supply the path through
  `run_with_digest`.
- No daemon detach, systemd/cron, backup/update, provider work, or M10
  platform qualification is included.

## Unresolved findings

None. The only new dependency is narrowly justified above; no schema,
authority, cancellation, stale-state, secret-redaction, or protocol issue
remains open for O002. Later defects must be recorded as corrective O011+
plans rather than by rewriting this closure record.

## Planning transition

O002 is removed from the dependency-ready section and recorded as closed in
the implementation index, registry, roadmap, and handoff sequence. Its hard
dependency, accepted O001, is closed. O003 is the only plan promoted to
dependency-ready; O004-O010 remain queued behind their direct predecessors.
M9 remains active, and M10 remains gated on accepted O010 closure.
