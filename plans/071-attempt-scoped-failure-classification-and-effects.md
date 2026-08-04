# Plan 071 — Attempt-Scoped Failure Classification and Effects

Date: 2026-08-04
Status: complete
Parent roadmap: `plans/070-failure-resilience-router-recovery-and-sbc-simplification-roadmap.md`
Planning baseline: `e73db213e7e381043cda3cfb8a3dd8109f3f39ca`

## Purpose

Replace the current split retry/health/backoff interpretation with one typed failure decision and make shared-state effects idempotent by the actual durable attempt identity.

This is the semantic foundation for all later resilience work. It must correct concrete stuck-state and misclassification defects without introducing a policy framework, plugin system, rule language, or comprehensive fault matrix.

## Confirmed defects

### 1. Retry and effects can disagree

`src/eggpool/retry/classification.py` classifies HTTP responses for retry. `src/eggpool/failure/classifier.py` independently classifies observations for account, model, circuit, and backoff effects. Coordinator fallbacks also call `classify_failure_category()` directly.

A response can therefore be retryable but produce no shared-state consequence, or produce a consequence that does not match the reason for rerouting.

Representative mismatches:

- 403 with a quota body can be retried as quota but reaches the effects classifier with no response signal;
- 409/422 with quota evidence can be retried, while the effects classifier treats those codes as request-local;
- model-specific 404 evidence is reduced to an exception class before effects are decided;
- transport exceptions are commonly submitted as `source="upstream_http"` rather than `source="transport"`.

### 2. Effect idempotency collides across independent requests

The coordinator builds effect keys from failure shape, for example account, model, provider, protocol, status, and error class. It omits both proxy request ID and durable attempt ID.

`EffectsApplier` retains those keys in a process-global dictionary. A later request with the same failure shape can be treated as a duplicate and skip health, circuit, model, or probe-release work.

This can strand a half-open probe or leave the current request's runtime state unconverged until restart.

### 3. A naive unique key would leak memory

Changing the key to a unique request/attempt string while leaving `EffectsApplier._applied` process-global would make the dictionary grow for the lifetime of the process.

The effect-applied fact belongs to one retained attempt lifecycle and must be retired when that lifecycle converges.

### 4. Circuit failure can be recorded twice

`HealthManager.record_failure()` records a circuit-breaker failure. `EffectsApplier._apply_circuit_penalty()` can then record another failure for the same observation.

One provider failure must produce at most one circuit transition.

### 5. Probe-release ownership is implicit

Some effect branches call `release_request()`, some call `record_failure()`, some rely on finalization, and duplicate-key no-ops can skip all of them.

Probe convergence must be an explicit component result for the current attempt, not a side effect inferred from which classifier branch happened to run.

## Scope

Primary files:

- `src/eggpool/failure/observation.py`
- `src/eggpool/failure/effects.py`
- `src/eggpool/failure/classifier.py`
- `src/eggpool/failure/applier.py`
- `src/eggpool/retry/classification.py`
- `src/eggpool/request/coordinator.py`
- `src/eggpool/request/finalization_job.py`
- `src/eggpool/request/finalizer.py`
- `src/eggpool/request/attempt_finalizer.py`
- `src/eggpool/health/health_manager.py`

Focused existing tests should be used from:

- `tests/unit/test_retry_classification.py`
- existing failure-effects tests;
- existing request coordinator retry/cleanup tests;
- existing finalization state-machine tests;
- existing health/circuit tests.

## Explicitly out of scope

- changing the number of retry attempts;
- changing downstream handoff rules;
- changing backoff caps; Plan 073 owns policy durations;
- changing SQLite recovery architecture; Plan 074 owns that work;
- adding provider-specific Python classes for every vendor;
- adding a policy DSL, rules engine, registry plugin, or dynamic code loading;
- persisting raw response bodies or credentials;
- adding a schema migration unless an existing durable attempt/result field cannot represent the invariant;
- adding a permanent event log for every classifier decision;
- adding a test for every possible HTTP status and exception combination;
- adding CI jobs, coverage gates, soak tests, or live-provider tests.

