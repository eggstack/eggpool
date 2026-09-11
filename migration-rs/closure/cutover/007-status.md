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

## Acceptance addendum — 2026-09-11

This append-only addendum records the required Linux requalification. The
original macOS conditional result remains historical and is not rewritten.
K007 is now **accepted/closed** based on fresh execution on the supported
Linux aarch64 systemd host described below. The accepted result promotes only
K008, as required by the plan.

### Implementation commits

- `e8aff344accaed82cc92378b9bc0dd3f4691e586` — original K007 deployment
  transition implementation and guarded disposable-host runner.
- `6ebc27efc00b1990be23f9260d7e18f8b1b18c48` — harden the disposable
  systemd qualification runner for personal-user lifecycle, service ownership,
  package dependency installation, and bounded failure reports.
- `83622501e498ef662b1549fd3e241cc44ab9fec6` — synchronize the canonical
  production deployment asset with the stable `/usr/local/bin/eggpool`
  manager-owned path and its explicit production environment.

### Linux host and candidate artifacts

The run used a disposable rootful Linux environment with systemd as PID 1:

| Item | Observed value |
|---|---|
| OS/architecture | Ubuntu 24.04.4 LTS, Linux aarch64 |
| systemd | 255.4-1ubuntu8.17 |
| Python | 3.12.3 |
| uv | 0.11.32 (aarch64 Linux) |
| pipx | 1.4.3 |
| Rust/cargo | rustc and cargo 1.98.1 |
| Maturin | 1.14.1 |

The exact candidate artifacts were:

| Era/artifact | Size | SHA-256 |
|---|---:|---|
| `eggpool-0.7.4-py3-none-any.whl` | 1,290,868 bytes | `57587749518844d262263350cd8aeb14a47e6af45ea0801694c280c65c621953` |
| `eggpool-0.8.0-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl` | 10,340,957 bytes | `e7bdc2c926588e0a18b88e65feedd03a3ae17af6db3ba29c2702912164e512d5` |
| `eggpool-0.8.0-linux-aarch64` | 22,952,456 bytes | `e384a4392591c65a3b3516b306cdaabb3982ec93887e67fcedafe65ca45176f1` |

The Rust wheel passed the target-aware wheel inspector, and the raw binary
reported `eggpool 0.8.0`. The service transition used the Python and Rust
wheels through the intended manager for each mode; the raw binary was built
and inspected as a candidate artifact, not installed into either service.

### Real systemd transition evidence

Both modes completed the full stateful sequence:

```text
Python package/service -> Rust wheel/service -> Python package/service -> Rust wheel/service
```

- [Personal-mode run report](007-run-personal.json) has status `pass` and
  four active observations. It used a disposable user-scoped systemd unit,
  `systemctl --user`, linger, one venv, and one preserved config/database
  state.
- [Production-mode run report](007-run-production.json) has status `pass` and
  four active observations. It used system-owned pipx with
  `PIPX_HOME=/var/lib/eggpool/pipx`, `PIPX_BIN_DIR=/usr/local/bin`, the
  dedicated `eggpool` user, and the system unit at
  `/etc/systemd/system/eggpool.service`.

For each report, the service was stopped before package mutation and was
active after every target installation. The unit hash and config hash stayed
constant across all four legs. The database existed and reopened on every
leg under the expected service owner; its hash changed only as migrations or
runtime state were written, with no path relocation or database reset. The
reports contain no host identity or secrets. Final cleanup disabled/stopped
the service, removed the disposable user, and left all managed paths absent.

The executed production command sequence was Python install/start/stop,
Rust install/start/stop, Python install/start/stop, Rust install/start, then
disable/cleanup. The personal sequence used the equivalent user-scoped
systemd commands. Both reports preserve the sanitized command traces.

### State, integration, and recovery evidence

| Requirement | Linux/result |
|---|---|
| Stable service executable | pass; personal unit resolves its venv manager path, production resolves `/usr/local/bin/eggpool` |
| Correct target after each leg | pass; each installed package passed its service health/readiness observation |
| Enabled/active service handling | pass; active state was restored after every transition and final cleanup was explicit |
| Config/database preservation | pass; stable paths and hashes recorded across all four legs |
| Production authority | pass; system-owned pipx and explicit service environment, no root-private PATH dependency |
| Cron/backup/logrotate references | pass; deployment integration tests verify stable command references; no executable-path logrotate hook exists |
| Installed-file cleanup | pass; no K007-managed systemd, cron, backup, logrotate, config, data, or log paths remained after the run |

The runner did not inject a live failure into the disposable host. Stop,
validation, start, health/readiness, rollback, and inactive/disabled-state
handling remain covered by the deterministic K005/K007 implementation and
test matrix described in the original closure record. The focused K007 suite
passed with `3 passed, 1 skipped`; the deployment integration suite passed
with `20 passed` after the stable-path asset correction. No new unresolved
high/medium service-cutover, ownership, or data-loss finding was introduced.

### Registry transition

K007 is accepted/closed. K008 is now dependency-ready and is the only future
plan unblocked by this closure. K009 remains queued behind K008; K010 remains
queued behind K009; K011 remains queued behind K010 and explicit production
publish authority; and K012 remains queued behind K011. M12 remains blocked
behind M11/K012.
