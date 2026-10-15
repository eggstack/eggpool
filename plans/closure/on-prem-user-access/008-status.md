# On-Prem User Access Milestone 008 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/008-hard-token-cost-admission-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m008--enforceable-output-token-and-price-bound-admission-corrective`

Repository baseline reviewed: `ca77c547` (M008 proof-carriage coverage)

Implementation commits or pull requests:

- `0149a6c8` — Implement on-prem user access M008 hard token/cost admission corrective
- `ca77c547` — Cover M008 proof carriage from admission through claim snapshot
- `49a0f845` — Activate on-prem user access M008 (planning status only)

## 1. Executive finding

Enforceable token/cost admission is implemented as a bounded corrective to the
conditionally closed M005/M006/M007 records, which remain immutable. Every
budget rule now carries an explicit `hard` or `estimated` enforcement mode
(pre-existing rows are `estimated`; promotion is an explicit admin
operation). Hard token rules admit only requests with a provable worst-case
bound and hard USD rules additionally require an operator-configured provider
tariff; anything unprovable is denied inside the publication transaction with
`BudgetNotEnforceable` before claim conversion, so zero upstream submissions
occur. Estimated rules behave exactly as M005 left them. The capability
boundary is complete on local evidence; no medium-or-higher finding remains.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Explicit hard vs estimated enforcement semantics (§5A) | `rust/src/user_budget.rs::BudgetEnforcement`; migration 0061; `UserBudgetRule.enforcement` in `/api/user-budget`, dashboard, CLI | pass | Request/concurrency caps stay strict in both modes; old rows map to estimated. |
| Typed per-surface output-capability contract (§5B) | `request/admission.rs::budget_admission_facts` + `BudgetAdmissionFacts`; chat `max_tokens`/`max_completion_tokens`, Responses/compact `max_output_tokens`, Messages `max_tokens`; `admission_extracts_per_surface_output_bounds` | pass | Caller fields are read, never mutated; absent/invalid caps degrade to unbounded and fail closed under hard rules. |
| Proof-or-reject before provider dispatch (§5B) | `check_initial`/`check_retry` return `NotEnforceable`; `PublicationError::BudgetNotEnforceable` → `EndpointError::BudgetNotEnforceable` (429, stable message); zero-row assertions in `hard_token_rule_denies_unbounded_request_without_upstream_io`, `hard_token_rule_denies_missing_proof_without_upstream_io` | pass | Denial happens inside the publication transaction before claim conversion. |
| Pricing trust boundary (§5C) | `ProviderConfig.budget.max_microdollars_per_million_tokens` + validation; `hard_usd_rule_denies_unknown_price_without_upstream_io`, `hard_usd_rule_admits_with_operator_tariff_hold`, `zero_cost_tariff_admits_hard_usd_without_hold`, `provider_tariff_overflow_is_rejected_by_config_validation` | pass | `pricing.fallback=generic_estimate` is never a hard price; `None` = unknown (deny), `Some(0)` = verified free; overflow fails closed, never saturates. |
| Retry/selector cost policy (§5A) | Initial hard hold covers two attempt-equivalents (concrete + unattributed selector overhead); retries extend by one equivalent after re-check (`hold_for_admission`/`hold_for_retry`); `hard_retry_denies_extension_beyond_remaining_budget`, `hard_retry_extends_within_remaining_budget` | pass | Retry path re-derives hard flags from current rules, so post-admission promotion still gates retries. |
| Transaction integration, no check-then-dispatch gap (§5D) | Proof assembled by coordinators from immutable claim-snapshot facts + generation tariff snapshot (`budget_proof_for_claim` in finite + streaming paths); atomic check+hold in `publish_transaction`; `hard_proof_flows_from_admission_through_claim_snapshot` | pass | In-flight holds keep their generation's tariff; tariff edits affect new requests only, matching rule-edit semantics. |
| UI/API/CLI truthful projection (§5D) | Dashboard budget form mode select + member mode column; `/api/user-budget` rule `enforcement`; CLI `--enforcement hard\|estimated` (default estimated); `on_prem_admin_ui` hard-form round trip + invalid-mode 400; `cli_contract` default/boundary cases | pass | Old dashboard forms without the field post as estimated. |
| Compatibility migration (§9) | Additive migration 0061 (defaults `estimated`, NULL price source); `rules_validate_admin_scope_and_usage_reads_are_bounded` asserts estimated default; historical fixture upgrades through 61 in `database_compatibility` | pass | No silent upgrade; downgrade still needs a pre-upgrade backup. |
| Settlement honesty, no hidden overruns (§5D/§8) | `hard_settlement_records_provider_overrun_without_hiding` (400 observed vs 300 held charged visibly, unknown counters zero) | pass | Unknown usage still consumes the conservative hold. |
| Docs (§12) | `docs/model-limits.md`, `docs/api-reference.md`, `docs/cli-reference.md`, `docs/deployment.md`, `architecture/deep-dive-request-lifecycle.md`, `architecture/deep-dive-database.md`, `rust/config.example.toml` tariff example | pass | `scripts/validate_release_docs.py` passes. |
| Non-goals respected (§6) | No billing reconciliation, no cost-based routing, no second SQLite writer, no provider-response mutation, no wire-format change | pass | Tariff is provider-wide; per-model refinement explicitly deferred. |