## Governing decisions

1. One immutable decision is produced from one normalized failure input.
2. The decision includes both request behavior and shared-state behavior.
3. The durable attempt identity is `(proxy_request_id, attempt_id)`; account/model/status shape is diagnostic metadata only.
4. Effect application is resumable and idempotent for one attempt, but separate attempts never collide.
5. Effect progress is retired with the retained attempt/finalization owner.
6. Provider penalties require provider evidence.
7. Request-local and EggPool-local failures release acquired runtime ownership without modifying provider health.
8. One attempt can record at most one circuit success or failure transition.
9. Raw body inspection remains bounded and produces only a normalized signal; raw content is not retained.
10. Existing public error classes may remain compatibility adapters, but they must carry or derive from the canonical decision rather than reclassify independently.

## Phase A — Define the canonical failure input and decision

### Required changes

Create or refine small immutable types in `eggpool.failure`.

A normalized input should carry only facts available at the failure boundary:

- source: client validation, local preparation, transport, upstream HTTP, stream, database, finalization, cancellation;
- proxy request ID;
- durable attempt ID when selected;
- provider ID, account name, model ID, upstream model ID;
- client and upstream protocol;
- status code;
- bounded normalized headers relevant to retry, especially `Retry-After`;
- bounded normalized response signal derived from body shape/text;
- exception class category, not a raw traceback payload;
- downstream-started fact;
- retry-after seconds when valid.

The canonical decision should include:

- retry permitted before handoff;
- retry scope: none or other account;
- client outcome/status family;
- account effect;
- model effect;
- circuit transition: none, success, or failure;
- backoff reason and optional requested duration/deadline;
- probe convergence requirement;
- evidence class/source;
- normalized response signal;
- whether the decision is provider-attributable.

Do not expose an open-ended dictionary as the core contract. A small dataclass/enum surface is preferred.

### Classification rules

At minimum, preserve and align these representative decisions:

- malformed client JSON, capability rejection, context-limit rejection: no retry, no provider effect;
- local request construction/serialization/transcoding bug: no provider effect;
- 400 generic validation: no retry, no provider effect;
- 401 confirmed authentication: retry another account when available, terminal account auth state;
- 403 quota evidence: retry another account, bounded quota suppression;
- 403 auth evidence: retry another account, terminal account auth state;
- 403 without evidence: client error, no provider effect;
- 402 quota: retry another account, bounded quota suppression;
- model-specific runtime 404: retry another account, bounded account/model quarantine;
- generic route 404: client/upstream compatibility error, no account suppression;
- 408: retry another account, bounded timeout/circuit effect;
- 409/422 with explicit quota evidence: retry another account, bounded quota/rate effect;
- 409/422 without evidence: no provider penalty;
- 429: retry another account, bounded rate-limit effect honoring later policy clamp;
- 5xx: retry another account, bounded server/circuit effect;
- connect/read/write/pool/remote-protocol timeout before handoff: retry another account, bounded transport/circuit effect;
- client cancellation: no provider penalty;
- midstream transport failure after handoff: no retry, provider-attributable failure only when evidence identifies upstream transport;
- database/finalization failure: no provider penalty.

### Acceptance criteria

- One pure classifier returns a complete decision for every representative case.
- Retry and shared-state fields are present in the same result.
- Response-body evidence used for retry is present in the decision used by effects.
- Transport failures carry `source=transport` or an equivalent typed source.
- Unknown failures default to no provider effect, not speculative suppression.
- No raw body, header set, credential, or traceback is retained in the decision.

## Phase B — Route coordinator behavior through the decision

### Required changes

