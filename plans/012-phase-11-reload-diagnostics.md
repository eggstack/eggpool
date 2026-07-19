# Phase 11 — Reload Diagnostics and Operational Semantics

Date: 2026-07-19
Status: implementation handoff
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`
Prerequisites: Phases 1–7; align with Phase 10 protocol work.

## Objective

Make every reload outcome observable, internally consistent, and stage-accurate. Replace scattered early-return bookkeeping with one terminal finalization path derived from the Phase 6 transaction state.

Operators, CLI clients, dashboard consumers, and tests should agree on what changed, which generation is active, whether retirement remains pending, and where a failure occurred.

## Problems addressed

Current diagnostic behavior can leave stale or misleading state:

- semantic no-op and ignored-only changes can return before counters and terminal metadata are updated;
- reconciliation or publication failures can be reported as validation failures;
- operation state may not consistently return to idle;
- `retirement_pending` can be inferred from generic success instead of actual runtime-manager state;
- active config digest and generation mirrors can disagree;
- cleanup or compensation errors may be logged but absent from the control result.

## Non-goals

- Do not expose full configuration, credentials, API keys, or raw exception traces.
- Do not create unbounded event history in memory.
- Do not make diagnostics perform blocking database or network work on request.
- Do not hide primary errors behind cleanup details.
- Do not change protocol fields casually; version response changes where compatibility requires it.

## Canonical reload result model

Define one typed result used by:

- reload manager internal state;
- control protocol response;
- CLI formatting;
- runtime diagnostic endpoint;
- operational event persistence;
- tests.

Suggested fields:

- request ID;
- result category;
- admitted timestamp;
- started/completed timestamps and duration;
- terminal transaction stage;
- old generation ID/digest;
- candidate/new generation ID/digest;
- active generation ID/digest after completion;
- changed sections;
- ignored sections;
- restart-required sections;
- semantic no-op flag;
- publication occurred;
- persistence committed;
- process transitions applied;
- compensation attempted/succeeded;
- candidate cleanup attempted/succeeded;
- retirement pending and retiring generation IDs;
- stable error code/class and bounded message;
- warning list with bounded count.

Use enums for result category and stage, not free-form strings spread across call sites.

## Result categories

At minimum distinguish:

- `success_committed`;
- `success_noop`;
- `success_ignored_only`;
- `rejected_busy`;
- `rejected_validation`;
- `rejected_restart_required`;
- `failed_candidate_prepare`;
- `failed_persistence_prepare`;
- `failed_process_transition_prepare`;
- `failed_commit`;
- `failed_publication`;
- `failed_process_transition_apply`;
- `failed_persistence_commit`;
- `aborted_cancelled`;
- `aborted_shutdown`;
- `compensation_failed`;
- `internal_error`.

Names may follow existing conventions, but stage and outcome must remain separate concepts.

## Single finalization path

Refactor reload execution so every admitted operation reaches one finalizer in `finally` or an equivalent structured context manager.

The finalizer should:

1. derive terminal outcome from transaction state and primary error;
2. capture active-generation and retirement snapshots;
3. update current/last reload state;
4. increment appropriate counters;
5. set completion time and duration;
6. return operation state to idle;
7. persist a bounded operational event best-effort;
8. release admission claim;
9. produce the canonical result.

Busy rejections are not admitted operations but should still produce a canonical lightweight result and optional event.

Avoid multiple early returns that bypass terminal bookkeeping. Early decision branches should set a result category and flow through the same finalizer.

## Counter semantics

Define counters precisely:

- total requests;
- admitted operations;
- busy rejections;
- committed reloads;
- no-op outcomes;
- ignored-only outcomes;
- validation/restart-required rejections;
- prepare failures;
- commit failures;
- cancellations;
- compensation failures;
- retirement failures.

Do not overload one `reload_count` with ambiguous meaning. Preserve existing public metrics through aliases if necessary, but document them.

## Stage accuracy

The terminal stage must come from the Phase 6 transaction state. Do not map all generic exceptions to validation.

Examples:

- database delta application error before publication: persistence/commit stage;
- process transition preflight error: process prepare stage;
- runtime manager install error: publication stage;
- candidate close error after primary preparation failure: original stage plus cleanup warning;
- SQLite commit failure after publication: persistence commit stage with compensation metadata.

## Retirement status

Derive retirement fields from Phase 3 runtime-manager diagnostics after commit finalization:

- no old generation: pending false;
- old generation draining: pending true with ID;
- old generation already closed: pending false;
- retirement task failed: pending/failed represented explicitly.

Do not set pending merely because result is successful.

## Error and warning policy

Primary error:

- stable code/class;
- concise redacted message;
- actual transaction stage;
- correlation request ID.

Warnings:

- cleanup failures;
- forced retirement;
- best-effort operational event failure;
- ignored config sections;
- optional-state hydration degradation.

Bound warning count and message length. Structured logs may include stack traces internally, but API/control responses must not.

## Persistence and history

If an operational events table exists, persist one terminal event per admitted reload plus optional requested/busy events. Avoid partial event sequences that cannot be correlated.

Consider a bounded reload history query containing only:

- request ID;
- timestamps;
- result category;
- stage;
- generation IDs/digests;
- duration;
- redacted error code;
- retirement outcome.

Retention policy should follow existing operational-event cleanup conventions.

## CLI and dashboard presentation

CLI should clearly distinguish:

- configuration valid but no semantic changes;
- ignored-only changes;
- restart required;
- committed with old generation draining;
- failed before publication;
- failed with successful compensation;
- critical compensation failure.

Dashboard/runtime diagnostics should display:

- active generation and digest;
- last reload result/time/duration;
- current transaction stage if active;
- retirement count/state;
- last critical reload error.

Do not block the dashboard waiting for transaction internals.

## Tests

### Outcome matrix

Exercise every result category and assert:

- result category;
- terminal stage;
- counters;
- current operation state;
- active generation/digest;
- event record;
- error/warning redaction.

### No-op and ignored-only

Assert both update last-result metadata, completion timestamp, counters, and idle state without constructing/publishing a candidate.

### Stage classification

Inject faults at every Phase 6 barrier and assert stage is exact.

### Cleanup and compensation

Inject primary failure plus cleanup failure. Assert primary remains primary and cleanup appears as warning. Inject compensation failure and assert critical category.

### Retirement

Cover no retirement, pending retirement, completed retirement, forced close, and failed retirement.

### Busy and cancellation

Busy response returns promptly with active request metadata limited to safe fields. Cancellation/shutdown outcomes finalize and release admission.

### Protocol compatibility

Validate response serialization for current and any new protocol version. Ensure old clients receive a compatible subset or explicit version error.

## Implementation sequence

1. Define canonical enums and result dataclass/model.
2. Map Phase 6 transaction states to terminal stages.
3. Introduce one finalization context/path.
4. Remove or redirect early-return bookkeeping.
5. Define counters and metric names.
6. Derive active/retirement state from runtime manager.
7. Align operational-event persistence.
8. Update control response and CLI formatting.
9. Update dashboard/runtime diagnostics.
10. Add complete outcome matrix tests.

## Acceptance criteria

- Every admitted reload reaches one terminal finalizer.
- Operation state returns to idle on every outcome.
- No-op and ignored-only operations update last-result metadata and counters.
- Every injected failure reports its actual transaction stage.
- Active generation/digest in diagnostics matches runtime manager state.
- Retirement status reflects actual tracked retirement tasks.
- Primary errors are preserved; cleanup/compensation details are structured separately.
- Responses and persisted events are bounded and secret-redacted.
- CLI, dashboard, control response, and metrics agree on outcome semantics.
- Protocol changes are versioned or backward compatible.

## Handoff evidence

Provide the result schema, outcome-to-stage mapping table, counter definitions, sample CLI/control outputs for major outcomes, redaction tests, and the full diagnostic outcome matrix results.