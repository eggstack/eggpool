# O006 — Database, Backup, Recovery, and Automatic Backup

Status: queued behind O005

Source roadmap: `migration-rs/subsystems/operational-cli-lifecycle-roadmap.md`

Primary class: capability/invariant

Hard dependency: accepted O005.

## Objective

Implement `migrate`, `db vacuum`, `backup`, and `recover` in Rust, then replace the R008 deferred `automatic_backup` inventory entry with the real backup callback registered through the single M8 task supervisor.

This plan is data-safety sensitive. It must preserve the existing SQLite migration/checksum authority and freeze/qualify the Python backup archive contract from O001 before writing production archive code.

## Part A — `migrate`

Reuse F004's migration runner and the canonical numbered SQL/checksum set.

Requirements:

- same fresh/current/partially-applied/checksum-mismatch behavior as the oracle;
- serialized DB access using the existing database layer;
- no generated Rust schema source;
- no automatic reset on migration failure;
- clear current/applied result presentation;
- DB remains readable by Python within the supported migration rollback boundary.

M9 must not invent a downgrade engine unless Python already has one.

## Part B — `db vacuum`

Use the existing database maintenance connection path and config settings. Preserve:

- config resolution;
- busy/locked error category;
- successful completion output;
- safe connection close on cancellation/failure.

Do not hold the live server's request path hostage with an unbounded lock. If Python expects vacuum while stopped, enforce that; if live vacuum is supported, use the same contention guard/budget assumptions.

## Part C — backup service

Build one reusable backup service consumed by manual CLI and automatic task.

Freeze and implement the O001 archive contract, including:

- default backup directory and timestamped naming;
- archive format and version/manifest metadata;
- reviewed members (database/config/env or other current members);
- explicit exclusions;
- retention/listing semantics;
- secure file modes;
- deterministic member names independent of absolute host paths.

### Consistent SQLite capture

Do not naïvely copy a live WAL database file. Use the existing rusqlite backup/checkpoint facilities or another SQLite-consistent snapshot mechanism that is compatible with the current database connection design.

The backup operation must be bounded and must not permanently stall request traffic. Record duration/size as bounded diagnostics only.

### Archive safety

Archive creation must:

- reject/normalize member paths to a reviewed relative set;
- never follow unexpected symlinks outside reviewed paths;
- set conservative member permissions;
- cap metadata/member count according to known EggPool members;
- fail closed if a required member changes/disappears during preparation in a way that makes the snapshot inconsistent.

If the frozen format is ZIP and no current dependency safely provides ZIP create/read, a small focused mature archive crate is allowed only with explicit O006 dependency/security review. Do not add a general filesystem utility package.

## Part D — recover service

Recovery is more dangerous than backup. Required phases:

1. resolve/select source using O001 rules;
2. open and validate archive without extracting to final paths;
3. reject absolute paths, `..`, symlinks/special files, duplicate critical members, oversized/unexpected member counts, invalid manifest/version, and corrupt DB;
4. extract reviewed members into a private staging directory;
5. validate staged config and DB migration compatibility;
6. determine/stop running server according to O003/O001 contract;
7. create a safety backup of current state if Python does so and it can be completed safely;
8. atomically replace files in deterministic order with rollback/retained originals until the new state is validated;
9. restore reviewed ownership/modes;
10. restart only when the current command contract requires it.

A failure before commit changes nothing. A failure during file replacement must retain enough old/staged state for deterministic compensation or explicit operator recovery; never delete the only valid database/config first.

Recovery must not extract arbitrary archive entries even if the archive format supports them.

## Part E — manual CLI

`backup --output-dir` and `recover [source]` must preserve:

- interactive source selection/cancellation where applicable;
- list/read backup metadata needed for presentation;
- exit codes and success/error output;
- no raw env/API secrets in normal output;
- path-with-spaces behavior;
- nonexistent/corrupt/incompatible sources.

## Part F — `automatic_backup` task

Implement the business callback and register it in the existing R006/R008 task inventory.

Requirements:

- exact enable/schedule semantics from O001/Python config;
- callback calls the same backup service as CLI;
- process-owned M8 supervisor scheduling only;
- no private sleep/timer loop;
- singleton/non-overlap enforced by supervisor;
- reload task-spec changes are staged transactionally through R007;
- failure is isolated and reported in bounded task diagnostics without shutting down inference;
- retention cleanup after successful backup only as current contract defines;
- shutdown waits for/cancels according to supervisor semantics without corrupting a partially-written archive.

After closure `automatic_backup` must no longer appear deferred in capability inventory.

## Fault-injection tests

Use temp roots and synthetic DB/config/env. Cover at least:

- fresh/current/checksum migration cases;
- locked/busy/migration SQL failure;
- vacuum success/locked/cancellation;
- backup with WAL activity while synthetic requests write;
- backup output-dir missing/permission/full-disk simulation;
- failure after DB snapshot but before archive rename;
- archive corrupt/truncated/wrong version;
- traversal/absolute path/symlink/special file/duplicate member/oversized member;
- recovery config invalid and DB incompatible;
- stop failure and restart failure;
- failure at every final replacement boundary with old state retained or restored;
- repeated backup names/collision handling;
- automatic schedule enabled/disabled/reload interval/task failure/non-overlap;
- task cancellation/shutdown during archive staging;
- backup archives never contain temp absolute paths or unintended files.

Use narrow test-only fault hooks; no production fault-injection framework.

## Security review

Explicitly review:

- archive traversal and symlink behavior;
- secret-bearing `.env`/config member necessity and permissions;
- temp/staging directory modes;
- replacement ownership;
- TOCTOU around reviewed source files;
- decompression/resource limits;
- backup directory exposure;
- error/log redaction.

## Non-goals

- cloud/remote backups;
- encryption/key management not present in Python contract;
- incremental backup engine;
- DB downgrade;
- new backup tables/schema;
- a second scheduler.

## Verification

Run fmt/Clippy, focused O006 tests, F004 migration tests, R006-R009 task/lifecycle regressions, aggregate Rust tests, targeted Python backup/recover/migration tests, migration oracle, and static checks. Include an isolated subprocess crash/fault test around replacement if needed to prove recoverability.

## Closure evidence

Write `migration-rs/closure/operations/006-status.md` with archive contract table, migration/vacuum parity, restore fault matrix, automatic-task registration evidence, dependency/security review, and unresolved findings.

## Acceptance criteria

O006 closes only when manual database/backup/recovery commands are real and fault-safe, existing schema/checksum compatibility remains intact, malicious/corrupt archives fail closed, automatic backup is a real singleton M8 task, and no ordinary backup/restore failure can require a database reset to recover.

Accepted O006 promotes only O007.