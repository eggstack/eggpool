# Plan 094 — Backup I/O and SBC Profile

Date: 2026-08-10
Status: planned
Parent roadmap: `plans/093-sbc-runtime-and-maintenance-simplification-roadmap.md`
Planning baseline: `ad7eee822f1dfb8c43dfbe20410c41009697cd7d`

## Purpose

Prevent automatic runtime backups from blocking EggPool's canonical event loop on full-file archive work, and make the SBC example's backup defaults/documentation consistent with its low-wear/minimum-footprint goal.

This plan is intentionally narrow. It does not redesign the backup format, restore UX, scheduler, retention policy, or database durability model.

## Confirmed current behavior

Relevant files/functions:

- `src/eggpool/background/backup.py`
  - `automatic_backup_loop()`
  - `run_backup_once()`
- `src/eggpool/lifecycle/backup.py`
  - `create_runtime_backup()`
  - `create_backup()`
  - `_build_archive()`
  - `prune_backups()`
- `config.sbc.example.toml`
- corresponding backup unit/integration tests and deployment/operations documentation.

`create_runtime_backup()` moves the live SQLite snapshot operation to `asyncio.to_thread()`, but then calls synchronous archive construction after returning to the event loop. `_build_archive()` copies the staged SQLite snapshot into an uncompressed ZIP and performs blocking filesystem reads/writes. Staging-directory cleanup is also synchronous. A large database on microSD or slow local storage can therefore stall the only server loop.

The SBC example enables automatic backup every 24 hours with seven retained copies while describing itself as low-wear/minimum-footprint. Full snapshot + archive copying is a qualitatively larger storage event than ordinary buffered metrics writes.

## Goals

1. Ensure all potentially large filesystem work for runtime backup executes off the canonical asyncio event-loop thread.
2. Preserve a consistent SQLite snapshot using the existing stdlib backup mechanism.
3. Preserve archive format and restore compatibility.
4. Preserve bounded failure isolation: backup failure must log/report through the existing supervised task path without crashing proxy operation.
5. Reconcile the SBC example so its default is truthful for a low-wear/minimum-footprint appliance.
6. Avoid creating a new executor, backup worker service, queue, or background process.

## Non-goals

- compressing the database with a new codec;
- changing archive format/version unless strictly required for compatibility;
- remote/cloud backups;
- incremental/differential backup;
- backup encryption;
- backup streaming protocol;
- new backup database/schema;
- automatic VACUUM before/after backup;
- benchmarking backup throughput in CI;
- changing manual `eggpool backup` / restore behavior beyond the shared implementation necessary for event-loop safety.

## Workstream A — Establish the blocking boundary

Inspect `create_runtime_backup()` and direct callers and document which operations may scale with database size:

1. source SQLite snapshot copy;
2. ZIP/archive creation and copy of staged DB/config/env;
3. filesystem metadata calls involved in archive publication;
4. staging directory deletion;
5. retention pruning.

Do not overreact to tiny `Path.stat()`/directory operations. The objective is to move **database-size-proportional** work off the loop.

Use the existing `asyncio.to_thread()` model. Prefer one coarse synchronous helper that owns the staged snapshot/archive/cleanup sequence over multiple tiny thread crossings if this keeps lifecycle cleanup explicit and testable.

## Workstream B — Move full-file work off loop

Refactor `create_runtime_backup()` so the event-loop thread does not perform full-file archive creation or staged-database deletion.

Preferred shape:

- keep async orchestration at the public boundary;
- execute the blocking SQLite snapshot and archive publication inside `asyncio.to_thread()` either as one bounded synchronous function or two clearly separated calls;
- ensure `finally` cleanup of staging files occurs in the blocking helper/thread as well when deletion can scale with file count/size;
- propagate exceptions to the existing caller so the supervisor retains current failure handling.

Do not create an unbounded thread pool. Use the default executor through `asyncio.to_thread()`.

## Workstream C — Preserve atomic/restore behavior

Verify that the refactor does not change these invariants:

- the live database is never copied by naïvely reading the database/WAL files while writes are active;
- `sqlite3.Connection.backup()` remains the consistency mechanism;
- archive publication remains temp-file then rename/replace;
- archive metadata continues to record the live DB target, not the staging path;
- optional `.env` inclusion follows the existing security/config contract;
- retention pruning still happens only after a successful archive creation;
- failed archive creation removes its temporary archive/staging state best-effort and does not publish a partial final archive.

## Workstream D — SBC profile decision

