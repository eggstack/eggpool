# On-Prem User Access Milestone 009 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/009-global-budget-intersection-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m009--instance-global-budget-and-scope-intersection-corrective`

Repository baseline reviewed: `04264a18` (M009 implementation)

Implementation commits or pull requests:

- `04264a18` — Implement on-prem user access M009 global budget intersection corrective
- `372f0e1d` — Activate on-prem user access M009 (planning status only)

## 1. Executive finding

The instance-global budget ceiling is implemented as a new corrective to the
conditionally closed M005 record, which remains immutable. Exactly one
instance-wide scope exists (all-model rules plus optional exact-model rules,
no tenant identifier, no second database): every admitted call — attributed or
legacy-unattributed — must satisfy all applicable instance and user rules in
the same publication SQLite transaction, or it is denied before claim
conversion with zero upstream submission. The M008 enforcement-mode and
conservative reservation contract is reused unchanged, including hard-mode
proof-or-reject and the operator-tariff trust boundary. Concurrent requests
from distinct users cannot both take the final global slot (proven for 2, 10,
and 49 identities), retries never double-count, and global counters never
depend on a user row. The capability boundary is complete on local evidence;
no medium-or-higher finding remains.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| One instance-owned budget namespace, no tenant hierarchy (§5A) | Migration 0062 (`instance_budget_rules` UNIQUE `(ifnull(model,''), window)`, no user column); single-instance scope documented in `instance_budget.rs` header | pass | Day/month UTC windows, exact-model or all-model scope, malformed/overflow rejection, admin-only mutation. |
| Intersection: all applicable instance/user rules must allow (§5A) | Publication checks user scope then instance scope in one transaction; `global_and_user_rules_intersect_without_bypass` (global-spent denies despite user headroom; second user still capped) | pass | Absent instance rules mean no additional cap and no instance rows. |
| M008 hard/estimated semantics per dimension (§5A) | Shared `hold_for_admission`/`hold_for_retry`, tariff-only hard USD, `NotEnforceable` propagation; `global_hard_token_and_usd_deny_unprovable_without_upstream_io` (unbounded + unpriced deny with zero rows; proven request holds 300 tokens / 900 micros with tariff provenance) | pass | Instance hard flags re-derive from current rules on every path. |
| Atomic combined reservation, no double last-slot (§5B) | Same-txn check+insert for both scopes; `global_request_last_slot_admits_exactly_one_across_users` (1 winner + 1 denial, 1 request + 1 hold); 10- and 49-identity races with exactly one winner each | pass | Single SQLite gate serializes the race; see gate observations below. |
| One logical request across failover/selector/retry (§5B) | Retry re-check consumes no new request slot (`new_request=false` in both evaluators); `global_retry_and_settlement_count_one_logical_request` (retry extends, terminal folds 57 tokens / 25 micros across two attempts into request_count 1 per bucket) | pass | Fixed during implementation: the first draft consumed a request per retry and the new test caught it. |
| Bounded aggregates, no full scans, one gate (§5B) | Indexed window/window_start/scope sums in `rules_exceeded`; per-model usage rows with cross-model SUM only for all-model rules; no second writer; `settle` folds shared attempt rows | pass | 10-identity race 5.4 ms, 49-identity race 15.8 ms on local host (observation only, not a capacity claim). |
| History independent of user rows (§5B) | No user FK on any instance table; `disabled_user_does_not_erase_global_history` (usage intact after disable, remaining headroom usable) | pass | `user_budget_usage` keeps its RESTRICT semantics; instance history never joins `users`. |
| Admin CRUD + observable state, member fencing (§5C) | CLI `users global-budget set/remove`; dashboard instance section with set/remove; `/api/user-budget` `instance_rules`/`instance_usage` for admins only; `on_prem_admin_ui` set/remove round trip + member 403 + invalid-mode rejection; `global_rules_validate_admin_scope_and_reject_malformed` | pass | Members keep their existing owner-scoped view (their effective user-side cap); global aggregates are admin-visible only, so members cannot infer others' consumption. |
| Rollover with active holds (§8) | Window-CASE hold projection shared with the user scope; `global_window_projection_ignores_crossed_utc_boundaries` (backdated day+month holds free today's/month's slots while concurrency still counts live holds; third request denied) | pass | True clock travel is not simulable in-process; the projection SQL is the reviewed shared pattern. |
| No-config single-admin parity (§9/§13) | `no_global_rules_preserves_single_admin_parity` (attributed + legacy requests take no hold, write no rows); rule-free focused targets green unchanged | pass | Measured overhead of the rule-free path is the two extra indexed rule lookups; see §10. |
| Legacy/identity-switch bypass closed (§13) | Instance checks run for unattributed requests too; `legacy_unattributed_requests_count_against_global_ceiling` + `legacy_unattributed_settlement_folds_instance_usage` | pass | Required an `record_attempt_usage` refactor so attempt facts exist without a user reservation. |
| Docs (§12) | `docs/model-limits.md` (new instance section), `docs/api-reference.md` (admin fields, member fencing), `docs/cli-reference.md` (global-budget rows), `docs/deployment.md` (ceiling paragraph), dashboard help text, `architecture/deep-dive-database.md` + `deep-dive-request-lifecycle.md` | pass | Provider quotas documented as unrelated quantities. |
| Non-goals respected (§6) | No multi-org/external-billing/sync, no per-provider credential quotas, no substitution after refusal, no second pool/writer | pass | Tariff stays provider-wide per M008 (documented non-goal). |

