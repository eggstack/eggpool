# Routing Selection Milestone 001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/routing-selection/001-ordered-quota-scoring-and-candidate-allocation-cleanup.md`

Source subsystem roadmap:

- `plans/subsystems/routing-selection-roadmap.md#milestone-001--ordered-quota-scoring-and-candidate-allocation-cleanup`

Repository baseline reviewed: `4db8000d`

Implementation commits or pull requests:

- `4db8000d` — Implement routing-selection M001 ordered quota scoring cleanup

## 1. Executive finding

M001 is complete and **closed**. The deferred Plan 231 transient pipeline
is removed from the private router path: no per-selection String-keyed
active/projected/penalty maps, no String-keyed estimator result map, and no
candidate re-cloning through a by-name index. `QuotaFairScorer`'s public
methods, the selection lock, score formulas, ordering, fairness,
quota/health effects, and `RoutingDecisionTrace` are proven equivalent by a
seam-level numeric parity matrix plus twin-router determinism coverage and
the unchanged historical routing suites.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Ordered quota snapshot, one lock acquisition | `QuotaEstimator::snapshot_ordered` (borrowed names → `Vec<Option<QuotaAccountSnapshot>>`, single `self.lock()`, same `sync_mirrors` as `snapshot`) | pass | `pub(crate)`; missing accounts → `None` |
| Shared scoring core, zero-penalty private path | `QuotaFairScorer::score_ordered` (borrowed names, direct active lookup, one projected scalar, `0.0` penalty) through existing `score_one` | pass | `pub(crate)`; `score_accounts` kept as the compatibility wrapper on the same core |
| Move candidates instead of String reindex | `eligible.into_iter().zip(scores)` with in-place `candidate.score` assignment | pass | Source inspection: no `BTreeMap<String, RoutingCandidate>`, no candidate clone-back |
| No per-selection temp maps in the router path | Source inspection: no `active`/`projected`/`penalties` map construction in `build_eligible_candidates` | pass | Map-result `snapshot` is now called only by public `score_accounts` |
| Final sort/fairness/lock/claim unchanged | Diff inventory: comparator, `fairness_order`, `selection_lock`, claim book untouched; historical suites green | pass | — |
| Public scorer APIs unchanged + equivalent | `score_accounts`/`rank_accounts`/`near_ties` signatures untouched; seam parity matrix proves numeric identity | pass | Existing `quota` tests exercise the public surface (7 green) |
| 1/4/16/128 parity matrix | In-crate seam tests + `routing_selection_efficiency` twin-router matrix | pass | Modes/scopes/pinning/quota-modes/pressure/malformed covered (see §4) |
| No dependency/config/API/schema/concurrency change | `git diff --name-only`: no `Cargo.toml`/`Cargo.lock`/config/migration; no new lock, map-kind, or await | pass | `cargo deny` not required |

## 3. Production implementation evidence

Landed ownership (all in `4db8000d`):

- `rust/src/quota/estimator.rs`: `snapshot_ordered` (+ unit test
  `ordered_snapshot_preserves_order_and_missing_entries`: order, `None`
  for missing, value equality with the map snapshot incl. synced
  reservation/pending mirrors).
- `rust/src/quota/scorer.rs`: `score_ordered` (+ unit test
  `ordered_path_matches_public_scorer_across_account_counts`:
  1/4/16/128 accounts × projected {0, 500} × prefer_native {true, false}
  with active pressure, pending claims, reservations, and a ghost account;
  every `RoutingScore` field compared NaN-tolerantly, ghost ineligible on
  both paths).
- `rust/src/routing/eligibility.rs`: borrowed names → one ordered snapshot
  → index-aligned scores → moved candidates; validation (`malformed_score`)
  and the exact final comparator preserved.
- `rust/tests/routing_selection_efficiency.rs`: twin-router determinism
  across Off/RoundRobin/Random × all scopes (full at 4/16 accounts, Off
  everywhere) × hard-cap/score-only × pinned/unpinned × native preference
  × pending/reservation/active-warm-up pressure; exact eligible sets,
  exact `disabled`/`no_model` codes, `malformed_score` via the claim-less
  trace, Off-mode top-candidate selection, comparator conformance.

Structural before/after pipeline:

- Before: eligible `Vec` → clone names → build `active`/`projected` maps →
  empty `penalties` map → `score_accounts` → map-result `snapshot` →
  lookup + snapshot clone per name → `by_name` map → lookup + candidate
  clone per score → validate + sort.
- After: eligible `Vec` → borrow names → one ordered snapshot →
  `score_ordered` (shared core) → zip-move candidates + scores → unchanged
  validate + sort.