Review `config.sbc.example.toml` and the user-facing description around automatic backup.

The preferred default for the explicitly low-wear/minimum-footprint SBC example is:

```toml
[backup]
enabled = false
```

Keep documented instructions showing how an operator can enable daily backups deliberately.

If existing product documentation treats automatic backup as mandatory for the SBC profile, do not silently retain the contradiction. Instead either:

- change the profile language from minimum-footprint/low-wear to a balanced profile and document the write cost; or
- disable backup by default and document the reliability trade-off.

For this roadmap, default-off is preferred because systemd-supervised local appliances can opt in explicitly and Roadmap 093 is specifically a low-wear simplification pass.

Do not change the general/default config's backup setting unless its current behavior is demonstrably inconsistent.

## Workstream E — Focused tests

Add or update focused tests that prove behavior rather than implementation details:

1. runtime backup still produces a valid restorable archive containing the expected SQLite snapshot;
2. archive publication remains atomic on failure;
3. backup exceptions propagate to `run_backup_once()` / supervisor handling and do not crash unrelated runtime tasks;
4. event-loop safety test: monkeypatch/instrument the blocking archive helper to prove it executes on a non-event-loop thread. Do not add timing sleeps or brittle latency thresholds;
5. SBC config validation passes with the new backup default;
6. no automatic backup task is registered for the SBC profile when backup is disabled.

Prefer thread-identity assertions or a deterministic blocking sentinel over wall-clock tests.

## Documentation changes

Update only docs made stale by this plan:

- `config.sbc.example.toml` comments;
- architecture/deployment/operations backup notes if they state the SBC profile enables backup;
- `AGENTS.md` lean-default summary if necessary;
- changelog only if the shipped SBC example/default behavior is user-visible.

Do not add a new backup design document.

## Verification

Run focused backup/config tests first, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.sbc.example.toml check-config
```

If a representative local database is available, one manual backup while issuing local proxy requests may be used as a non-gating sanity check. Do not create a permanent latency benchmark.

## Acceptance criteria

- [ ] `create_runtime_backup()` no longer performs database-size-proportional archive copy work on the canonical event-loop thread.
- [ ] Staged database cleanup that may involve large files does not block the event loop.
- [ ] SQLite snapshot consistency still uses `sqlite3.Connection.backup()`.
- [ ] Final archive publication remains atomic; no partially built final archive is exposed after failure.
- [ ] Runtime backup failures remain isolated and observable without terminating ordinary proxy service.
- [ ] Backup retention still runs only after successful archive publication.
- [ ] The existing backup format and restore path remain compatible.
- [ ] The SBC example no longer claims minimum-footprint/low-wear behavior while silently enabling daily full-database backups; default-off is implemented unless the profile is explicitly renamed/documented as balanced.
- [ ] Enabling backup explicitly continues to work without additional services/dependencies.
- [ ] Focused tests prove off-loop execution deterministically without timing thresholds.
- [ ] Existing smoke/config gates pass.
- [ ] No new executor service, queue, worker process, compression dependency, remote backup feature, or automatic VACUUM is introduced.

## Rejection conditions

Reject the implementation if:

- `_build_archive()` or equivalent full-file copy still runs synchronously on the event-loop thread;
- a thread/executor is created per backup rather than using bounded/default executor behavior;
- the refactor switches to raw copying of the live SQLite files instead of a consistent backup snapshot;
- failure can leave a final-name archive that is partial/corrupt;
- the SBC profile continues to describe itself as minimum-footprint while automatic full backups remain enabled without an explicit documented choice;
- tests rely on arbitrary sleeps or hard latency numbers;
- the implementation expands into a generalized backup subsystem.

## Implementation sequence for GPT-5.6 Luna

1. Read Plan 093, this plan, `AGENTS.md`, backup source files, config model, and focused backup tests.
2. Trace every caller of `create_runtime_backup()` and `create_backup()` before changing signatures.
3. Identify the smallest synchronous helper boundary that can run safely in `asyncio.to_thread()`.
4. Refactor without changing archive contents or restore metadata semantics.
5. Add deterministic thread-boundary regression coverage.
6. Change/reconcile the SBC backup default and comments.
7. Run focused tests and config validation.
8. Run the ordinary repository gate.
9. Update this plan with implementation commit and exact verification results; mark complete only when all acceptance items are satisfied.
10. Stop; do not optimize restore memory usage or add new backup features in this phase unless a regression blocks compatibility.