1. At the first boundary where an upstream response or transport exception is known, build one canonical decision.
2. Carry that decision in the retryable/non-retryable wrapper or replace the wrappers with a compact typed failure object.
3. `_classify_upstream_error()` must not ask one classifier for retry and later reconstruct effects from status/error class.
4. `_RetryableUpstreamError` may remain temporarily for compatibility, but it must contain the canonical decision.
5. `_NonRetryableUpstreamError` must also carry a decision when selected attempt cleanup or probe release is required.
6. `_cleanup_failed_attempt()`, `_handle_exhausted()`, finalization, and health/backoff application must consume the same decision.
7. Remove body-signal loss between `RetryClassifier` and `FailureObservation`.
8. Remove direct coordinator reclassification branches once their behavior is represented by the decision.
9. Keep `CancelledError` control flow distinct; do not wrap it as a failure decision.
10. Do not catch `BaseException` for ordinary classification.

### Compatibility strategy

- Retain existing error classes and public status rendering where practical.
- Convert old classifiers into narrow adapters to the canonical classifier during migration.
- Delete duplicate decision tables after all production call sites use the canonical result.
- Do not maintain two permanent authoritative classifiers.

### Acceptance criteria

- The retry loop, attempt cleanup, finalization, health, circuit, quarantine, and backoff all consume one decision instance or an exact immutable copy.
- No coordinator path infers provider effects solely from exception class after a decision exists.
- Ambiguous 403/409/422 and model-specific 404 evidence reaches the shared-state effect path.
- Transport exceptions no longer fall through the generic upstream-HTTP unknown path.

## Phase C — Make effects attempt-scoped and bounded

### Required changes

1. Define the effect identity from actual selected-attempt facts:
   - proxy request ID;
   - durable attempt ID.
2. Store component progress on the retained attempt cleanup/finalization owner, or in another structure whose lifecycle is exactly that owner.
3. Required component markers should cover at least:
   - account effect applied;
   - model effect applied;
   - circuit transition applied;
   - probe converged;
   - durable backoff persistence attempted/completed where relevant.
4. Rejoining the same attempt resumes only incomplete components.
5. A separate attempt with identical account/model/status/error facts receives a separate effect identity and applies normally.
6. Retire effect progress after the attempt terminal owner has converged and released operational references.
7. Remove or bound `EffectsApplier._applied`; it must not grow with process lifetime.
8. Do not use the supervisor's 64-entry diagnostic history as the idempotency boundary.
9. Durable backoff persistence remains best-effort and may be retried only as part of the bounded retained owner; it cannot hold the client response indefinitely.

### Acceptance criteria

- Two consecutive requests that both receive the same 503 on the same account each record one independent failure.
- Replaying cleanup/finalization for one attempt does not record a second failure.
- A duplicate replay cannot strand a probe.
- Effect-progress memory is bounded by active retained attempt ownership, not total process requests.
- Completed operational references are released.

## Phase D — Enforce one circuit transition and explicit probe convergence

### Required changes

1. Choose one owner for the circuit transition.
2. Recommended approach:
   - `HealthManager.record_failure()` owns both health count and one circuit failure;
   - the effects applier does not call `circuit_breaker.record_failure()` again for that same account effect.
3. If a decision needs a circuit penalty without an account failure transition, provide one explicit method and mark the circuit component complete once.
4. A success path records one circuit success.
5. Request-local rejection releases a half-open slot without recording success or failure.
6. Quota/rate/model effects that release a probe must mark the probe component converged.
7. Every selected attempt terminal path must leave the circuit breaker with no orphaned half-open in-flight flag.
8. Preserve idempotency when release methods are called more than once.

### Acceptance criteria

- One 5xx or transport attempt increments the configured circuit failure count once.
- Three configured failures require three failed attempts, not two attempts plus duplicate penalties.
- Client validation and capability rejection do not change the failure count.
- A half-open probe is cleared by success, provider failure, request-local rejection, cancellation, or terminal cleanup.
- A replay of any path does not change the circuit a second time.

## Phase E — Consolidate tests and remove duplicate paths

