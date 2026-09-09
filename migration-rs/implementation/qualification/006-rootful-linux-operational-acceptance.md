# Q006 — Disposable Rootful Linux Operational Acceptance

Status: accepted; closed 2026-09-09

Source roadmap: `migration-rs/subsystems/qualification-roadmap.md`

Repository baseline: planning baseline `00dd27fa103e3c663968ecd95d9289c60fca0601`; implement against current main after accepted Q005.

Primary class: invariant/capability

Hard dependency: accepted Q005.

## Objective

Qualify the O009 deployment and O003 lifecycle behavior on a real disposable Linux system with actual systemd/process/user/filesystem semantics. Fake command runners remain unit evidence; Q006 verifies the operating-system boundary they cannot model.

## Environment requirements

Use a disposable Linux VM/host that can safely permit root operations and has:

- systemd as PID 1 for systemd acceptance;
- a non-root test user with sudo/root escalation available to the qualification procedure;
- cron/crontab if available for watchdog/backup cron acceptance;
- logrotate if available;
- isolated EggPool config/data/log/backup roots or a disposable machine where reviewed production paths may be used;
- no valuable pre-existing EggPool installation.

Record distribution/version/kernel/architecture/systemd/cron/logrotate versions and candidate SHA.

Do not run this against a maintainer's production server.

## Acceptance sequence

### Preflight

- build/copy the Rust candidate without changing public install authority;
- create minimal valid config using deterministic loopback provider where practical;
- verify `check-config`, migrations and foreground health first;
- assert no pre-existing managed service/cron/unit paths conflict.

### Personal systemd flow

Exercise the supported personal install semantics from O009:

- render unit;
- install/start;
- inspect actual unit contents/mode/ownership;
- health/readiness through the service;
- runtime-status/rehash;
- graceful restart;
- process kill/recovery behavior where service restart policy is part of contract;
- repeated install idempotence;
- stop/uninstall behavior.

### Production systemd flow

Where Q001/O009 marks it mandatory:

- dedicated `eggpool` system user/group creation when absent;
- `/etc/eggpool`, `/var/lib/eggpool`, `/var/log/eggpool`, `/var/backups/eggpool` modes/ownership;
- config/env seed behavior;
- hardened unit publication;
- `daemon-reload`, enable, start;
- service health and deterministic inference;
- restart/rehash/runtime-status;
- log file ownership and writability;
- backup command and backup directory ownership;
- clean uninstall/removal behavior according to flags.

### Cron/watchdog

If cron is available:

- install managed watchdog block;
- verify unrelated crontab lines survive;
- repeated install does not duplicate block;
- execute `croncheck`/`ensure-running` behavior against stopped/running server;
- uninstall removes only managed block.

### Backup cron/logrotate

- install/remove backup cron according to O009 contract;
- verify wrapper/script modes and paths;
- run the backup command once through the installed contract if safe;
- install/render logrotate config;
- validate syntax with real `logrotate` when available;
- do not rotate/delete unrelated logs.

### Uninstall/keep flags

Use disposable seeded state to exercise:

- default removal;
- `--keep-data`;
- `--keep-config`;
- `--keep-path`;
- `--deploy-artifacts`;
- repeated uninstall/no-op behavior;
- symlink/path refusal cases in a temporary fixture.

At the end, inspect managed leftovers explicitly.

## Failure scenarios

Exercise real OS failures where safe:

- invalid config before install;
- service start failure from a deliberately invalid loopback/provider-independent setting;
- permission denial on a disposable managed path;
- stale PID/socket before service start;
- existing unit/service takeover behavior;
- missing optional cron/logrotate command;
- mandatory `systemctl` failure;
- interrupted service followed by normal start/reconciliation.

Do not sabotage global system services or package manager state.

## Evidence

Capture bounded, secret-free evidence:

- unit/cron/logrotate hashes or reviewed text where safe;
- `systemctl show/status` selected fields only;
- file mode/uid/gid/path facts;
- process pid/uid/gid and command identity;
- health/status codes;
- backup artifact metadata;
- managed leftovers list after uninstall;
- exact commands/exit codes.

Do not dump full environment or config secrets.

## Automation

Provide a reusable acceptance script or documented command sequence that can run in a disposable VM. It must have a clear preflight and cleanup mode and refuse to run when safety conditions are not met.

A local VM/manual runner is acceptable. Do not require privileged always-on GitHub Actions for M10 closure.

## Required regressions

Any defect found at this OS boundary must gain a deterministic fake/temp-root regression where possible so it does not require root to detect again.

## Verification

In addition to the real host acceptance, run affected local suites:

```text
cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o003 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --all-targets --no-run
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
# Q006 disposable Linux acceptance command
git diff --check
```

## Non-goals

Q006 does not define the public M11 installer/release asset pipeline, mutate a production server, benchmark SBC resources, or run paid providers.

## Closure evidence

Write `migration-rs/closure/qualification/006-status.md` with:

- disposable host metadata;
- personal/production systemd result matrix;
- actual mode/ownership/service observations;
- cron/logrotate/backup-cron results and non-applicable reasons;
- uninstall/keep-flag results;
- real OS findings and deterministic regression added for each;
- cleanup/leftover report;
- unresolved findings and registry transition.

## Acceptance criteria

Q006 closes only when the required Linux deployment modes operate correctly on a disposable real system, repeated install/remove converges safely, no manual DB reset is required, no unsafe managed leftovers remain, and no unresolved high/medium operational/security finding remains.

Accepted Q006 promotes only Q007.

Closure: [Q006 closure record](../../closure/qualification/006-status.md)
