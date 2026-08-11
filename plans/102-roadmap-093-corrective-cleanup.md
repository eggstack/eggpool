# Plan 102 — Roadmap 093 Corrective Cleanup

Date: 2026-08-11
Status: planned
Parent roadmap: `plans/093-sbc-runtime-and-maintenance-simplification-roadmap.md`
Depends on:

- `plans/098-analytics-index-write-amplification-audit.md`
- `plans/099-runtime-archaeology-pruning.md`
- `plans/101-sbc-runtime-characterization-closure.md`

Planning baseline: `1afac227cef737dd4766ee9dfc5f76a5f6250c68`

## Purpose

Perform one narrow corrective cleanup after Roadmap 093 closure.

The Roadmap 093 implementation is functionally complete. This plan exists only to correct two closure-hygiene defects found during post-implementation review:

1. historical migration `0026_attempt_observability.sql` was comment-edited during Plan 097/098 follow-up and its manifest checksum was changed, even though Plan 098 explicitly required historical migrations to remain immutable;
2. `src/eggpool/db/connection.py` retains an orphaned class-level comment describing the production test-injection seam removed by Plan 099.

This is not a new optimization, hardening, database, testing, or documentation initiative. Restore historical migration immutability, remove the stale comment, verify the repository, document completion, and stop.

## Governing constraints

1. Do not change runtime behavior.
2. Do not change schema behavior.
3. Do not remove or alter migration `0053_remove_attempt_status_analytics_index.sql`; it is the correct forward migration for Plan 098's index removal.
4. Do not add a new migration.
5. Do not rewrite, squash, renumber, or otherwise normalize the migration history.
6. Do not change `MigrationRunner` or add runtime migration-checksum enforcement.
7. Do not reopen the analytics-index decisions from Plan 098.
8. Do not restore production database test-injection hooks removed by Plan 099.
9. Do not modify transaction ownership, rollback, commit ambiguity, startup reconciliation, or fail-closed behavior.
10. Do not modify request persistence, context estimation, backup behavior, routing, rehash, finalization, connection pools, or SBC defaults.
11. Do not add tests solely to test comments or plan bookkeeping.
12. Do not expand CI, add a full-suite gate, add coverage/benchmark/soak/hardware jobs, or introduce a new verification framework.
13. One implementation commit is preferred. A second documentation-only closure commit is acceptable only if required by the repository's existing planning workflow.

## Confirmed defects

### A. Historical migration 0026 drift

At the Roadmap 093 planning/implementation baseline, `src/eggpool/db/schema/0026_attempt_observability.sql` contained the historical comment:

```sql
-- First-attempt timestamp on requests.  Set once when the first
-- attempt row is inserted; subsequent updates leave it alone.  Used
-- by the dashboard to compute coordinator overhead (time from
-- request open to first attempt dispatch).
```

At current `HEAD`, only that historical comment was rewritten to describe the new post-Plan-097 runtime write path:

```sql
-- First-attempt timestamp on requests.  New rows set this once on the
-- request INSERT at the first durable-attempt boundary; subsequent
-- updates leave it alone.  Used by the dashboard to compute coordinator
-- overhead (time from request open to first attempt dispatch).
```

The migration's executable SQL remained unchanged, but the file blob changed and `src/eggpool/db/schema/checksums.json` changed the `0026_attempt_observability.sql` entry from the historical value:

```text
283dfa926ee5279187004dbad85db0e2f908319143497c2dc366954c67e8cc93
```

to the rewritten-file value:

```text
a6426ecffaee260f5a04c7caa50414d4daed5b7cb0ba90c1c42afea0220f2151
```

Plan 098 explicitly required historical migrations not to be edited. The correct cleanup is to restore migration 0026 byte-for-byte to the pre-Roadmap-093 historical file and restore its manifest checksum. Do not reinterpret or modernize historical migration comments.

### B. Orphaned Database fault-injection comment

Plan 099 correctly deleted production test-only fault-injection state and setter APIs from `Database`, but `src/eggpool/db/connection.py` still contains the now-orphaned comment immediately before `Database.__init__()`:

```python
    #: Test-only fault injection seam for the pre-commit boundary.
    #:
    #: When set on the class, every outermost ``transaction()`` exits
    #: by raising this exception *after* the inner work has yielded
    #: successfully but *before* the SQLite COMMIT is issued.  This
    #: simulates a process crash / power-loss between yield and commit
    #: so reload tests can verify that callers see the failure and
    #: run the rollback / compensation path.  Must default to ``None``
```

There is no longer any class attribute following this comment. Delete the entire obsolete comment block. Do not replace it with a compatibility note and do not restore a class attribute merely to make the comment true.

## Workstream A — Restore migration 0026 immutability

### Objective

Make `0026_attempt_observability.sql` exactly match its historical pre-Roadmap-093 contents again and restore the corresponding checksum manifest entry.

### Steps

1. Compare current migration 0026 against the known-good baseline before editing:

```bash
git diff adad407dc8fc7e53578c2a659f8183eca5fe752c..HEAD -- \
  src/eggpool/db/schema/0026_attempt_observability.sql \
  src/eggpool/db/schema/checksums.json
```

2. Confirm the only intended 0026 difference is the first-attempt comment text. If executable SQL differs from the baseline, stop and investigate before applying this plan; do not blindly overwrite a material schema change.
3. Restore `src/eggpool/db/schema/0026_attempt_observability.sql` exactly from the pre-Roadmap-093 baseline:

```bash
git show adad407dc8fc7e53578c2a659f8183eca5fe752c:src/eggpool/db/schema/0026_attempt_observability.sql \
  > /tmp/eggpool-0026-historical.sql
cmp /tmp/eggpool-0026-historical.sql src/eggpool/db/schema/0026_attempt_observability.sql || true
```

Then copy the historical file back into the working tree.
4. Restore only the `0026_attempt_observability.sql` checksum entry in `src/eggpool/db/schema/checksums.json` to:

```text
283dfa926ee5279187004dbad85db0e2f908319143497c2dc366954c67e8cc93
```

5. Confirm migration `0053_remove_attempt_status_analytics_index.sql` and its checksum remain unchanged.
6. Verify no other historical migration file was touched by this corrective pass.

### Required invariants

- Migration 0026 executable SQL is unchanged relative to both current HEAD and the historical baseline.
- Migration 0026's full file content matches the historical baseline byte-for-byte.
- The manifest entry for migration 0026 again matches that historical file.
- Migration 0053 remains the only forward schema change introduced by Plan 098.
- Existing databases already at schema version 53 require no new migration or repair action.

## Workstream B — Remove orphaned production comment

### Objective

Delete the stale `Database` class comment that documents a test-injection seam which no longer exists.

### Steps

1. Inspect the opening of `Database` in `src/eggpool/db/connection.py`.
2. Delete the complete `#: Test-only fault injection seam ...` block immediately before `__init__()`.
3. Search for stale references to removed production fault-injection APIs:

```bash
rg -n \
  'TEST_INJECT_BEFORE_COMMIT_CALL|set_test_inject_|_test_inject_(before_commit|commit_call|rollback_call|in_transaction_before_rollback)|Test-only fault injection seam' \
  src tests AGENTS.md README.md architecture .opencode docs plans
```

4. Classify any matches:
   - supported test-support helpers under `tests/` are allowed;
   - historical completion notes in plan files may remain if they accurately describe what was removed;
   - active production/documentation claims that the old `Database` seam still exists must be corrected only if found.
5. Do not broaden this into another dead-comment or architecture-document sweep.

### Required invariants

- `Database` exposes no restored test-only fault injection state or setters.
- `_commit_connection()` and private rollback boundaries remain patchable by tests exactly as after Plan 099.
- Transaction behavior is unchanged.

## Workstream C — Migration-integrity verification

### Objective

Prove that the historical migration restoration and manifest correction leave fresh and upgraded schema behavior unchanged.

### Discovery

Use repository search to identify the existing migration checksum/integrity and compatibility tests rather than inventing a new test file:

```bash
rg -n 'checksums\.json|checksum|migration.*compat|fresh.*upgrade|EXPECTED_SCHEMA_VERSION' tests src/eggpool/db
```

Run the existing focused tests that cover:

- migration manifest/checksum consistency, if present;
- fresh-database migration through version 53;
- upgrade compatibility through migration 0053;
- expected schema version / migration inventory;
- attempt-observability/index schema expectations affected by migrations 0026 and 0053.

If there is no dedicated checksum test, use the repository's existing script/helper if one exists. Do not create runtime checksum enforcement merely because a test is absent.

### Direct file assertions

Before committing, verify:

```bash
cmp \
  <(git show adad407dc8fc7e53578c2a659f8183eca5fe752c:src/eggpool/db/schema/0026_attempt_observability.sql) \
  src/eggpool/db/schema/0026_attempt_observability.sql
```

and independently calculate SHA-256 using an existing project helper or standard tool, for example:

```bash
python - <<'PY'
from hashlib import sha256
from pathlib import Path
p = Path('src/eggpool/db/schema/0026_attempt_observability.sql')
print(sha256(p.read_bytes()).hexdigest())
PY
```

The result must be exactly:

```text
283dfa926ee5279187004dbad85db0e2f908319143497c2dc366954c67e8cc93
```

Do not normalize line endings, whitespace, or comments after this check.

## Workstream D — Focused database regression verification

Run a narrow union covering the behavior adjacent to the cleanup, using existing tests only:

- transaction ownership / child-task rejection;
- commit failure and rollback failure ambiguity handling;
- migration compatibility/integrity;
- attempt stats/index behavior around migration 0053.

Prefer the exact test files referenced by Plans 095, 098, and 099 when they still exist. Do not run reload soak/performance suites as an acceptance requirement.

At minimum, confirm the behaviors remain true:

1. child tasks do not inherit transaction ownership;
2. non-owner rollback remains impossible because no public rollback API exists;
3. commit/rollback ambiguity still fails closed;
4. migration 0053 removes only `idx_request_attempts_status_started`;
5. retained attempt indexes still satisfy the Plan 098 focused query-plan assertions;
6. fresh schema reaches version 53 successfully.

## Workstream E — Ordinary lean repository gate

Run the existing repository gate without changing CI:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

A complete 8k+ retained-suite run is not required for this comment/checksum cleanup. Run additional tests only if a focused failure indicates a real dependency.

## Workstream F — Diff and scope audit

Before committing, inspect:

```bash
git diff --check
git diff --stat
git diff -- \
  src/eggpool/db/schema/0026_attempt_observability.sql \
  src/eggpool/db/schema/checksums.json \
  src/eggpool/db/connection.py
```

The implementation diff should be extremely small:

- historical comment restoration in migration 0026;
- one checksum entry restoration;
- deletion of one obsolete comment block in `connection.py`;
- this plan's status/closure notes if the executor records completion in the same commit or a follow-up documentation commit.

Reject unrelated formatting churn, refactors, generated-file changes, dependency changes, test architecture changes, or edits to runtime semantics.

## Acceptance criteria

- [ ] `src/eggpool/db/schema/0026_attempt_observability.sql` matches commit `adad407dc8fc7e53578c2a659f8183eca5fe752c` byte-for-byte.
- [ ] Migration 0026's executable SQL is unchanged by the corrective pass.
- [ ] `checksums.json` records migration 0026 as `283dfa926ee5279187004dbad85db0e2f908319143497c2dc366954c67e8cc93`.
- [ ] Migration `0053_remove_attempt_status_analytics_index.sql` and its checksum are unchanged.
- [ ] No other historical migration is edited.
- [ ] No new migration is added.
- [ ] Fresh schema migration still reaches version 53 successfully.
- [ ] Existing migration-integrity/checksum verification passes, or the closure record explicitly states the existing equivalent verification used when no dedicated checksum test exists.
- [ ] Existing migration compatibility tests covering upgrade/fresh-schema equivalence remain green.
- [ ] The orphaned `Database` test-injection comment is completely removed.
- [ ] No removed production fault-injection field or setter is reintroduced.
- [ ] Transaction ownership, commit ambiguity, rollback failure, fail-closed invalidation, and startup reconciliation behavior are unchanged.
- [ ] Plan 098's retained index/query-plan coverage remains green and `idx_request_attempts_status_started` remains absent at schema version 53.
- [ ] Ruff formatting, Ruff lint, Pyright, smoke tests, and both shipped `check-config` commands pass.
- [ ] CI workflow is unchanged.
- [ ] Runtime dependencies and lockfile are unchanged.
- [ ] The implementation contains no request-path, routing, backup, finalization, rehash, provider-pool, or SQLite pragma changes.
- [ ] The final diff is limited to this corrective cleanup and plan closure bookkeeping.
- [ ] Plan 102 is marked complete with implementation commit SHA and exact focused verification results.

## Rejection conditions

Reject the implementation if any of the following occurs:

- migration 0026 is rewritten to describe current runtime behavior rather than restored historically;
- executable SQL in migration 0026 changes;
- migration 0053 is removed, rewritten, renumbered, or folded into migration 0026;
- a new migration is introduced to undo a comment/checksum-only issue;
- historical migration immutability is "solved" by weakening or deleting migration-integrity checks;
- `MigrationRunner` gains checksum enforcement or new migration machinery as part of this cleanup;
- production test-injection hooks are restored;
- transaction/rollback/commit behavior changes;
- index policy is reopened or additional indexes are removed;
- a full-suite, coverage, benchmark, soak, or hardware CI gate is added;
- dependencies or the lockfile change;
- unrelated documentation/code cleanup is bundled into the implementation;
- the executor discovers material executable-SQL drift in migration 0026 and proceeds without first classifying it.

## Handoff sequence for GPT-5.6 Luna

1. Read Plans 093, 098, 099, 101, and this plan.
2. Inspect the current diff of migration 0026/checksums against `adad407d...` and confirm only the known historical-comment drift exists.
3. Restore migration 0026 byte-for-byte from `adad407d...`.
4. Restore only migration 0026's checksum entry to `283dfa...`.
5. Delete the orphaned `Database` test-injection comment block.
6. Run `rg` for removed fault-injection symbols and classify remaining matches without broad cleanup.
7. Run direct byte/hash verification for migration 0026.
8. Discover and run the existing focused migration-integrity/compatibility and database/index regression suites.
9. Run the ordinary Ruff/Pyright/smoke/config gate.
10. Audit `git diff --check`, `git diff --stat`, and the three intended source files for scope.
11. Record exact test commands/results and implementation SHA in this plan, mark it complete, and commit the narrow cleanup.
12. Push to the repository and stop. Do not create another roadmap unless a genuinely unrelated material defect is discovered.
