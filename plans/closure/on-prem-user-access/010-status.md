# On-Prem User Access Milestone 010 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/010-budget-crash-recovery-and-schema54-restore.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md`

Repository baseline reviewed: `be6248fc` (M010 implementation)

Implementation commits or pull requests:

- `542a2f28` — Activate on-prem user access M010 (planning status only; records actual baseline `1218423b`/schema 62, superseding the plan text's stale `c18dcc20`/schema 60)
- `be6248fc` — Implement on-prem user access M010 budget crash recovery and schema-54 restore

## 1. Executive finding

The crash-state audit proved one real defect and three non-defects. The
defect: startup reconciliation terminalized `pending` requests but never
touched user/instance budget holds, and no expiry sweeper exists, so any
crash between publication commit and terminal settlement leaked a phantom
`active` hold that permanently occupied quota (hard caps could wedge; the
single-concurrency-slot case wedged the whole instance). The fix is
additive and narrow: `fix_bounded` releases active budget holds on the
requests it interrupts, in the same transaction, without folding usage.
Double-counting is provably impossible (idempotent attempt facts,
settle-once guards, no accounting writes in reconciliation), dispatched
attempts always carry their durable hold (check+hold commits strictly
before `submit_once`), and committed holds cannot vanish (single-writer
transactions, WAL durability, retention spares nonterminal rows). A frozen
byte-valid schema-54 image restores and migrates forward 55→62 with legacy
history intact and no fabricated identity; ordinary-host gate observations
show sub-millisecond budget overhead with no gate-tenure concern. The M003
updater-test condition is a stale historical failure: both flagged tests
pass in isolation and in the full serial lib run at this SHA.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Crash-state transition audit (§A) | §3 matrix below, built from branch code with file:line references | pass | One real defect (phantom holds); dispatch ordering, double-count, and hold-loss proved safe. |
| Additive budget recovery, no core behavior change (§A) | `rust/src/coordinator/reconciliation.rs::fix_bounded` gains two conditional bounded `active→released` updates in the existing transaction; `classify_bounded` gains two counts; report/aggregate gain four fields; no publication/finalization/settlement line changed | pass | Reconciliation still inserts nothing and mutates no usage bucket or attempt fact. |
| Fault injection before/after commit, acceptance, mid-stream, finalizer, duplicate terminal, cancel, backup-during-hold (§B) | 7 new `on_prem_budgets` tests + 2 new `on_prem_global_budgets` tests + backup round-trip test; distinct durable shapes covered (pending+user hold, pending+user+instance holds, compensated+retried+extended hold, settled, cancelled, duplicate terminal, backup image) | pass | Assertions converge on rows/reconciler reports, no sleeps. Mid-stream and post-acceptance crashes share the pending+active-hold durable shape, covered by construction. |
| Independent schema-54 restore + forward upgrade (§C) | Frozen `tests/fixtures/database/schema54-pre-upgrade.sql` (SHA-256 `a76d43047986282d271ce99527f5827c743f50860625c9ffd44963a306fd42af`); 2 new `database_compatibility` tests | pass | Ledger 1..54, checksums, integrity, upgrade 55..62, bootstrap/login/tokens/profiles/rules, restart, backup/restore round trip. |
| Storage-pressure observations 1/10/49 (§D) | `paired_gate_observations_across_identities_and_rules` + `artifacts/qualification/m010-gate-observations.json` | pass | Sub-ms overhead, worst outlier 2.8ms, WAL truncates cleanly; not SBC capacity evidence (M012 owns that). |
| M003 updater condition disposition (§E) | Both flagged tests pass isolated + full serial lib (164/164) at this SHA | pass | Stale historical failure; no separate corrective opened; M003 history untouched. |
| Docs (§12) | `architecture/deep-dive-request-lifecycle.md`, `architecture/deep-dive-database.md`, `docs/backup-restore.md` | pass | `docs/deployment.md`/`docs/cli-reference.md` need no change (no new commands or surfaces); recorded, not churned. |

## 3. Crash-state transition matrix (from branch code)

| Window | User hold | Instance hold | Usage | Converges to |
|---|---|---|---|---|
| Crash before publication commit | rolled back (never existed) | rolled back | none | clean retry as new request |
| Crash after commit, before dispatch | durable `active` | durable `active` | none | **reconciler interrupts request + releases holds, no fold (new fix)** |
| Crash after upstream acceptance / mid-stream / before finalizer commit | durable `active` (possibly extended by retries) | durable `active` | none (unknown stays unknown) | same as above |
| In-process post-commit compensation, then retry | stays `active`, extended by retry | stays `active`, extended | none | reused hold; later crash → same as above (tested) |
| In-process compensation, never retried | stays `active`, request `pending` | stays `active` | none | orphan until restart, then released by fix (bounded, documented) |
| Terminal settlement (incl. cancel) then crash | `settled` + buckets folded | `settled` + buckets folded | exactly once | reconciler no-ops; duplicate terminal conflicts (tested) |
| Retry on interrupted request | none resurrected | none resurrected | none | duplicate gate refuses non-pending requests (tested: `DuplicateConflict`) |
| Backup during active hold | captured verbatim | captured verbatim | as committed | restored image reconciles like a crash (tested) |
| Read-only DB | migration/startup fail closed (`ReadOnlyMigration`); tested in `database_compatibility` | — | — | no torn state |
| Busy/commit ambiguity | single-writer `BEGIN IMMEDIATE` gate serializes; SQLite commits are atomic (commit or rollback, never torn) | — | — | no ambiguous-commit window exists in this architecture |

Non-goals preserved: reconciliation performs no `INSERT`, folds no usage,
hydrates no ephemeral state, and schedules nothing.

## 4. Verification executed

### Commands run (local, this machine, serial as required)

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
uv run pytest tests/tooling/ -q --tb=short --deselect tests/tooling/test_installer.py::test_quick_installer_harness_passes
uv run python scripts/qualify_quick_installer.py  # direct, no 180s cap
git diff --check
```

### Results

Local:

- Full default workspace/all-targets serial suite: **1005 passed, 0 failed** (993 carried + 12 new: 7 budgets + 2 global + 2 compatibility + 1 gate-observation).
- Full no-default workspace/all-targets serial suite: **1006 passed, 0 failed**.
- Strict Clippy default + no-default, `fmt --check`, `git diff --check`: pass.
- Focused plan targets all green: `on_prem_budgets` 27, `on_prem_global_budgets` 16, `database_compatibility` 11, `coordinator_c010` 20, `coordinator_publication` 10, `coordinator_finalization` 11, `coordinator_c008` 34, `coordinator_c009` 13, `runtime_lifecycle_r009` 6, lib `operations::backup` 1, full lib 164 (incl. both M003-flagged updater tests, isolated and in-suite).
- Tooling: ruff format (59 files) + check pass, pyright 0 errors, release-docs validator pass, `pytest tests/tooling/`: **165 passed, 1 skipped**, plus the installer harness run directly: **78/78 cases pass**.
- Hosted CI: this push re-runs it; run recorded on push.

### Environment-gated note (not a product finding)

`test_quick_installer_harness_passes` under pytest exceeds its 180 s budget
on this host (load average 36–50 during the run; the harness is SIGKILLed on
`subprocess.run` timeout). The identical harness binary run directly exits 0
with all 78 cases `pass`. No scripts/packaging/installer file changed in
this milestone, so the timeout is host overload, not a regression. It is
recorded here as a low-severity environment observation, not a code finding.

## 5. Production implementation evidence

- `rust/src/coordinator/reconciliation.rs`: `fix_bounded` returns a 5-tuple;
  two new bounded updates release `active` user/instance holds whose request
  is `interrupted`, with `settled_at` left NULL (never settled); two new
  classification counts; `converged`/`truncated`/`fixed_total` account for
  all five fixes; module docs updated (durable cases + never-does list).
- `rust/src/runtime_lifecycle/recovery.rs`: aggregate carries
  `user_budget_holds_released`/`instance_budget_holds_released`; the
  `crash_recovery` event shape is unchanged.
- No migration, config, API, CLI, wire, settlement, or tariff change. No
  Cargo dependency/feature change (`cargo deny`/feature-tree/release-build
  gates not re-triggered).

## 6. Invariant review

Same invariants as M008/M009, plus: single SQLite gate/worker WAL/NORMAL
unchanged (observations confirm no second writer, no pool, no WAL retune);
routing deterministic and load-based; fail-closed limits preserved
(unknown usage still settles conservatively; retries on dead work refuse at
the duplicate gate); credentials/prompts/bodies stay out of
persistence/logs/diagnostics (new tests assert secret-free shapes where
they touch rows; the fixture SQL contains env-var names only, no secrets).

## 7. Failure and recovery review

Covered in §3. The one deliberate non-repair: in-process compensated
requests that are never retried stay `pending` with an active hold until the
next restart. This matches the pre-existing orphaned-pending lifecycle (only
startup reconciles) and is now bounded by the fix instead of permanent.

## 8. Migration and compatibility review

Forward-only 54→62 proven against a frozen image, not a synthetic
post-migration seed. Legacy rows byte-preserved; `principal_kind` defaults
to `legacy_shared_key` with `user_id NULL` by migration 57's own DDL (no
fabricated identity); access state `legacy_unmigrated` → `admin_initialized`
→ `managed` verified end to end. `--no-default-features` parity holds
(1006 tests). Checksums/manifest untouched (no migration files added).
Rollback remains protected-backup + matching old binary, demonstrated by
the pre-upgrade backup captured in-test.

## 9. Security review

No auth, secret, redaction, privilege, or audit logic changed. Identity
recovery on the upgraded image verified (bootstrap gating on zero users,
password login accept/reject, personal-token issue/verify/revoke,
admin password rotation with epoch invalidation, member lifecycle).
Fixture and artifacts are secret-free.

## 10. Documentation and operations

Updated: `architecture/deep-dive-request-lifecycle.md` (§5 budget crash
semantics), `architecture/deep-dive-database.md` (covered budget states),
`docs/backup-restore.md` (55–62 scope, in-flight hold capture, v54 restore
path, protected-backup-before-migrate). `docs/deployment.md` and
`docs/cli-reference.md` verified to need no change (no new commands,
surfaces, or transitions). Observation artifact force-added per the
235–237 precedent despite the `/artifacts/qualification/` ignore.

## 11. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| Low | Pytest installer-harness timeout under host overload (load 36–50); harness itself 78/78 green when run directly. | CI truth for that target comes from hosted runners; local rerun timing only. | None — environment observation, re-check on a quiet host if needed. |
| Low | Orphaned compensated-pending holds converge only at next restart. | Bounded reservation occupancy between compensation and restart. | None — matches the startup-reconciliation lifecycle; M012 SBC work may revisit if restart intervals matter. |

No critical, high, or medium findings.

## 12. Roadmap disposition

M010 is closed. Unblock audit (same commit): M010 has no downstream hard
dependency beyond what M008/M009 already unlocked — no registered plan
declares M010 as a hard dependency — so nothing is promoted. M011 stays
ready (independent, parallel). M012 stays blocked on M008–M011 closures
plus provisioned physical SBC and reference ordinary-Linux target (the M010
ordinary-host observations are explicitly not SBC evidence). Historical
M003/M005/M007 closures stay immutable; this record is the corrective
account. The Persistence M011 documentation reconciliation (ready, hard on
M009/M010 closures) is a separate subsystem and unaffected by this audit.