- The optional adjacent `fairness_order` move-only clone was deliberately
  left in place (partition/reorder logic, not a trivial move; explicitly
  optional per the plan). Recorded as a low residual below.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain_d008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_claims -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test quota -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_selection_efficiency -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib quota:: -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
git diff --check
```

### Results

All local (darwin development host; CI truthfully not run):

- Focused: `routing_domain` 12 passed; `routing_domain_d008` 4 passed;
  `routing_claims` 11 passed; `quota` 7 passed;
  `coordinator_publication` 7 passed; `coordinator_boundaries` 5 passed;
  `routing_selection_efficiency` 2 passed; lib `quota::` seam tests
  2 passed.
- Full default workspace suite: 796 passed, 0 failed (serial).
- Full no-default workspace suite: 797 passed, 0 failed (serial);
  no-default check + Clippy clean.
- Strict Clippy (default): clean. Locked release build: ok.
  `git diff --check`: clean.
- No timing assertions used as correctness tests; parity is structural
  (temporary-collection removal) plus semantic (field-exact scores,
  order, exclusions, traces, claims).

## 5. Invariant review

- Selection lock stays one async mutex with no await after acquisition:
  untouched; contention/cancellation suites green.
- Eligibility checks in the same logical order: `candidate_for_account`
  untouched.
- Score formula and all `RoutingScore` fields exact: seam matrix proves
  field identity across 24 configurations.
- Malformed/ineligible behavior and reason codes exact: `malformed_score`
  preserved (missing-account test); `disabled`/`no_model` exact in the
  matrix; historical exclusion suites green.
- Final sort precedence (priority, score, native preference, name) exact:
  comparator untouched; conformance asserted pairwise in the matrix;
  historical ordering suites green.
- Fairness Off/RoundRobin/Random, scopes, rotor commit, probes, accepted
  trace exact: `fairness_order` untouched; matrix covers all modes/scopes
  deterministically (default deterministic randomness); historical
  fairness suites green.
- Claim creation/conversion/release semantics exact: `routing_claims`
  (11) and coordinator suites green.
- Trace/snapshot determinism and field equivalence: twin-snapshot
  equality across the matrix; coordinator persistence suite green.
- Public scorer compatibility: signatures unchanged; no `HashMap`
  substitution anywhere; no new dependency or unsafe code.

## 6. Failure and recovery review

The selection path remains synchronous after `selection_lock`
acquisition; no new cancellation point was added. Estimator snapshotting
acquires/releases its mutex wholly inside `snapshot_ordered` and returns
owned bounded inputs before fairness/claim mutation, so no lock inversion
is introduced (single acquisition, no nesting with claim/catalog/health
locks). `Mutex` poison behavior is unchanged (no new recovery semantics).
Restart/reload semantics are unchanged (all state still
generation/process owned).

## 7. Migration and compatibility review

No migration. No config, HTTP, CLI, provider, database, persisted schema,
or public Rust API change. Public `QuotaFairScorer` and routing types keep
their signatures and fields. The two new crate-private methods are the
only API surface delta and are invisible outside the crate. No external
compatibility version changed.

## 8. Security review

Account names were already routing metadata; no new raw content or
credentials are captured. No diagnostic shape changed. No auth, privilege,
or DoS-relevant bound changed (fewer allocations under the same lock;
lock hold time strictly reduced, never extended).

## 9. Documentation and operations

- `architecture/deep-dive-routing.md`: ordered private snapshot/scoring
  path note (ownership, equivalence, untouched contracts).
- `plans/subsystems/routing-selection-roadmap.md`: lifecycle/status only.
- Legacy Plans 230/231 untouched; the deferred-work decision they record
  is now completed by this milestone (referenced, not rewritten).

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Adjacent move-only clone in `fairness_order` (`candidates.iter().cloned()` partition) intentionally left | Minor transient clone outside the scoring hot path; explicitly optional per plan | None; a future micro-pass may address it once parity coverage exists (it now does) |
| low | No representative 64/512/4096-entry affinity workload measured | M002 (affinity LRU) cannot yet be promoted | None in this milestone; recorded in §11 |

No medium-or-higher finding. No corrective pass required.

## 11. Roadmap disposition

Routing-selection M001 is **closed**. Explicit disposition of roadmap M002
(semantic-affinity exact-LRU cost qualification): **still evidence-gated
and not promoted** — this milestone produced no 64/512/4096-entry
affinity workload measurements, so M002 stays `not started` behind its
stated hard dependency (M001 closure, now satisfied) plus its evidence
dependency (representative workload showing the exact `VecDeque` touch is
material, still outstanding). No other work is unblocked or blocked by
this closure.

## 12. Registry updates

`plans/registry.md` + `plans/subsystems/routing-selection-roadmap.md`
updated in the same commit: M001 `ready` → `closed` with this record;
roadmap table and M002 blocker note reflect the explicit non-promotion.
