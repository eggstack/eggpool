# Typed Failure Effects and Bounded Model Quarantine

Date: 2026-07-25
Status: completed — all workstreams implemented and verified, Plan 030 closure evidence at artifacts/plan-030-exact-head-evidence.md

Parent roadmap:

- `plans/022-upstream-error-isolation-and-hotpath-hardening-roadmap.md`

Depends on:

- `plans/023-error-isolation-reproducer-and-invariant-baseline.md`
- `plans/024-provider-bound-thinking-control-normalization.md`

## Objective

Centralize the consequences of request and upstream failures into one typed, test-pinned decision. Replace first-observation indefinite model withdrawal with bounded, provider/account/model/protocol-scoped quarantine that requires corroboration before becoming terminal and automatically clears when authoritative evidence or successful traffic demonstrates recovery.

This phase is the policy boundary between error classification and shared mutable state. It must ensure that unsupported thinking controls, client validation failures, context-limit errors, protocol-shape errors, and compatibility adaptations remain request-local.

## Current risk to close

Today, status classification, retry categorization, health transitions, runtime account state, catalog withdrawal, and durable backoff are applied in several locations. A misleading provider response can therefore be interpreted more broadly than intended. In particular, a model-like 404 can disable a model indefinitely and persist that fact, while generic unknown failures may still increment circuit state.

The implementation must make every shared-state effect explicit and auditable.

## Scope

### In scope

- Typed failure-effect decision.
- Unified classification inputs and outputs.
- Request-local/client-validation categories.
- Account, model, and provider-scoped effects.
- Bounded model quarantine state machine.
- Corroboration by repeated observations and authoritative catalog refresh.
- Automatic clear on successful request or model reappearance.
- Durable schema/migration for quarantine provenance and expiry.
- Hydration compatibility for historical backoff rows.
- Observability and operator controls.
- Full status/body/error-class matrix tests.

### Out of scope

- Provider request adaptation mechanics; Plan 024 owns them.
- Retained cleanup ownership; Plan 026 owns finalization execution.
- Database connection replacement; Plan 027 owns recovery.
- Routing-score redesign.
- Automatic disabling based on arbitrary natural-language similarity.

## Workstream A — Define a canonical failure observation

Create an immutable input object, for example:

```python
@dataclass(frozen=True, slots=True)
class FailureObservation:
    source: Literal[
        "client_validation",
        "provider_validation",
        "upstream_http",
        "transport",
        "stream",
        "finalization",
        "database",
    ]
    status_code: int | None
    error_class: str | None
    provider_id: str | None
    account_name: str | None
    model_id: str | None
    upstream_model_id: str | None
    client_protocol: str
    upstream_protocol: str
    response_signal: FailureSignal | None
    retry_after_s: float | None
    response_started: bool
```

The response signal must be extracted by conservative, bounded parsers. It may include:

- authentication failed;
- quota exhausted;
- rate limited;
- model absent;
- unsupported request control;
- context limit exceeded;
- generic client validation;
- temporary upstream failure;
- transport failure;
- unknown.

Do not store or propagate raw response bodies in the observation. Signal extraction may inspect a bounded response prefix and structured JSON fields, then discard content.

## Workstream B — Define `FailureEffects`

Create one immutable output object, for example:

```python
@dataclass(frozen=True, slots=True)
class FailureEffects:
    retry: bool
    retry_scope: Literal["none", "same_account", "other_account"]
    client_outcome: Literal[
        "client_error", "upstream_error", "service_unavailable", "timeout"
    ]
    account_effect: Literal[
        "none", "failure", "cooldown", "quota", "rate_limit", "disable_auth"
    ]
    model_effect: Literal["none", "quarantine", "terminal_withdrawal"]
    circuit_penalty: bool
    persist_backoff: bool
    backoff_reason: str | None
    backoff_until: float | None
    release_probe_only: bool
    evidence_class: str
```

Every coordinator/finalizer/health call site must consume this object rather than independently reclassifying status and error class.

Mandatory default: unknown client/provider validation produces no account/model/circuit effect and only releases any acquired probe slot.

## Workstream C — Establish an effects decision table

Create a single pure classifier with table-driven tests.

Minimum policy:

| Observation | Retry | Account effect | Model effect | Circuit penalty | Durable backoff |
|---|---:|---|---|---:|---:|
| Local capability rejection | No | None | None | No | No |
| Upstream unsupported thinking control | Optional compatibility path only | None | None | No | No |
| Context-limit validation | No | None | None | No | No |
| Generic HTTP 400/409/422 validation | No | None | None | No | No |
| HTTP 401 confirmed auth | Other account | Disable auth | None | Yes | Yes |
| HTTP 403 with explicit quota signal | Other account | Quota | None | No or policy-defined | Yes, bounded |
| HTTP 403 without auth/quota evidence | No by default | None | None | No | No |
| HTTP 402 quota | Other account | Quota | None | No | Yes, bounded |
| HTTP 404 authoritative model absence | Other account | None | Terminal withdrawal | No | Yes |
| HTTP 404 runtime model-like signal only | Other account if available | None | Quarantine | No | Yes, bounded |
| HTTP 404 generic route not found | No | None | None | No | No |
| HTTP 408/transport timeout | Other account | Cooldown/failure | None | Yes | Yes, bounded |
| HTTP 429 | Other account | Rate limit | None | No | Yes, Retry-After bounded |
| HTTP 5xx | Other account | Failure/cooldown | None | Yes | Yes, bounded |
| Client cancellation | No | None | None | No | No |
| Midstream transport failure | No retry after bytes | Failure | None | Yes | Bounded |
| Finalization/database failure | No provider retry | None | None | No | No provider backoff |

The exact account/circuit policy for quota/rate-limit may follow existing behavior, but it must be explicit and test-pinned.

## Workstream D — Implement model quarantine state machine

Replace immediate indefinite `disable_model()` on a runtime-only model-like failure with a state machine.

Suggested states:

```text
healthy
  -> suspected (first runtime observation; short TTL)
  -> quarantined (repeated observation within evidence window; longer TTL)
  -> terminal_withdrawn (authoritative catalog absence or explicit operator action)
```

Required properties:

- Keyed by provider ID, account ID, canonical model ID, upstream model ID, and upstream protocol.
- First observation TTL configurable, default 60–300 seconds.
- Repeated observation threshold configurable and bounded.
- Repetition requires equivalent normalized evidence, not any 404.
- Expiry automatically restores eligibility unless fresh evidence exists.
- Successful request clears suspected/quarantined state for the exact key.
- Authoritative provider model-list reappearance clears state.
- Catalog-confirmed absence may create terminal withdrawal according to existing withdrawal policy.
- Operator disable remains terminal until operator reversal.
- Alias failures cannot withdraw every alias/collapsed representation without identity proof.

## Workstream E — Separate authoritative and runtime evidence

Introduce explicit evidence provenance:

- `runtime_http`;
- `provider_catalog`;
- `model_info`;
- `manual_override`;
- `operator_action`;
- `migration_legacy`.

Terminal withdrawal may be produced only by:

- authoritative provider catalog under configured withdrawal policy;
- explicit operator action;
- curated manual override;
- repeated runtime evidence plus an explicit opt-in policy, if retained at all.

Default runtime behavior must remain bounded quarantine.

## Workstream F — Durable schema and hydration

Add or extend durable rows to include:

- exact scope key;
- state;
- reason/evidence class;
- first observed time;
- last observed time;
- observation count;
- expiry;
- authoritative source;
- source model ID or bounded hash where appropriate;
- last status/error class;
- cleared time and clear reason, or delete with audit event.

Migration requirements:

- Preserve historical account backoff and model-unavailable rows.
- Detect legacy model-unavailable rows lacking provenance.
- Hydrate them as `migration_legacy` with bounded quarantine unless an authoritative catalog record confirms absence or operator configuration explicitly requires terminal preservation.
- Do not silently re-enable explicit operator-disabled models.
- Migration must be idempotent and rollback-safe.

## Workstream G — Apply effects exactly once

Choose one authoritative application point per attempted request. The coordinator may calculate effects, but shared state must be applied once through a method such as:

```python
await failure_effects_applier.apply_once(
    attempt_identity,
    observation,
    effects,
)
```

Use an idempotency key including request/attempt identity and effect generation. Retried finalization must not double-penalize health or increment quarantine observations.

The finalizer must receive `effects_applied` or an immutable effects record. It must not call `classify_failure_category()` independently for the same terminal event.

## Workstream H — Routing integration

Eligibility must consult active quarantine for the exact key. It must not mutate quarantine during read.

When one account/provider is quarantined:

- Other providers for the same collapsed model remain eligible.
- Other accounts under the same provider remain governed by their own evidence key unless provider-wide evidence is authoritative.
- Other protocols remain eligible if their provider contract differs.
- Expired quarantine is pruned lazily or by bounded maintenance.

Routing traces should record `model_quarantine` exclusion with provenance and remaining TTL, without storing provider body content.

## Workstream I — Operator interface and diagnostics

Add read-only diagnostics for active model quarantine and evidence. Reuse existing CLI/dashboard/admin patterns.

Required operations:

- List active suspected/quarantined/terminal entries.
- Show scope, evidence source, observation count, expiry, and reason.
- Clear a bounded quarantine explicitly.
- Do not permit unauthenticated mutation through public proxy endpoints.

Readiness must not fail merely because one model is quarantined. It may fail if all configured traffic paths are unavailable under existing readiness policy, but that is separate from database health.

## Workstream J — Tests

Suggested files:

- `tests/unit/test_plan_025_failure_effects_table.py`
- `tests/unit/test_plan_025_failure_signal_extraction.py`
- `tests/unit/test_plan_025_model_quarantine_state_machine.py`
- `tests/unit/test_plan_025_quarantine_hydration.py`
- `tests/integration/test_plan_025_error_isolation.py`
- `tests/integration/test_plan_025_cross_provider_quarantine.py`
- `tests/unit/test_plan_025_effects_idempotency.py`
- `tests/unit/test_plan_025_quarantine_cli.py`

Required status matrix includes 400, 401, 402, 403 quota/non-quota, model-like and generic 404, 408, 409, 422, 429, all relevant 5xx statuses, transport exceptions, client cancellation, midstream failure, capability errors, finalization failures, and database failures.

## Acceptance criteria

### Canonical effects

- [ ] One pure classifier produces all retry and shared-state effects.
- [ ] Coordinator, finalizer, health manager, runtime state, and backoff persistence do not independently reinterpret the same failure.
- [ ] Every matrix row has exact field assertions.
- [ ] Unknown validation defaults to zero shared-state effects.
- [ ] Effects are applied idempotently once per attempt outcome.

### Request-local failures

- [ ] Local capability rejection changes no account/model/circuit/backoff state.
- [ ] Upstream unsupported thinking control changes no shared health state.
- [ ] Context-limit and generic 400/409/422 validation change no shared health state.
- [ ] Client cancellation changes no provider health state.
- [ ] Finalization/database errors never create provider backoff.
- [ ] Acquired probe slots are released on every request-local path.

### Model quarantine

- [ ] First runtime model-like 404 creates bounded suspected/quarantine state, not indefinite disablement.
- [ ] Generic 404 does not quarantine a model.
- [ ] Repeated equivalent evidence promotes deterministically.
- [ ] Expiry restores eligibility automatically.
- [ ] Exact-key success clears bounded quarantine.
- [ ] Provider catalog reappearance clears bounded quarantine.
- [ ] Terminal withdrawal requires authoritative or explicit operator evidence by default.
- [ ] One provider/account quarantine does not suppress alternate providers/accounts/protocols.

### Persistence and migration

- [ ] Durable records include scope, provenance, count, and expiry.
- [ ] Legacy model-unavailable rows migrate deterministically.
- [ ] Explicit operator disables remain terminal.
- [ ] Migration is idempotent and preserves request/usage history.
- [ ] Restart hydration reproduces the same unexpired state.
- [ ] Expired entries do not reappear after restart.

### Observability and operations

- [ ] Routing trace exposes quarantine exclusion without raw body content.
- [ ] CLI/admin diagnostics list and clear bounded quarantine.
- [ ] Counters distinguish request-local validation, bounded quarantine, and terminal withdrawal.
- [ ] No unbounded error text is persisted.

### Verification

- [ ] Plan 023 and Plan 024 focused suites remain green.
- [ ] Full effects matrix passes on Python 3.11 and 3.12.
- [ ] Multi-provider integration proves isolation.
- [ ] Standard non-slow suite passes.
- [ ] Ruff format, Ruff check, Pyright, and xfail/skip audit pass.

## Closure evidence

The implementation record must include the complete failure-effects table as actually implemented, migration behavior for representative legacy rows, and a state-audit diff from the MiniMax-M3 unsupported-thinking reproducer. Closure requires proof that the failed request adds request history only and that immediately following unrelated and corrected requests succeed without restart or database deletion.
