# Plan 078 — Runtime Invariant and Request-Boundary Corrections

Date: 2026-08-05
Status: completed (2026-08-05)
Parent roadmap: `plans/077-sbc-lifecycle-simplification-and-runtime-correctness-roadmap.md`
Planning baseline: `cd8967799e6613f3a5965af8cd15ce3c5269aaa8`

## Purpose

Correct a bounded set of confirmed runtime and request-boundary defects before changing lifecycle ownership. This plan must improve truthfulness and fail-closed behavior without introducing another supervisor, compatibility mode, configuration migration framework, or test harness.

The required corrections are:

1. an acquired `AttemptRuntimeLease` component cannot count as released when its dependency is missing;
2. unsupported Granian runtime thread counts must fail configuration validation;
3. duplicate terminal submissions must use semantic comparison rather than `repr()`;
4. generation-candidate cleanup diagnostics must redact and bound exception text;
5. forwarded client-IP headers must be trusted only from configured reverse proxies;
6. stale documentation must consistently define stream handoff as ASGI response start;
7. production configuration must not silently bind an ephemeral port unless an existing explicit test-only path requires it.

## Scope boundaries

In scope:

- `src/eggpool/request/finalization_job.py`
- `src/eggpool/models/config.py`
- `src/eggpool/runtime_manager.py`
- `src/eggpool/api/proxy_request.py`
- central redaction helpers under `src/eggpool/security/`
- directly affected configuration examples and architecture documentation
- focused tests in existing unit/integration files

Out of scope:

- supervisor ownership changes;
- terminal command consolidation;
- database recovery redesign;
- model-quarantine ordering;
- dashboard redesign;
- dependency replacement;
- generalized proxy trust middleware.

## Workstream A — Make runtime lease convergence truthful

### Current defect

`AttemptRuntimeLease.release_once()` derives the required component set from both the acquired flag and presence of a dependency. When a component was acquired but its dependency is `None`, it is omitted from the required set. The lease may then set `released=True` even though no release operation occurred.

### Required behavior

For each component:

- `active_count` is required whenever `active_count_acquired=True`;
- `quota_reservation` is required whenever `quota_reservation_acquired=True`;
- `health_probe` is required whenever `health_probe_acquired=True`.

Dependency availability must not determine whether a component is required.

When an acquired component has no dependency:

1. append one `RuntimeReleaseOutcome` with `released=False` and a bounded non-secret error such as `missing dependency: router`;
2. do not add the component to `_released_components`;
3. leave `released=False`;
4. retain retry/invariant ownership through the existing finalization result;
5. do not synthesize provider health evidence.

When a dependency exists but exposes neither supported release method, treat that as the same local invariant failure. Do not silently skip it.

Keep release idempotence. A component already in `_released_components` must not execute again.

### Result projection

Audit every place projecting:

- `runtime_cleanup_complete`;
- `active_count_decremented`;
- `quota_reservation_removed`;
- `health_released_or_recorded`;
- `retryable`.

A result must not report complete cleanup until every acquired component is in `_released_components` and every required terminal outcome obligation has converged.

Do not infer runtime cleanup from durable reservation state.

## Workstream B — Enforce the supported single-loop runtime

### Required configuration change

Make `server.threads=1` the only valid production configuration.

Preferred implementation:

- retain the existing field for configuration compatibility;
- validate equality to `1` and raise `ConfigError`/Pydantic validation error for any other value;
- remove the runtime warning path because invalid configuration must not start;
- update `config.example.toml`, `config.sbc.example.toml`, deployment docs, and `AGENTS.md` to state that one event-loop thread is required.

Do not add a `multi_loop_experimental` flag or maintain lock-rebinding as a supported architecture.

Audit Granian startup argument construction and confirm it always receives one thread after validated configuration.

### Ephemeral port handling

Change the production configuration constraint to ports `1..65535` unless repository tests or an internal app-construction helper demonstrably require `port=0`.

If tests require an ephemeral socket:

- keep that behavior in a test/application helper that bypasses file-backed production configuration;
- do not add a new public configuration flag merely for tests.

## Workstream C — Replace `repr()` terminal conflict detection

### Required design

Add one bounded semantic comparison representation for duplicate terminal registration. It may be:

- a frozen dataclass;
- a tuple returned by a helper;
- a stable digest over an explicit tuple.

It must include only fields that change terminal meaning. At minimum inspect and decide explicitly for:

- outcome;
- status/error class;
- durable request/attempt identity;
- handoff state;
- usage/cost payload identity where replaying different values would be unsafe;
- failure-effects identity;
- runtime-lease acquisition facts.

It must exclude incidental mutable diagnostics, object addresses, task state, callbacks, exception representation, and unbounded strings.

Requirements:

1. first registration stores the semantic key;
2. an identical duplicate joins the existing job;
3. a meaningfully different duplicate raises `TerminalConflictError` before mutation;
4. comparisons do not serialize request bodies, headers, credentials, or traceback text;
5. no JSON schema or generalized idempotency framework is introduced.

Update tests that currently depend on representation equality.

## Workstream D — Redact candidate cleanup diagnostics

### Required behavior

`RuntimeGenerationCandidate.abort()` must not retain or log raw `str(cause)`, `repr(exc)`, provider URLs, credentials, request content, environment values, or arbitrary close-callback text.

Use the existing central redaction behavior where possible. Add one small helper only if the current helper is request-error-specific.

