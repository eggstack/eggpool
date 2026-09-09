# O010 Closure — Differential Qualification and M9 Closure

Status: closed

Recommendation: accept O010 closure; M9 is closed. M10 is eligible for its
own planning/implementation review. No M10 implementation plan is promoted
automatically.

Implementation commit: [`f41489c`](https://github.com/eggstack/eggpool/commit/f41489c)

Plan: [O010 — differential qualification and M9 closure](../../implementation/operations/010-differential-qualification-and-m9-closure.md)

## Outcome

O010 completed the M9 qualification pass and found one bounded correctness
defect: the O004 mutation guard was process-global, so unrelated config files
could incorrectly receive `Busy` during parallel operation. The guard is now
keyed by the canonicalized target path. Same-file overlap remains fail-closed
as `Busy`, while independent config roots proceed concurrently. O010 also
added the two-sided command-help corpus, explicit O010 process-task evidence,
and refreshed the historical R011 inventory assertion for the now-real O008
update checker.

No new dependency, schema, public listener, scheduler, Python fallback, or
M10/M11 behavior was introduced.

## Command implementation coverage

The frozen O001 matrix contains 63 command paths. `rust/tests/cli_contract.rs`
continues to assert exact parser paths/options, and
`tests/migration_rs/test_o010_operations.py` launches both implementations
against every path's help probe. All 63 paths reached a real Rust parser/help
surface; no supported path emitted migration-stage `NotImplemented`.

The two stdlib watchdog fast paths (`croncheck` and `ensure-running`) do not
expose Python Click help probes, so their O010 assertion checks the Rust help
surface directly. This is the only explicit help-probe normalization. The
exact `version` observation remains byte-for-byte equal.

## Differential and regression evidence

| Boundary | Verification | Result |
|---|---|---|
| O010 two-sided command corpus | `rtk uv run pytest tests/migration_rs/test_o010_operations.py -q --tb=short --maxfail=1` | 2 passed |
| Full migration oracle | `rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1` | 102 passed, 3 skipped |
| Targeted Python M9 bundle | CLI/control/runtime/backup/update/deploy/integration pytest bundle | 516 passed, 3 skipped |
| Full Rust target corpus | `rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1` | 440 passed across 52 suites |
| Rust compile/lint | all-target no-run, Cargo fmt check, Cargo clippy `-D warnings` | Pass |
| Python project gates | Ruff format/check, Pyright, and smoke tests | 733 formatted; 14 smoke passed |

The focused Rust regression suites for F003/F004/F006, provider transport,
routing/catalog/health, coordinator C007-C014, runtime R006-R013, and O002-O009
were also run serially and passed. No paid or live provider was used.

## Gate evidence

### Lifecycle, races, and mutation safety

- O002/O003 plus R006-R013 cover foreground/detached lifecycle, PID/socket
  collision and stale-state recovery, control framing, reload publication,
  retirement, shutdown, watchdog startup, and task convergence.
- C010/C011 and the coordinator finalization/stream suites cover restart
  reconciliation, finite/stream handoff, cancellation, retained finalization,
  and no replay after downstream handoff.
- O004 covers invalid candidates, environment-owned secrets, unrelated TOML
  preservation, and typed live-apply/restart outcomes. The new concurrent
  regression runs eight independent config mutations in parallel; same-file
  mutation contention retains the `Busy` contract.
- No raw secret, request body, or provider credential appears in the new
  qualification observations or diagnostics.

### Backup, recovery, update, and deployment

- O006 and its Python counterparts cover canonical migrations, SQLite snapshot
  backup, collision-safe archives, allowlisted members, metadata validation,
  traversal rejection, atomic restore, and recovery without DB reset.
- O008 covers latest/exact/v-prefixed resolution, check-only behavior,
  malformed/oversized metadata, digest/version verification, staged replacement,
  rollback, restart failure, concurrent/update-checker behavior, and config/DB
  immutability.
- O009 covers deterministic systemd/cron/backup-cron/logrotate rendering,
  argv-only fake command execution, mandatory-command failures, install
  idempotence, uninstall keep flags, symlink/path refusal, and bounded
  temporary-root acceptance. No host `/etc`, systemd, cron, user, or production
  HOME was mutated. Rootful Linux/SBC characterization remains M10 scope.

### Background task closure

`rust/tests/operations_o010.rs` proves that a configured process runtime
registers exactly one callback for each of `metrics_flush`, `update_checker`,
and `automatic_backup`; all six inventory rows are registered with no deferred
owner/reason. O006/O007/O008 exercise the three callbacks, while R006-R013
prove the shared supervisor's singleton, reload, generation, and shutdown
boundaries. R011's old “update checker deferred” assertion was corrected to
the accepted post-O008 contract.

### Resource, dependency, and security review

- The new mutation state is one bounded in-process `BTreeSet<PathBuf>` and is
  released by an RAII guard; it does not retain file contents or diagnostics.
- The complete M9 diff adds no Cargo dependency, migration, config field,
  HTTP listener, ORM, second scheduler, second provider HTTP stack, or Python
  execution fallback.
- Backup/update/deploy code remains under the previously reviewed allowlists,
  staged replacement, symlink checks, and bounded archive/member limits.
- No shell files changed; ShellCheck is therefore not an applicable changed
  boundary. A direct repository-wide probe reports pre-existing findings in
  `scripts/install.sh` and is not attributed to O010.

## Unresolved findings and boundaries

No unresolved high/medium M9 correctness, security, resource, compatibility,
lifecycle, packaging, or data-loss finding remains. The following are
intentionally not claimed by O010:

- M10 broad OS/architecture/SBC characterization, live-provider qualification,
  dashboard visual review, and complete cross-system characterization;
- M11 public Rust-default release/install/update cutover and release assets;
- M12 Python production/runtime retirement.

## Registry and roadmap transition

O010 is removed from the dependency-ready queue and recorded as completed in
`migration-rs/registry.md`. M9 is marked closed after O010. M10 is the sole
next eligible milestone for a separate planning/implementation review; no
future implementation plan is currently promoted, and M11/M12 remain sequenced
behind M10 and their own reviews. The O001-O009 historical closure records are
unchanged.