## 3. Production implementation evidence

Landed ownership/storage/protocol/runtime/operator changes:

- Storage: migration `0062_instance_budgets.sql` (rules, reservations without user FK, per-model usage buckets, audit, policy-revision triggers); `checksums.json` + `runtime-manifest.json` (count 62, refreshed hashes); inventory guards updated (`migrations.rs`, `build_manifest.rs`, `database_compatibility.rs` incl. historical upgrade through 62).
- Service: `rust/src/instance_budget.rs` owns `InstanceBudgetService` (admin-gated CRUD, bounded reads), the pub(crate) check/insert/extend/settlement contract reusing M008 hold math, and per-scope bounded evaluation. Shared M008 helpers (`hold_for_admission`, `hold_for_retry`, tariff/worst-case math, `exceeds`, `BudgetPriceSource::as_str`) are now `pub(crate)`.
- Coordinator: `publication.rs` evaluates the instance intersection on initial (all requests) and retry (hold-bearing requests only) paths in the same transaction, inserting/extending the shared hold atomically; `finalization.rs` folds instance settlement in the same terminal transaction. `user_budget::record_attempt_usage` was split out so attempt facts exist for unattributed requests.
- Operator: CLI `users global-budget set/remove`; dashboard `/users` instance section with set/remove forms; API instance fields for admins, unchanged member shape.
- Tests: new `rust/tests/on_prem_global_budgets.rs` (13 tests: cross-user last-slot, 10/49-identity races with timings, intersection, model sharing, retry counting, concurrency release, rollover projection, legacy coverage + settlement, disable-durability, hard proof-or-reject, validation/fencing, parity); `on_prem_admin_ui` instance round trip + member 403; `cli_contract` global-budget parse cases.