The helper must:

- accept an arbitrary object/exception;
- redact secret-shaped strings and credentials;
- limit output length to a documented bound;
- preserve only useful exception class and bounded stage information;
- return deterministic safe text for diagnostics and logs.

Apply it to:

- `primary_failure`;
- `close_errors`;
- warning logs emitted during abort.

`close_errors_by_type` should continue storing exception class names only.

Do not create a new logging framework.

## Workstream E — Bound forwarded-header trust

### Configuration

Add one small security setting using the narrowest viable shape, preferably:

```toml
[security]
trusted_proxies = ["127.0.0.1", "::1"]
```

An empty list means forwarded headers are ignored.

Do not implement CIDR parsing unless an existing dependency/helper already provides it and a shipped deployment needs it. Exact peer IP matching is sufficient for the intended local deployment.

### Request behavior

`get_client_ip()` must:

1. read the immediate ASGI peer address;
2. honor `X-Forwarded-For`/`X-Real-IP` only when that peer is in `trusted_proxies`;
3. otherwise return the immediate peer address;
4. take only the first bounded forwarded value;
5. reject/ignore empty, control-character, or implausibly long values;
6. never fail the proxy request solely because attribution headers are malformed.

This is attribution hygiene, not an authentication mechanism.

## Workstream F — Documentation reconciliation

Search the repository for statements equivalent to:

- no retry after first downstream byte;
- handoff immediately before first stream delivery;
- zero bytes means pre-handoff.

Update authoritative documentation to state:

- streaming handoff occurs at ASGI `http.response.start`;
- an empty started stream is post-handoff;
- `bytes_emitted` is payload accounting only;
- no retry occurs after response start.

Do not rewrite historical completed plans unless they are presented as current architecture. Update `AGENTS.md`, request-lifecycle architecture, and active operator documentation.

## Focused verification

Extend existing tests; do not create a new harness.

Required cases:

1. acquired active count with missing router leaves the lease unreleased;
2. acquired quota reservation with missing estimator leaves the lease unreleased;
3. acquired health probe with missing health manager leaves the lease unreleased;
4. partial successful release remains idempotent and retries only missing components;
5. final result remains incomplete while any acquired component is unresolved;
6. `threads=1` validates and `threads=2` fails before startup;
7. production config rejects port zero if the audit permits the change;
8. semantically identical terminal duplicates join;
9. semantically different duplicates conflict without mutation;
10. candidate abort diagnostics redact an API-key-shaped exception and enforce the length bound;
11. forwarded headers from an untrusted peer are ignored;
12. forwarded headers from an explicitly trusted peer are honored;
13. malformed forwarded values fall back to the peer address.

Suggested commands:

```bash
uv run ruff format src/eggpool/request/finalization_job.py src/eggpool/models/config.py src/eggpool/runtime_manager.py src/eggpool/api/proxy_request.py src/eggpool/security tests/unit tests/integration
uv run ruff check src/eggpool/request/finalization_job.py src/eggpool/models/config.py src/eggpool/runtime_manager.py src/eggpool/api/proxy_request.py src/eggpool/security tests/unit tests/integration
uv run pyright src/eggpool/request/finalization_job.py src/eggpool/models/config.py src/eggpool/runtime_manager.py src/eggpool/api/proxy_request.py src/eggpool/security
uv run pytest <affected finalization/config/runtime/proxy tests> -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

## Acceptance criteria

- [x] Every acquired runtime component is included in the required convergence set.
- [x] A missing dependency produces an incomplete local release outcome and never successful convergence.
- [x] Completed components are not replayed on retry.
- [x] `server.threads` accepts only one.
- [x] Production startup cannot use port zero unless an explicit supported reason is documented.
- [x] Duplicate terminal comparison is semantic, bounded, and secret-free.
- [x] Candidate abort diagnostics and logs are redacted and length bounded.
- [x] Forwarded client attribution is honored only from configured trusted peers.
- [x] Current documentation consistently defines response handoff as ASGI response start.
- [x] Focused tests and the existing smoke gate pass.
- [x] No new supervisor, compatibility mode, framework, or CI job is added.

## Rejection conditions

Do not close this plan if:

- `released=True` is possible while an acquired component lacks a completed marker;
- missing dependencies are ignored;
- multi-thread mode merely emits another warning;
- terminal equality still depends on `repr()`, mutable diagnostics, or raw exception text;
- abort diagnostics can contain a supplied API key or full arbitrary exception string;
- forwarded headers remain globally trusted;
- tests require live providers, timing sleeps, or a new harness.

## Implementation sequence for GPT-5.6 Luna

1. Read the named files and map all result-projection call sites.
2. Correct lease requirement calculation and add focused tests.
3. Restrict runtime threads and resolve port-zero behavior.
4. Introduce the smallest semantic terminal key and migrate conflict tests.
5. Centralize bounded candidate-error redaction.
6. Add exact trusted-proxy attribution behavior.
7. Reconcile current documentation.
8. Run focused checks, then smoke.
9. Mark this plan complete only with the exact commands and outcomes recorded.

## Closure verification (2026-08-06)

The closure pass reran the shared ownership/configuration/reload/database
focused suite (the exact command and 227-test result are recorded in Plan 085),
both shipped `check-config` commands, and the final CI-equivalent gate. The
documentation sweep also removed the remaining current-profile wording that
described `threads > 1` as a warning-only mode.
