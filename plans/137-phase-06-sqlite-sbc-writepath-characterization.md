# Plan 137 — Phase 6: SQLite/SBC Write-Path Characterization and Narrow Tuning

## Objective

Determine whether EggPool's correctness-critical SQLite work materially contributes to local-model dispatch overhead on Raspberry Pi/SBC-class storage, then make only evidence-backed low-risk changes.

## Principles

- Do not introduce an alternate weak-consistency/"fast" mode.
- Do not remove durable request/reservation/attempt persistence merely to win a benchmark.
- Do not tune SQLite pragmas by folklore.
- Prefer reducing unnecessary statements/transactions over adding caches/queues.
- Diagnostic/analytics writes remain subordinate to the data plane.

## Measurement set

Use representative profiles rather than an elaborate benchmark harness:

1. in-memory/fake upstream to establish local software floor;
2. localhost local-runtime upstream with low TTFT;
3. LAN local-runtime upstream;
4. SBC storage profile where available.

Measure median/p95 local pre-upstream time, SQLite lock wait, statement counts by operation kind, transaction count, WAL growth, and CPU/RSS. Reuse existing dispatch/database diagnostics where possible. Do not add permanent benchmark telemetry solely for this plan.

## Work items

### 1. Dispatch persistence audit

Trace the request/reservation/attempt transaction and identify redundant reads/writes that can be folded without changing durability semantics. Check whether any diagnostic fields are written synchronously even when their feature is disabled.

### 2. Read contention

Confirm dashboard/statistics behavior with `worker_threads=1` SBC default and optional read-only stats connection. Avoid opening extra connections by default unless measurement demonstrates material contention.

### 3. WAL residue

Evaluate `PRAGMA journal_size_limit` as a bounded-storage policy after checkpoints. If adopted, make it an explicit conservative config/default documented for SBCs. Do not aggressively lower `wal_autocheckpoint` without evidence.

### 4. Database lifecycle clarity

Review `_transition_state()` and related lifecycle helpers. Either enforce the transition invariants the method/documentation claims or simplify the naming/documentation so there is no false safety contract.

### 5. Schema baseline decision

Document, but do not necessarily execute in this phase, a 1.0 migration-baseline policy: once compatibility policy permits, replace indefinite pre-1.0 migration archaeology with a supported baseline/bridge strategy. Do not destructively squash migrations while current installs still need them.

## Changes allowed only with evidence

- connection count/default worker count;
- cache size/mmap size;
- checkpoint cadence;
- synchronous mode;
- transaction structure.

The existing WAL + `synchronous=NORMAL` SBC posture should remain unless measured results and durability analysis support a change.

## Tests

Use existing DB/request tests for correctness. Add only focused tests for any changed pragma/config and lifecycle invariant. Performance evidence can be recorded in the plan/commit notes or existing perf tooling; do not create a new mandatory benchmark gate.

## Acceptance criteria

- A concise before/after characterization exists for representative local dispatch.
- Any default change is tied to measured improvement and documented durability/storage tradeoff.
- No correctness-critical write is made best-effort.
- Diagnostic writes remain disabled/sampled under the SBC profile.
- WAL residue is bounded if measurement shows a meaningful storage benefit and the pragma is safe.
- DB lifecycle transition documentation matches executable behavior.
- No alternate consistency mode, new writer service, or CI benchmark job is introduced.
