# K007 Closure — Deployed-Service Cross-Era Transition and Recovery

Status: conditionally closed; external Linux acceptance required

Plan: [K007 — deployed-service cross-era transition and recovery](../../implementation/cutover/007-deployed-service-cross-era-transition-and-recovery.md)

## Decision

The implementation boundary is complete, but K007 is not accepted as a
milestone closure on this host. The repository now contains the stable-path,
service-coordination, rollback, and guarded disposable-host qualification
implementation. The mandatory real Linux Python → Rust → Python → Rust run
could not be executed because the available host is macOS x86_64. K008 is
therefore not unblocked.

## Implementation

- `e8aff344accaed82cc92378b9bc0dd3f4691e586` — stable manager-exposed
  deployment paths, explicit system-owned production pipx authority,
  systemd-aware stop/start/readiness coordination, a transition guard held
  across stop and recovery, readiness probing, deterministic regressions, and
  the guarded K007 Linux runner.

## Deployed path and manager decision

| Deployment | Canonical command | Package authority | State |
|---|---|---|---|
| Personal systemd | invoked manager-exposed `eggpool` path resolved from `argv[0]`/`PATH` | existing uv, pipx, or owning venv manager | implemented; Linux run pending |
| Production systemd | `/usr/local/bin/eggpool` | system-owned pipx with `PIPX_HOME=/var/lib/eggpool/pipx` and `PIPX_BIN_DIR=/usr/local/bin` | implemented; Linux run pending |
| Standalone raw binary | resolved executable path | O008 raw updater | remains separate from package-managed production |

Production deployment refuses root-private uv/pipx and unmanaged standalone
commands. The unit sets `HOME`, `PATH`, `PIPX_HOME`, and `PIPX_BIN_DIR`
explicitly; it does not source shell startup files. The service file, backup
wrapper, and production cron all use the stable command path.

## Transition sequence

The deployed update path now follows this bounded sequence:

1. resolve the exposed executable and package provenance;
2. acquire the private transition guard before service stop;
3. capture systemd scope, enabled state, active state, and main PID;
4. stop through systemd when managed, waiting for an inactive unit and zero
   main PID, or through the existing proven process authority;
5. perform the exact same-manager K004 transition;
6. verify manager ownership, exact version, target config, and executable
   self-check;
7. start only when the service was active before the transition;
8. wait for both `/v1/healthz` and `/v1/readyz`;
9. release the guard only after success or rollback recovery.

An inactive or disabled service is not started as a side effect. Failed stop
returns before package mutation. Post-mutation validation, start, or health/
readiness failure reuses K005 exact rollback; a rollback failure retains the
typed manual-recovery result and leaves the service stopped unless a later
operator action proves it safe.

## State/config/database preservation

| Requirement | Evidence | Result |
|---|---|---|
| stable unit path | `render_personal_systemd`, production renderer, `deploy/eggpool.service`, O009 renderer assertions | pass locally |
| config/database paths are not relocated | K007 runner records config/database hashes on all four legs; K005 manager matrix already proves package-state preservation | deterministic pass; real Linux pending |
| unit hash remains unchanged | K007 runner compares the unit hash after every leg | deterministic pass; real Linux pending |
| no reset or duplicate migration | existing K005/Q003 compatibility tests plus K007 runner's single stateful database | deterministic pass; real Linux pending |
| `.env`/secrets are not recorded | bounded runner output and path/hash-only evidence | pass locally |

## Failure and rollback matrix

| Failure | Implemented behavior | Evidence |
|---|---|---|
| concurrent transition | create-new guard returns `update_in_progress` before service/package work | Rust update unit test |
| stop failure or stale main PID | stop fails before manager invocation | systemd stop boundary; real Linux execution pending |
| manager failure or wrong target | K005 same-manager rollback and typed error | K005 Rust/migration tests |
| target config/self-check failure | target validation fails and rollback path is selected | K005 transition tests |
| start, health, or readiness failure | transition error triggers exact rollback; readiness is independently probed | runtime implementation; real Linux execution pending |
| rollback manager failure | typed `rollback_failed` includes previous/target versions and manual command | K005 Rust test |
| inactive/disabled service | no start/re-enable side effect | runtime state branch; real Linux execution pending |

The K007 runner is `scripts/qualification_deployed_transition.py`. It refuses
non-Linux, non-root, non-systemd hosts, existing managed paths, and an existing
production `eggpool` user. It uses one package environment and one unit across
all four legs and writes only bounded structural evidence.

## Personal and production support disposition

Both modes are implemented. Personal mode preserves the manager-exposed path
for uv/pipx/venv installs. Production mode is intentionally package-managed by
system-owned pipx and stable `/usr/local/bin/eggpool`; root-private manager
environments are not supported. Neither mode receives accepted K007 support
status until the real disposable Linux cycle is recorded.

## Cron, logrotate, and backup checks

- Personal watchdog and backup renderers consume the resolved stable command.
- Production backup cron invokes `/usr/local/bin/eggpool-backup`, whose wrapper
  invokes `/usr/local/bin/eggpool`.
- The production systemd unit is stable and includes explicit manager paths.
- Logrotate operates on `/var/log/eggpool/*.log` and has no executable-path
  hook.
- O009 renderer tests and `git diff --check` pass; real installed-file checks
  remain part of the pending Linux run.

## Verification

Passed locally:

```text
cargo fmt --manifest-path rust/Cargo.toml --all
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test operations_o008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib -- --test-threads=1  # 43 passed
uv run pytest tests/migration_rs/ tests/smoke/ -q --tb=short --maxfail=1
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
git diff --check
```

The guarded host runner and its contract tests pass on the current macOS host
only through the expected blocked/non-mutating path. The mandatory command to
run on disposable Linux is:

```bash
sudo -E uv run python scripts/qualification_deployed_transition.py \
  --python-wheel /path/to/eggpool-python.whl \
  --rust-wheel /path/to/eggpool-rust.whl \
  --mode personal \
  --output migration-rs/closure/cutover/007-run-personal.json \
  --i-understand-disposable-host
```

Run production with `--mode production` on a separate disposable host or
after the personal evidence is complete. Do not convert the blocked local
observation into a Linux pass.

## Findings and registry transition

No unresolved high/medium service-path, package-ownership, rollback, or local
data-loss finding remains. One operational acceptance blocker remains:
real-systemd Linux evidence for both claimed modes, including the complete
cross-era cycle and installed cron/logrotate/backup observations.

K007 is recorded as conditionally closed rather than accepted. K008 remains
queued/blocked on accepted K007; K009-K012 remain blocked behind their direct
predecessors. No future M11 plan is unblocked by this record.