## 3. Production implementation evidence

Landed ownership/storage/protocol/runtime/operator changes:

- Storage: migration `0061_user_budget_enforcement.sql` (rule/audit `enforcement`, reservation `enforcement`/`bound_tokens`/`bound_microdollars`/`price_source`, enforcement index); `checksums.json` + `runtime-manifest.json` (count 61, refreshed asset hashes); inventory guards updated (`migrations.rs`, `build_manifest.rs`, `database_compatibility.rs`).
- Admission: `BudgetAdmissionFacts` computed in `admit_parsed_request`/`admit_compact_parsed_request` from the parsed body before it moves; carried via `RoutingRequestFacts` → `SelectionSnapshot` → `SelectionClaim` getters.
- Enforcement: `user_budget.rs` hard/estimated rule evaluation with worst-case holds (`single = input_worst + output`, initial ×2, retry ×1, tariff conversion with checked i128 arithmetic); `BudgetCheck::NotEnforceable`; reservation rows record mode/bounds/provenance.
- Coordinators: finite + streaming attach `budget_proof_for_claim` (snapshot facts + selected-provider tariff) to every `PublicationInput`; publication enforces atomically on initial and retry paths.
- Endpoint: `EndpointError::BudgetNotEnforceable` → 429 with stable message, mapped from both coordinators.
- Operator: `ProviderConfig.budget` tariff with `i64` range validation (live-reloadable under the existing `providers.` disposition; per-request snapshot semantics documented); CLI `--enforcement`; dashboard form + mode display; API rule mode.
- Tests: `on_prem_budgets` 4 → 19 tests (15 new hard/compat/carriage cases); `on_prem_admin_ui` hard-form round trip + mode display; `cli_contract` enforcement default/boundary.

