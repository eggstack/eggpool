# Q006 Closure — Disposable Rootful Linux Operational Acceptance

Status: accepted; closed 2026-09-09

Plan: [Q006 — disposable rootful Linux operational acceptance](../../implementation/qualification/006-rootful-linux-operational-acceptance.md)

Implementation commit: `c090ca0d`

Machine-readable evidence: [`006-run.json`](006-run.json)

Evidence SHA-256: `b44e34f2aff4749c5c61f5f414bf485e3cd72c5671a23308ae2193a24df34333`

Candidate SHA-256: `af91726e113703a5dfc6e0360c9941977ac9b91fba002d7835b1d7ad78bc8fde`

## Outcome

Q006 passed on a disposable Ubuntu 24.04.4 LTS Linux ARM64 host with systemd
255 as PID 1. The reusable guarded runner is
[`scripts/qualification_rootful_linux.py`](../../../scripts/qualification_rootful_linux.py).
It requires explicit disposable-host acknowledgement, effective root, Linux,
systemd, and the required OS tools; it uses only a loopback provider and
cleans its ownership-marked paths in a `finally` path.

Both personal and production systemd modes installed, started, served health
and readiness successfully, served authenticated model metadata, completed
finite and streaming loopback inference, rehashed, restarted, and recovered
after the service process was killed. The final result was `pass` with 41
bounded command records.

## Environment

| Fact | Observed value |
|---|---|
| OS | Ubuntu 24.04.4 LTS |
| Kernel / architecture | 6.8.0-1064-raspi / aarch64 |
| systemd | 255.4-1ubuntu8.17, PID 1 |
| logrotate | 3.21.0 |
| cron | `crontab` available; version query unavailable |

No physical-machine identifier, credentials, raw request bodies, or full
environment/config dump is present in the evidence.

## Acceptance matrix

| Area | Result |
|---|---|
| Personal systemd | pass; `Type=simple`, non-root service user, health/readiness/models 200, finite and streaming inference 200 with terminal evidence |
| Production systemd | pass; dedicated `eggpool` user/group, hardened unit, same service and inference checks |
| Cron/watchdog | pass; install, idempotent reinstall, unrelated crontab preservation, runtime path, `croncheck`, managed removal |
| Backup cron | pass; wrapper installed and backup command produced an archive |
| Logrotate | pass; managed configuration installed and validated by the real command |
| Keep flags | pass; config, data, state, and unit were each retained by the all-keep exercise before final removal |
| Repeated uninstall | pass; second production uninstall against absent artifacts was a no-op |
| Cleanup | pass; all managed paths and the temporary production user were absent after both uninstall and repeat-uninstall checks |

Observed production ownership/modes included root-owned 0644 unit, cron, and
logrotate files; `root:eggpool` 0755 config directory; `eggpool:eggpool`
0750 data/log directories; private backup directory; root-owned 0640 config
and env files; and a root-owned 0755 backup wrapper and candidate. The backup
archive was owned by `eggpool:eggpool` with mode 0600. The personal keep-flags
check observed root-owned 0644 config/unit files and user-owned 0750 data and
0700 state directories.

## Boundary corrections and regressions

Real OS acceptance found and corrected deployment defects: personal services
now receive the deploying user’s `HOME`; production services can write the
documented backup directory under `ProtectSystem=strict`; seeded production
config/env files are explicitly `root:eggpool`; absent systemd units make
repeated uninstall a no-op; and the runner waits for service HTTP readiness,
uses the service main process for forced-kill recovery, and places the
production candidate at a hardened executable path.

Deterministic regressions cover personal `HOME`, production systemd hardening
and writable paths, absent-service uninstall, command-boundary failure
handling, path/symlink safety, and the host-portable Q005 crash contract.
The O002/O003/O006/O009 suites and the migration suite passed after these
corrections.

Expected non-zero `systemctl is-active` polling records occurred while waiting
for the killed personal service to recover; the subsequent bounded poll passed
and the aggregate runner result was pass. No unresolved high or medium
operational/security finding remains.

## Exact verification

```text
cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o003 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --all-targets --no-run
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
cargo fmt --manifest-path rust/Cargo.toml --check
cargo clippy --manifest-path rust/Cargo.toml -- -D warnings
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
git diff --check
sudo -E env PATH="$PATH" uv run python scripts/qualification_rootful_linux.py \
  --binary rust/target/release/eggpool \
  --output migration-rs/closure/qualification/006-run.json \
  --i-understand-disposable-host
```

Results: O002 8 passed; O003 2 passed; O006 4 passed; O009 8 passed;
all-target compile passed; migration suite 140 passed and 4 skipped; smoke
suite 14 passed; Ruff/Clippy/Pyright/format checks passed; and the disposable
runner passed.

## Registry transition

Q006 is formally accepted and closed. Per the plan, only Q007 is promoted:
Q007 is now the sole dependency-ready plan; Q008, Q009, and Q010 remain queued
behind their direct predecessors. M11 remains blocked on accepted Q010 and its
separate planning review.
