# On-Prem User Access M013 — Transcode-eligibility admission bridge corrective

Status: implemented

Repository baseline: `ea56ddcb` on `plans/on-prem-small-team-user-access` (PR #7), schema 62, M008/M009 closures accepted.

Source roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md` (corrective dependency graph and promotion)

Long-term references:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR:

- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted)

Primary class: capability

## 1. Objective

Restore cross-surface upstream eligibility broken by M008's admission edit.
This is a **new corrective** to the closed M008 record
(`plans/closure/on-prem-user-access/008-status.md`), not a rewrite of it.

## 2. Readiness and dependencies

No hard predecessor beyond the accepted M008/M009 closures, which supply the
admission bridge and the budget contract this pass preserves. Ready: the
defect, the missing line, and the failing hosted gate are all identified.
No physical hardware prerequisite.

## 3. Evidence and why prior qualification missed it

Hosted CI `check` run `38517675228` (PR #7) failed at
`uv run pytest tests/tooling/` with
`test_q007_loopback_fake_emits_secret_free_evidence` reporting
`messages-to-responses-finite returned HTTP 503` (`No eligible account was
available`). All Rust gates in the same run passed. Local bisection proved
the cause: M008 implementation `0149a6c8` fails the case while its parent
`b0e8715f` passes all seven.

Root cause: when M008 extended `routing_request_facts_from_parts`
(`rust/src/request/admission.rs`) with the budget-fact copies, the edit
dropped the adjacent line
`facts.transcode_protocols = inputs.transcode_protocols.clone();`.
Admission-built routing facts therefore carried an empty transcode list, so
cross-surface candidates (Messages → Responses) lost eligibility while
native surfaces kept passing — exactly the observed 3-pass/1-fail shape.

Why verification missed it:

1. The dropped line sat adjacent to the added lines in one edit hunk; review
   read the additions, not the surviving context.
2. No Rust test asserted the admission→facts bridge carries routing inputs:
   existing tests build `RoutingRequestFacts` directly (bypassing admission)
   or assert budget facts only.
3. The Python tooling suite was not run locally for M008/M009. The source
   plans' §11 only requires tooling commands "when scripts are changed", and
   no script changed — but the tooling suite drives the Rust binary, so a
   Rust-only behavior break hid until hosted CI. The before-push matrix in
   `AGENTS.md` lists `uv run pytest tests/tooling/` unconditionally; that
   authority should have governed.

A removed-line audit of the full M008+M009 `rust/src/` diff
(`git diff b0e8715f..ea56ddcb`) confirms this was the only accidental
deletion; every other removed line maps to an intentional restructure.

## 4. Non-regressing invariants

- Same invariants as M008 §4 (single-instance on-prem, TOML topology keys,
  generation leases, single SQLite gate/worker WAL/NORMAL, canonical wire,
  published protocol behavior, redaction, fail-closed limits).
- No budget, schema, config, wire-format, or operator-surface change in this
  pass: the fix restores one deleted copy, nothing more.

## 5. Scope and required production changes

### In scope

- Restore `facts.transcode_protocols = inputs.transcode_protocols.clone();`
  in `routing_request_facts_from_parts`.
- One regression test proving admission-built facts preserve routing inputs
  (`transcode_protocols`, `requested_protocol`, `catalog_stale_after_s`,
  `capability_policy`, `now`, `request_surface`) alongside budget facts.

### Explicitly out of scope

- No budget semantics, migration, config, dashboard, CLI, API, or doc change
  beyond this plan and its closure record. No new restart/reload keys. No
  unrelated closed scope is reopened.

## 6. Ordered work packages

1. Restore the deleted copy (one line, `rust/src/request/admission.rs`).
2. Add the bridge regression test
   (`admission_preserves_routing_inputs_for_transcode_eligibility` in
   `rust/tests/on_prem_budgets.rs`); verify it fails with the line removed
   and passes with it restored.
3. Run the full default/no-default gates **plus** the Python tooling suite
   locally (`uv run pytest tests/tooling/ -q`) and the Q007 loopback
   qualification to pass.

## 7. Failure, cancellation, restart and contention

No new runtime path: the restored copy is a synchronous field assignment
inside admission fact construction. No transaction, retry, selector, or
contention semantic changes.

## 8. Compatibility and migration

No schema, config, API, CLI, or wire change. Zero migration impact; existing
databases, rules, holds, and clients are unaffected.

## 9. Focused and adversarial tests

- `cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1`
- `uv run pytest tests/tooling/test_qualification_live_provider.py -q` (the
  hosted-failing target, run locally against the debug binary)
- Then the full default/no-default gates per §11.

## 10. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
uv sync --frozen
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

`cargo deny`, the feature tree, and the locked release build run only on
Cargo dependency/feature changes (none here).

## 11. Documentation updates

This plan and its closure record. No user/architecture doc change: behavior
returns to the documented M008 contract.

## 12. Acceptance criteria

`messages-to-responses-finite` returns 200 in the Q007 loopback
qualification; the full local default/no-default Rust suites and the full
Python tooling suite pass; the bridge regression test fails without the fix
and passes with it.

## 13. Stop conditions

Stop if the restored line does not repair the Q007 case (the defect would be
elsewhere and this plan's premise wrong), if any gate regresses, or if scope
expands beyond the one-line restoration plus its guard.

## 14. Closure evidence required

New `plans/closure/on-prem-user-access/013-status.md` must include: the
bisected commits (base pass, M008 fail), the exact dropped line, the
removed-line audit method and result, the guard's fail-without/pass-with
proof, exact commands and local outcomes for every §10 gate including the
tooling suite, and the hosted CI outcome on push. Do **not**
retrospectively edit `008-status.md`.

## 15. Handoff

One-line restoration plus guard; `server/*` stays thin and coordinator
remains the single ingress execution owner. M010/M011/M012 are unaffected.