Planned but absent: nothing in the source plan's scope is unimplemented. Per-model
tariffs and an HTTP-level hard-denial test remain explicit non-goals/follow-ups
(see §10).

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_extraction_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_kernel_boundary -- --test-threads=1
git diff --check
uv run python scripts/validate_release_docs.py
```

Focused targets from the source plan §10 were run explicitly before the full
suite: `on_prem_budgets`, `coordinator_publication`, `coordinator_finalization`,
`coordinator_c011`, `on_prem_model_policy`, `on_prem_admin_ui`,
`codex_compaction_compat`, `wire_runtime`, `database_compatibility`,
`cli_contract`, plus `on_prem_attribution` and `server_transport` for the
touched coordinator/endpoint paths. New coverage is concentrated in
`on_prem_budgets`/`on_prem_admin_ui`/`cli_contract`; the remaining targets
verify no regression. No Cargo dependency/feature changed, so `cargo deny`,
the feature tree, and the locked release build were not re-run (nothing they
guard changed). No Python tooling files changed, so the ruff/pyright/pytest
tooling matrix was not re-run.

### Results

Local (this machine, `current_thread`/serial as required):

- Full default workspace/all-targets serial suite: 978 passed, 0 failed
  (77 suites; includes 19 `on_prem_budgets`, 11 `coordinator_publication`,
  10 `coordinator_finalization`, 18 `coordinator_c011`, 9
  `database_compatibility`, 13 `codex_compaction_compat`).
- Full no-default workspace/all-targets serial suite: 979 passed, 0 failed.
- Strict Clippy default + no-default, `fmt --check`, `git diff --check`:
  pass. Wire seam guards (`wire_extraction_contract` 15,
  `wire_kernel_boundary` 4): pass. Release-docs validator: pass.
- Hosted CI: not run from here; PR #7 checks execute on push. Physical
  hardware: not required by this plan; SBC/small-team measurement belongs to
  M012. No test was claimed without running it.

## 5. Invariant review

- One local/SBC instance, optional <50 named users; no SaaS/tenancy/billing/IAM/WAN additions. Upstream keys stay in TOML/environment; no user-table or admin-UI key surface added.
- Generation leases, coordinator publication/finalization ownership, single SQLite gate/worker with WAL/NORMAL, canonical wire, transparent streaming, bounded hot-path memory, provider fairness: unchanged (no topology, runtime, or streaming change; proof facts are bounded scalars).
- Budget refusal never marks provider unhealthiness: denials roll back the routing claim (`rollback_after_error`) without health effects.
- Published Chat/Responses/compact/Messages behavior, auth headers, client profiles, dashboard themes, legacy upgrade: unchanged; admission never mutates caller output fields.
- Identity/cost metadata redaction preserved; no secrets/prompts/bodies in logs, HTML, or artifacts; DB failures and unprovable bounds fail closed.
- Routing stays deterministic and load-based; selector decisions still precede provider/account routing with no account pinning, and selector spend is covered by the documented upfront equivalent rather than a separate hold.

## 6. Failure and recovery review

- Unbounded output, missing proof, unknown tariff, non-positive or overflowing bounds all deny as `NotEnforceable` inside the publication transaction; zero-row assertions prove no partial request/reservation survives.
- Retry re-checks current rules and extends transactionally; over-budget retries deny as `Exceeded` without touching the surviving hold.
- Rule/tariff edits are atomic and new-request-only; in-flight requests keep their permit and held amounts.
- Cancellation/duplicate-finalization paths are unchanged from M005/M007; settlement stays idempotent, records observed usage visibly, and preserves holds until terminal settlement. A dedicated budget crash/restore fault campaign is M010's scope, not this plan's.

## 7. Migration and compatibility review

Migration 0061 is additive with conservative defaults (`estimated`, zero bounds, NULL price source); fresh databases migrate to 61 and the pre-61 historical fixture upgrades through 61 with repositories round-tripping. Stored user rules, provider accounts, historic consumption, and API schemas remain readable; old dashboard forms and CLI invocations without the new mode field behave as estimated. Wire bytes are untouched. Downgrade still requires a matching pre-upgrade backup.

## 8. Security review

Rule promotion requires an active admin (CLI actor, CSRF + admin session, audit row with mode). Member reads stay owner-scoped; hard-mode denial messages are stable, secret-free, and machine-readable without leaking limits, usage, tariffs, or identity. Tariff validation rejects out-of-range values; budget arithmetic uses checked i128 and fails closed on overflow. No password/token/bearer material enters the new columns, logs, or snapshots. The tariff is security-relevant operator configuration: it is covered by the existing `providers.` reload disposition with per-request snapshot semantics, so a live tariff change cannot retroactively weaken in-flight holds.

## 9. Documentation and operations

Updated `architecture/deep-dive-request-lifecycle.md` (admission/enforcement semantics), `architecture/deep-dive-database.md` (migration 61, hold facts, API mode), `docs/model-limits.md` (hard/estimated contract, tariff, denial code), `docs/api-reference.md` (rule mode, 429 semantics), `docs/cli-reference.md` (`--enforcement`), `docs/deployment.md` (tariff configuration), and `rust/config.example.toml` (commented tariff). Formal accounting assumptions (byte-length input bound, per-surface output fields, ×2 initial / ×1 retry equivalents, tariff-only hard USD, overflow fail-closed, per-model tariff non-goal) are stated identically in code docs, user docs, and §2 above.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| Low | Hard-mode holds over-reserve by design (×2 upfront); unknown-usage settlement charges the conservative hold. | Usage accounting reads high until observed usage settles; visible via unknown counters and observed columns. | None — conservative by design; refine only under a future bounded plan with measured selector/usage evidence. |
| Low | Tariff is provider-wide and must cover the provider's most expensive model. | Operators with mixed free/paid models on one provider must set the paid ceiling or split providers. | Documented; per-model tariffs need a separately approved bounded plan. |
| Low | No HTTP-level hard-denial test through `execute_endpoint`; denial is proven at the publication gate plus endpoint mapping. | A future endpoint refactor could only break this via the covered gate/mapping, both tested. | Optional follow-up test under a later milestone; not a correctness gap. |

No critical, high, or medium findings. No physical-capacity or hard-spend claim is made beyond the tested worst-case reservation contract.

## 11. Roadmap disposition

M008 is closed. Its hard predecessor requirement on M009 is satisfied: the
enforcement-mode and conservative reservation contract M009 must consume is
landed and verified, so M009 is promoted to ready in the same commit. M010
remains blocked on M008+M009 closures (still needs M009). M011 stays ready and
independent. M012 remains blocked on M008–M011 closures plus operational
hardware. No predecessor closure record was altered.

## 12. Registry updates

Applied in the same commit: M008 plan `active` → `implemented` with this
closure recorded as `closed`; M009 plan `blocked` → `ready`; roadmap M008
`active` → `closed`, M009 `blocked` → `ready`; M008 row moved from active to
recently-closed; blocked-work table retains M010/M012 with unchanged reasons.
Historical M005/M006/M007 records remain immutable.
