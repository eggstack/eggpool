# O008 Closure — Update, Version Resolution, and Update Checker

Status: closed

Recommendation: closed; O009 is dependency-ready.

Implementation commit: [`c9b63d5`](https://github.com/eggstack/eggpool/commit/c9b63d5562cbfa515ae4dfb6cfa7ba688f82b14e)

Plan: [O008 — update, version resolution, and update-checker background task](../../implementation/operations/008-update-version-and-update-checker.md)

## Outcome

O008 adds a Rust-native update service to the migration candidate. Rust
resolves releases from the future M11 GitHub Releases authority, uses direct
Hyper/Rustls traffic, verifies a compatible artifact before installation, and
performs a rollback-capable executable replacement. The Python/PyPI behavior
remains the exact oracle for command semantics and version ordering; Rust does
not invoke pip, pipx, uv, PyPI, or a Python subprocess.

## User-level parity matrix

| Contract | Rust behavior | Evidence | Result |
|---|---|---|---|
| `version` and current version | Cargo package metadata supplies `PACKAGE_VERSION`; update comparisons use the same value | `rust/src/version.rs`, `UpdateService::current_version` | Pass |
| Latest target | `ReleaseTarget::Latest` reads the centralized GitHub Releases `/latest` endpoint and rejects drafts/pre-releases | `ReleaseClient::resolve`, O008 loopback metadata test | Pass |
| Exact target | Optional leading `v` is normalized; exact tags are compared by typed release keys | `ReleaseVersion`, `ReleaseTarget`, version unit tests | Pass |
| Invalid/missing exact release | Invalid syntax is rejected before network work; HTTP/status/JSON failures are typed and non-zero at the CLI | `UpdateError`, `runtime::update` | Pass |
| Current/no-op and downgrade | Current latest is a no-op; exact older releases remain valid targets; exact current is a no-op | `runtime::update`, version ordering tests | Pass |
| `--check` | Resolves and compares only; it does not validate, lock, download, stop, rename, or restart an executable | `runtime::update` check branch; check-only service tests | Pass |
| Apply/restart | Managed executable is validated, the prior running state is observed, and only a previously running server is stopped/restarted | `server_is_running`, O003 lifecycle calls, `ApplyReport` | Pass |
| Config/database safety | Update code never opens or writes config/database paths; replacement tests preserve both byte-for-byte | `verified_replacement_self_checks_and_preserves_other_files` | Pass |
| Failure categories | Metadata, artifact, integrity, install-path, self-check, replacement, restart, and concurrency failures have distinct secret-free categories | `UpdateError::category`, CLI projection | Pass |

## Artifact and integrity contract

The release API is centralized as
`https://api.github.com/repos/eggstack/eggpool/releases`. A release must expose
the exact raw executable asset:

```text
eggpool-{version}-{os}-{arch}
```

`version` is the normalized release tag without the optional leading `v`, and
`os`/`arch` are the Rust target identity of the running candidate. The asset
metadata must carry a `digest` value in `sha256:<64 hex digits>` form. The
client rejects missing or malformed evidence, verifies the downloaded bytes,
checks the declared size, caps metadata at 2 MiB and artifacts at 128 MiB, and
never executes bytes before the staged self-check. GitHub release/download
redirects are limited to the originating host or the explicit GitHub release
asset hosts. No custom signing scheme was introduced; M11 may add signed
manifest verification behind the same descriptor boundary.

No provider/account proxy or credential is used for release traffic. The
release client has bounded connect, per-read, overall, response-size, and
redirect limits and does not automatically retry.

## Replacement and fault matrix

| Fault point | Behavior | Recovery/evidence |
|---|---|---|
| Before download / unsupported path | Regular, owned, non-hardlinked executable and writable sibling directory are required | Typed `unsupported_install_path`/`unsafe_executable`; no download or file mutation |
| Stage write or download cleanup | Staging uses create-new sibling files and private mode | Partial stage is removed; current executable remains unchanged |
| Integrity or staged self-check | SHA-256, size, executable mode, and `eggpool version` output are checked | No rename occurs; current executable remains unchanged |
| Old-binary rename | Old binary is moved to a PID-scoped rollback path only after revalidation | Rename failure is terminal and leaves the old binary in place |
| New-binary rename | Staged binary is atomically renamed into the executable path | Failure attempts immediate old-binary restoration |
| Installed version verification | Installed path is self-checked again | Failed verification removes the new path and restores the old binary |
| Restart | Previously running process is restarted only after verification | Restart failure restores the old binary and makes a best-effort old-binary restart |
| Rollback cleanup / concurrency | Rollback is removed only after verification/restart; create-new lock rejects overlap | Actionable rollback remains on cleanup failure; concurrent apply returns `update_in_progress` |

Symlinks, hard links, unsafe ownership, hostile artifact names, untrusted
redirect hosts, and source-checkout executable layouts are refused. Staging
and lock files are private and cleaned on normal/error paths. The source
checkout/read-only failure is intentional until an M11-managed Rust install
exists.

## Background task registration

`ProcessRuntime` creates one process-owned `UpdateCheckerState`, registers one
`update_checker` callback in the existing `RuntimeTaskSupervisor`, and exposes
its bounded latest snapshot through authenticated `/api/stats/update`. The
callback runs check-only work, records only current/latest versions, a Unix
timestamp, availability, and a bounded error category, and swallows metadata
failures without affecting provider generations or shutdown.

The task retains the O001 schedule: `update_checker.enabled` gates an
immediate first check followed by 86,400-second intervals. Its callback is
available during initial task installation and R007 candidate staging, so
other reloads cannot drop an active checker or create a second one. The
frozen O001 config policy intentionally keeps `update_checker.enabled`
startup-only (`RestartRequired`); task-spec reconfiguration remains owned by
R007 rather than a private timer loop. R008's deferred inventory is now
reduced to no update-checker deferral; O006 and O007 retain their existing
callbacks.

## Dependency and security review

- No Cargo dependency or SQLite schema/migration was added.
- Hyper/Rustls is reused; Reqwest and package-manager subprocesses are absent.
- Release URLs and redirects are validated before requests; metadata and body
  reads are bounded and status/JSON failures are typed.
- Asset selection is exact-name based; URL hosts, digest syntax, byte size,
  executable ownership/link count, mode, and staged version are checked.
- The updater does not inherit provider routing, account keys, config secrets,
  database handles, or arbitrary metadata strings in diagnostics.
- Replacement uses sibling create-new staging/lock paths, revalidates the
  target before rename, retains a rollback until restart policy completes,
  and never modifies config/database/provider templates.
- Exact-version downgrades are explicit; latest checks only strict typed
  ordering and GitHub `/latest` remains stable-release-only.

## Verification evidence

Commands actually run:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check                         PASS
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings         PASS
rtk cargo check --manifest-path rust/Cargo.toml --all-targets                         PASS
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o008 -- --test-threads=1 PASS (7)
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o003 -- --test-threads=1 PASS (2)
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1 PASS (4)
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o007 -- --test-threads=1 PASS (4)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1 PASS (9)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r006 -- --test-threads=1 PASS (10)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r007 -- --test-threads=1 PASS (6)
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1 PASS (3)
rtk uv run pytest tests/unit/test_update_checker.py tests/unit/test_connect.py -k 'update or version' -q --tb=short --maxfail=1 PASS (53)
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1 PASS (100 passed, 3 skipped)
rtk git diff --check                                                             PASS
```

The serial `cargo test --all-targets -- --test-threads=1` aggregate was also
attempted, but the local runner stopped exposing its test process/output and
the invocation was abandoned. The all-target compilation, Clippy, and every
affected O003/O006/O007/R005-R008/O008 suite passed individually; this is
recorded as a runner limitation rather than represented as a passing aggregate.

## Unresolved M11 release prerequisites

O008 does not publish Rust assets or change the public installer. M11 must
publish the exact per-target asset names and SHA-256 metadata, decide whether
to add signed-manifest verification, qualify the supported release matrix,
and change the primary installation/update documentation to make Rust
canonical. Until then, check/resolution behavior is usable against a matching
authority fixture, while missing compatible assets fail explicitly.

## Registry transition

O008 is removed from the dependency-ready queue and recorded as closed.
Exactly O009 is promoted to `dependency-ready; O008 closure accepted`.
O010 remains queued behind O009, and M10 remains blocked on accepted O010
closure and its own planning/implementation review. No M11 or M12 plan is
unblocked by O008.