Planned but absent: nothing in the source plan's scope is unimplemented. The
dedicated crash/restore fault campaign is M010's scope; physical SBC/small-team
measurement is M012's.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_global_budgets global_last_slot_race -- --test-threads=1 --nocapture
git diff --check
uv run python scripts/validate_release_docs.py
```

Focused targets from the source plan §10 were run explicitly before the full
suite: `on_prem_budgets` (19, M008 regression over the touched
publication/finalization paths), `on_prem_global_budgets` (13 new),
`on_prem_admin_ui` (2), `database_compatibility` (9),
`coordinator_publication` (11), `coordinator_finalization` (10),
`server_transport` (14), `cli_contract` (7). No Cargo dependency/feature
changed, so `cargo deny`, the feature tree, and the locked release build were
not re-run. No Python tooling files changed, so the ruff/pyright/pytest
tooling matrix was not re-run.

### Results

Local (this machine, serial as required):

- Full default workspace/all-targets serial suite: 992 passed, 0 failed
  (79 suites; 978 carried from M008 + 13 global + 1 CLI contract).
- Full no-default workspace/all-targets serial suite: 993 passed, 0 failed.
- Strict Clippy default + no-default, `fmt --check`, `git diff --check`:
  pass. Release-docs validator: pass.
- DB gate observations (ordinary local host, in-memory SQLite, NOT SBC or
  capacity evidence): global last-slot race with 10 identities took
  ~5.4 ms, with 49 identities ~15.8 ms wall time including claim setup;
  exactly one winner each. Recorded here as the plan's controlled-concurrency
  observation; no SBC performance is asserted.
- Hosted CI: not run from here; PR #7 checks execute on push. Physical
  hardware: not required by this plan; belongs to M012. No test was claimed
  without running it.

## 5. Invariant review

- One local/SBC instance, optional <50 named users; no SaaS/tenancy/billing/IAM/WAN additions. No upstream keys in user/instance tables or admin UI.
- Generation leases, coordinator publication/finalization ownership, single SQLite gate/worker with WAL/NORMAL, canonical wire, transparent streaming, bounded hot-path memory, provider fairness: unchanged. Instance work adds indexed lookups inside the existing publication/finalization transactions only.
- Downstream budget refusal (user or global) never marks provider unhealthiness: denials roll back the routing claim without health effects.
- Published wire behavior, auth headers, client profiles, dashboard themes, legacy upgrade: unchanged.
- Identity/cost metadata redaction preserved; member fencing verified (403 on instance mutation, no instance fields in member API, no cross-user usage on member pages); DB failures and unprovable hard bounds fail closed.
- Routing stays deterministic and load-based; budgets never influence provider/account selection.

## 6. Failure and recovery review

- Cross-user last-slot races converge to exactly one winner at 2, 10, and 49 identities; losers get `BudgetExceeded` with no partial rows.
- Retry under a spent global slot denies without consuming a second request; retry within budget extends transactionally.
- Global hard rules deny unprovable requests as `NotEnforceable` with zero rows; instance hard flags re-derive from current rules on every path.
- Rule edits are atomic and new-request-only; in-flight holds keep their permit and amounts.
- Settlement is idempotent per scope (settled/released short-circuit in both evaluators); user and instance folds share one terminal transaction, so crash leftovers converge together. The dedicated budget crash/restore fault matrix is M010's scope.
- Malformed/overflowed rules are rejected at the service boundary; member mutation attempts fail closed at service and HTTP layers.

## 7. Migration and compatibility review

Migration 0062 is additive with conservative defaults; fresh databases migrate to 62 and the pre-62 historical fixture upgrades through 62 with repositories round-tripping. M005/M008 user rows, provider accounts, historic consumption, and API schemas remain readable; member API responses are byte-compatible with M008 (new fields are admin-only additions). Absent global rules, publication takes no instance hold and writes no instance rows — the one-admin workload is identical except the two bounded rule lookups. Downgrade still requires a matching pre-upgrade backup.

## 8. Security review

Instance rule mutation requires an active admin at every layer (CLI actor, CSRF + admin session, audit row with actor/version/mode); member and personal-token credentials get 403 and cannot select another scope. Instance reads are admin-gated in API and dashboard; usage aggregates never join user identities into member-visible output. No secret, prompt, body, or credential enters instance tables, logs, snapshots, or artifacts. Integer validation and checked worst-case arithmetic match the M008 contract; global denial messages carry no limits, usage, tariffs, or identity.

## 9. Documentation and operations

Updated `architecture/deep-dive-database.md` (migration 62, scope tables, bounded aggregates, admin-only visibility), `architecture/deep-dive-request-lifecycle.md` (same-transaction intersection, no-double-count, rule-free no-op), `docs/model-limits.md` (instance-ceilings section: scope, atomicity, no-bypass list, durability, fencing), `docs/api-reference.md`, `docs/cli-reference.md`, `docs/deployment.md`, and dashboard help text. Formal assumptions (one instance scope, per-scope independent holds, shared M008 hard contract, bounded indexed evaluation, no-bypass including legacy keys, admin-only global visibility) are stated identically in code docs, user docs, and §2 above.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| Low | A request under both a user-hard and a global-hard token cap holds the worst case twice (once per scope). | Conservative over-reservation when both scopes are hard; each hold settles independently against its own scope. | None — intersection semantics require independent per-scope holds; refine only under a future bounded plan with cross-scope evidence. |
| Low | Rule-free requests still execute two bounded indexed rule lookups per publication. | Sub-millisecond local overhead (see §4 timings); no row writes. | None — measured and bounded; SBC-side quantification belongs to M012. |
| Low | UTC-boundary behavior is proven via backdated holds, not clock travel. | The window-projection SQL is covered but a real midnight rollover was not observed in-process. | Optional: observe a live UTC reset during M012 physical qualification; not a correctness gap. |

No critical, high, or medium findings. No physical-capacity or hard-spend claim is made beyond the tested reservation contract.

## 11. Roadmap disposition

M009 is closed. Unblock audit (same commit): M009's hard M008 dependency was
already satisfied by the accepted M008 closure. M010's hard dependency (M008
and M009 accepted closures with a stable global user-budget state machine) is
now fully satisfied — the instance scope, intersection, and settlement it must
consume are landed — so M010 is promoted to ready. M011 stays ready and
independent (parallel browser work, unaffected by this closure). M012 remains
blocked: it additionally needs M011 closure plus provisioned physical hardware.
No predecessor closure record was altered.

## 12. Registry updates

Applied in the same commit: M009 plan `active` → `implemented` with this
closure recorded as `closed`; M010 plan `blocked` → `ready`; roadmap M009
`active` → `closed` with this closure linked and roadmap M010 `blocked` →
`ready`; M009 row moved from active to recently-closed; blocked-work table
retains M012 with its unchanged reason. Historical M005/M007/M008 records
remain immutable.
