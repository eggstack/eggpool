# Plan 136 — Phase 5: RequestCoordinator Decomposition

## Objective

Reduce `src/eggpool/request/coordinator.py` from a monolithic orchestration implementation into smaller ordinary lifecycle components while preserving all externally visible behavior and ownership invariants.

## Non-goal

This is not a rewrite and must not introduce an orchestration framework, workflow engine, event bus, generic state-machine library, dependency-injection container, or additional process/background worker.

## Invariants to preserve

- no upstream dispatch before required durable request/reservation/attempt commit;
- response handoff remains the retry boundary;
- only typed transport failures may retry under existing policy;
- local preparation/transcode/response-adaptation failures do not suppress another provider;
- every accepted attempt has exactly one finalization owner;
- pending selection claims convert/release exactly once;
- client cancellation and streaming EOF classifications retain current semantics;
- transient upstream suppression remains bounded and scoped;
- generation leases remain valid through stream completion.

## Target shape

Keep `RequestCoordinator` as a thin sequencing facade with dependencies and public entrypoints. Extract coherent, mostly stateless helpers/services along existing responsibility lines, for example:

1. provider-bound request preparation;
2. selection + durable dispatch claim;
3. upstream send/handoff;
4. response adaptation/usage observation;
5. failure effect application/backoff decisions;
6. finalization submission/terminal outcome construction.

Use existing modules such as `attempt_finalizer.py`, `finalizer.py`, `finalization_job.py`, `provider_bound_request.py`, `response_handoff.py`, and retry/failure modules rather than creating duplicate abstractions.

## Method

1. Inventory coordinator methods and classify each by responsibility and state dependencies.
2. Identify pure/static helpers first and move them with no behavior change.
3. Move provider-request preparation next.
4. Move failure/response adaptation logic only where there is already a natural module boundary.
5. Reduce coordinator-owned mutable state; do not duplicate state across components.
6. Keep one authoritative retry loop and one authoritative terminal-finalization path.
7. Remove compatibility shims only after all callers migrate.
8. Update architecture docs after the final shape is stable, not after every mechanical extraction.

## Size/complexity guardrails

- A new module should own a real lifecycle responsibility, not one function purely to reduce line count.
- Avoid classes when a function/dataclass is enough.
- Do not create interfaces with only one implementation unless needed for existing tests or a genuine provider boundary.
- Net source size should preferably decrease or remain roughly flat; a decomposition that adds substantial scaffolding fails the goal.

## Tests

Run existing focused request/retry/finalization/streaming tests after each extraction. Add regression tests only for defects exposed during decomposition; do not duplicate implementation-path tests.

Required behavior coverage:

- first-attempt success;
- retryable pre-handoff transport failure;
- upstream HTTP failure with scoped effects;
- local transcode/preparation failure with no provider punishment;
- non-stream adaptation failure;
- streaming success/client cancel/premature EOF;
- persistence failure before dispatch;
- finalization ownership and compensation.

## Acceptance criteria

- `RequestCoordinator` is materially smaller and reads as orchestration rather than containing most implementation details.
- There is one retry decision authority and one finalization ownership model.
- No new runtime process/task topology.
- Existing public/API behavior and error shapes remain compatible.
- Existing smoke/lint/type CI remains sufficient; no new mandatory workflow.
- Architecture documentation identifies the new responsibility boundaries.

## Out of scope

Changing routing strategy, changing DB durability policy, adding Responses API, changing streaming wire formats, or broad performance rewrites unrelated to the decomposition.