### Focused tests

Use representative parameterization rather than exhaustive matrices.

Required cases:

1. 403 quota body produces retry + bounded quota effect.
2. 403 without evidence produces no provider effect.
3. 409 or 422 quota body produces retry + bounded effect; generic body does not.
4. model-like 404 produces retry + bounded model effect; generic 404 does not.
5. connect/read/protocol failure carries transport source and provider effect.
6. client capability failure carries no provider effect.
7. two separate identical failures each apply once.
8. one attempt replay applies no component twice.
9. one failure increments the circuit once.
10. request-local rejection releases a half-open probe.
11. effect progress is retired after terminal convergence.

Prefer adding these to existing failure, retry, health, and request-lifecycle test modules. Do not create `test_plan_071.py`.

### Cleanup

- Remove duplicate status tables and direct coordinator classification branches after migration.
- Remove stale comments claiming a classifier is authoritative when production still bypasses it.
- Update architecture documentation and `AGENTS.md` only where the production contract changes.
- Do not retain compatibility adapters that are unused by production or supported integrations.

## Verification

Run affected checks first:

```bash
uv run ruff format src/eggpool/failure src/eggpool/retry src/eggpool/request src/eggpool/health tests/unit
uv run ruff check src/eggpool/failure src/eggpool/retry src/eggpool/request src/eggpool/health tests/unit
uv run pyright src/eggpool/failure src/eggpool/retry src/eggpool/request src/eggpool/health
uv run pytest <affected failure/retry/health/coordinator/finalization tests> -q --tb=short --maxfail=1
```

Then run the existing repository gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Do not add CI jobs or require live providers.

## Recommended implementation sequence

1. Write the canonical immutable input/decision types and pure representative classifier tests.
2. Make retry wrappers carry the decision without changing routing behavior.
3. Route retry cleanup and finalization through the decision.
4. Move effect progress into attempt-scoped retained ownership.
5. remove shape-keyed process-global idempotency.
6. correct circuit/probe component ownership.
7. delete duplicate production classifiers and coordinator branches.
8. run focused checks and smoke.
9. update architecture documentation and mark this plan complete.

## Plan acceptance criteria

- [x] One canonical failure decision includes retry and shared-state effects.
- [x] Provider response-body signals reach the effect path unchanged.
- [x] Transport failures are classified as transport failures.
- [x] Unknown/local failures default to no provider penalty.
- [x] Effect identity is `(proxy_request_id, attempt_id)` or an equivalent durable attempt identity.
- [x] Two independent identical failures do not collide.
- [x] Replay of one attempt does not replay effects.
- [x] Effect progress is retired and cannot grow with lifetime request count.
- [x] One failed attempt records at most one circuit failure.
- [x] Every selected terminal path explicitly converges the probe slot.
- [x] Duplicate retry/effect decision tables are removed from production paths.
- [x] Focused tests and the existing smoke gate pass.
- [x] No schema, dependency, policy engine, provider plugin system, CI expansion, or exhaustive fault matrix is added.

## Rejection conditions

Do not close this plan if:

- retry and effects can still classify the same response differently;
- a response-body signal is discarded before health/backoff application;
- transport errors still arrive as generic unknown HTTP observations;
- idempotency remains keyed by account/model/status/error shape;
- a unique-key dictionary grows without retirement;
- one attempt can increment the circuit twice;
- a duplicate submission can leave a half-open probe occupied;
- local request errors can suppress an account;
- both old and new classifiers remain authoritative production paths;
- tests expand into a status-by-exception Cartesian suite or new CI job.

## Definition of done

Plan 071 is complete when one immutable attempt-scoped decision controls retry and all shared-state effects, response and transport evidence are preserved, separate requests cannot collide in effect idempotency, one attempt can safely resume without replay, circuit/probe transitions are exactly once, obsolete classification paths are removed, and the focused plus smoke checks pass without new infrastructure.
