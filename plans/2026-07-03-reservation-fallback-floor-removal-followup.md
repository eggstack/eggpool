# Reservation Fallback Floor Removal Follow-up Plan

Date: 2026-07-03
Repository: `eggstack/eggpool`
Status: handoff plan
Priority: critical accounting correctness

## Context

The recent cost hardening pass added useful primitives: bounded estimated-cost selection, raw pre-clamp cost validation, quota-estimator reservation bounds, EWMA pre-seed guards, repair detection, and dashboard warning metrics. The implementation is close, but `src/eggpool/request/finalizer.py` still contains the old estimated-cost floor after the new lower-plausible selection logic.

That floor re-raises canonical estimated cost to `reservation_microdollars` whenever the selected cost is lower than the reservation. This directly contradicts the new invariant that reservation is a preflight routing budget, not a bill. It also preserves the exact MiniMax bug found in `usage.sqlite3`.

## Known failing fixture

Lock this live MiniMax row into a finalizer regression test:

- provider id: `minimax`
- model id: `MiniMax-M3`
- input tokens: `353`
- output tokens: `1386`
- cache read/write tokens: `0`
- local cost: `21848` microdollars
- local exactness: `estimated`
- reservation: `5411079` microdollars
- expected canonical cost: `21848` microdollars
- expected canonical exactness: `estimated`

Current likely behavior is that the new helper picks `21848`, then the old floor raises canonical cost back to `5411079`. That must stop.

## Required invariant

Reservation estimates are advisory. They are persisted for routing, audit, and quota visibility, but they must not floor canonical request cost. When local cost exactness is `estimated`, canonical cost must be the lower plausible value chosen by `choose_bounded_estimated_cost()`. Nothing later in the finalizer may override that choice back to the reservation amount.

## Phase 1: Remove or narrow the estimated-cost floor

Target: `src/eggpool/request/finalizer.py`.

Preferred fix: remove the blanket estimated-cost floor entirely. The helper already evaluates plausibility and chooses the lower defensible value. A second unconditional reservation floor defeats that helper.

Acceptable alternative: make the floor provenance-aware and only allow reservation-derived choices to remain reservation-derived. The floor must not apply when the chosen provenance is `local_estimated` or `min_local_reservation_estimated`.

The preferred implementation is simpler and less error-prone: delete the floor and rely on `choose_bounded_estimated_cost()`.

## Phase 2: Update contradictory comments and docs

Remove all language that says canonical estimated cost is never lower than reservation. Replace it with the correct invariant: reservation is a routing budget, and canonical estimated cost is selected by `choose_bounded_estimated_cost()`.

Check at least:

- `src/eggpool/request/finalizer.py`
- `README.md`
- `architecture/README.md`
- `AGENTS.md`
- the existing reservation fallback plan file

Correct language: lower plausible local estimate may be canonical even when reservation is higher.

## Phase 3: Add finalizer integration tests

Helper tests are not enough. The current bug exists because the helper can choose the right value and finalizer code later overrides it.

Add finalizer-level tests that persist or inspect the final request update:

1. `test_estimated_local_cost_beats_higher_reservation_floor_regression`
   - Use the MiniMax fixture above.
   - Expected canonical cost is `21848`, not `5411079`.

2. `test_reservation_lower_than_local_estimated_can_win_when_plausible`
   - Reservation lower than local, both plausible.
   - Expected canonical cost is reservation.

3. `test_higher_reservation_does_not_floor_after_lower_local_selection`
   - Local lower than reservation, both plausible.
   - Expected canonical cost remains local.

4. `test_generic_estimate_not_floored_to_implausible_reservation`
   - Local and reservation both implausible.
   - Expected canonical cost is generic bounded estimate, not reservation.

5. `test_provider_reported_still_wins_over_estimates`
   - Provider-reported cost precedence unchanged.

6. `test_trusted_local_exactness_still_ignores_reservation`
   - `exact`, `derived`, and `partial` local values remain canonical.

## Phase 4: Verify repair behavior

Repair tooling should still correct historical rows, but new finalized rows should not need repair.

Verify with tests:

- historical row where canonical cost equals reservation while lower local estimate exists is repaired to local estimate;
- newly finalized MiniMax fixture row does not match the suspicious repair pattern because canonical cost is already local;
- provider-reported rows are skipped;
- rows with missing, zero, or higher local estimates are skipped.

## Phase 5: Verify dashboard/stat behavior

The dashboard warning should remain useful for old data. After the finalizer fix, new rows following the MiniMax fixture should not increment `reservation_fallback_rows`.

Verify:

- stats query counts old suspicious rows;
- repair apply drops that count;
- a correctly finalized new fixture row is not counted as reservation fallback;
- dashboard warning renders only when the count or excess is nonzero.

## Phase 6: Run validation

Run focused tests:

- `tests/unit/test_request_finalizer.py`
- `tests/unit/test_cost_inflation_guards.py`
- `tests/unit/test_pricing.py`
- `tests/unit/test_quota.py`
- `tests/unit/test_cost_repair.py`

Then run the full suite, ruff, and pyright.

## Manual database expectation

For newly finalized rows matching the MiniMax fixture:

- canonical cost equals local cost;
- canonical cost is lower than reservation;
- exactness remains `estimated`;
- local exactness remains `estimated`;
- `reservation_fallback_rows` does not increase.

MiniMax total cost must stay near the repaired observed usage and must not rebound to the reservation total.

## Acceptance criteria

- The blanket estimated-cost floor is removed or made provenance-aware so it cannot override lower local estimated choices.
- The live MiniMax fixture persists canonical cost `21848`, not `5411079`.
- A finalizer integration test fails before the floor fix and passes after it.
- Docs and comments no longer claim canonical estimated cost is floored to reservation.
- Repair tooling still fixes historical rows.
- Dashboard warnings still expose historical suspicious rows but do not trigger for correctly finalized new rows.
- Provider-reported precedence remains unchanged.
- Trusted local `exact`, `derived`, and `partial` precedence remains unchanged.
- Full tests, ruff, and pyright pass.

## Non-goals

- Do not rework the pricing parser again unless a separate parser regression is found.
- Do not add MiniMax-specific branches.
- Do not remove reservation accounting.
- Do not hide reservation diagnostics from the dashboard.
- Do not make dashboard totals depend on live recomputation instead of correct persisted canonical cost.

## Implementation note

This is a narrow precedence cleanup. The previous pass added the right machinery but left the old floor in place. The minimal correct diff should remove or narrow that floor, add the live MiniMax finalizer fixture test, update contradictory docs, and verify repair/dashboard behavior.
