---
name: development
description: Development workflow for the EggPool project. Use when running linters, type checkers, tests, or pre-commit validation. Covers ruff, pyright, pytest, and the full pre-commit check sequence.
---

# Development Workflow

## Local Development Loop

Fast focused iteration — run only what you changed:

```bash
uv run ruff format <changed paths>
uv run ruff check <changed paths>
uv run pytest <affected test paths> -q --tb=short --maxfail=1
```

## Before-Push Check

Install the CI-only tool set and run the same checks as the CI job:

```bash
uv sync --frozen --extra ci
```

Then run:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

## CI

One GitHub Actions job on every PR:

| Job | Python | What it does |
|-----|--------|-------------|
| `check` | 3.11 | ruff format + ruff check + pyright + `pytest tests/smoke/` |

CI sets `PYTHONHASHSEED=0` and `TZ=UTC`; reproduce locally for deterministic results.
The workflow skips changes limited to plans, non-packaged documentation, and
agent guidance; package, configuration, script, deployment, lockfile, and
workflow changes still run the gate.

## Focused Verification

Catalog/storage changes should run the catalog, ping, reconciliation, provider-aware,
unresolved-model, and migration compatibility suites before the smoke gate. The
focused assertions should inspect durable rows/application DML effects, not
SQLite page-write counts.

Analytics index changes should run the attempt-stats and migration compatibility
tests. Query-plan assertions should cover only the critical retained filtered
index shape; do not turn workstation timing or page counts into CI thresholds.

```bash
# Single test file
uv run pytest tests/unit/test_contract.py -v

# Single test by name
uv run pytest -k "test_routing_plan_fallback" -v

# Integration tests only
uv run pytest -m integration -v

# Network-dependent tests
uv run pytest -m network -v

# Lint auto-fix
uv run ruff check --fix src/
```

## Linting

- **Ruff** for linting and formatting
- Rules: E, F, W, I, N, UP, B, A, SIM, TCH
- Line length: 88 characters
- Target: Python 3.11+

```bash
# Check formatting
uv run ruff format --check src/ tests/ scripts/

# Auto-fix formatting
uv run ruff format src/ tests/ scripts/

# Check lint
uv run ruff check src/ tests/ scripts/

# Auto-fix lint
uv run ruff check --fix src/ tests/ scripts/
```

## Type Checking

- **Pyright** in strict mode
- Covers `src/` AND `scripts/`
- Use `cast` or `Any` rather than excluding files

```bash
uv run pyright src/ scripts/
```

## Testing

- **pytest** with pytest-asyncio (strict mode), `xfail_strict = true`, `--strict-markers`
- **respx** for HTTPX upstream mocking
- Tests in `tests/unit/`, `tests/integration/`, `tests/smoke/`, `tests/perf/`, `tests/live/`, `tests/contract/`

### Test Markers

Defined in `pyproject.toml`:

- **`slow`** — marks tests as slow (deselect with `-m "not slow"`)
- **`performance`** — manually invoked real-runtime performance checks
- **`live`** — opt-in live provider/network verification tests
- **`network`** — tests requiring network access or external services
- **`integration`** — integration tests requiring full component wiring
- **`unit`** — unit tests

### Provider Contract Tests

```bash
uv run pytest tests/unit/test_contract.py tests/unit/test_contract_urls.py -v
```

### Smoke Suite

```bash
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

The smoke suite covers: package import, config parsing, invalid config rejection, check-config validation, DB migration, one non-stream request, one streaming request, one upstream failure followed by recovery, one premature EOF, one Anthropic request, and CLI help. Broader tests are selected by changed ownership boundary, not by a mandatory full-suite or soak ceremony.

Retained tests should be organized around capability contracts. Historical
release matrices and repeated rehash soak tests are not routine corpus; use the
focused reload integration suites for lifecycle coverage and the performance
suite only as a manually invoked diagnostic.

Completed phase/closure matrices are historical maintenance surface, not a
required preflight for ordinary changes. Prefer the current capability-named
tests and the smallest seam that proves the invariant; do not add plan-numbered
test files or permanent benchmark baselines.

Cache/compression tests should stay focused on segmentation, safe transforms,
native/provider-bound cache boundaries, routing isolation, and privacy. The
fixture sanitization check is retained, but broad replay matrices and duplicate
fixture replays are not routine corpus.

Provider-cache dialect changes should also run the focused OpenAI→Anthropic,
Anthropic→OpenAI, capability-override, loss-policy, and privacy/redaction
tests. Verify that generic OpenAI Chat Completions-compatible targets receive
no explicit breakpoint fields without a provider/model contract, that a
compatible-extension contract remains distinct from first-party semantics,
that TTL loss metadata uses the selected contract. Synthetic cache insertion is
not part of the runtime surface.

Performance tests under `tests/perf/` remain manually invoked diagnostics. They
must not become CI gates or imply universal timing/resource thresholds; compare
fixed request shapes and report local proxy timing separately from upstream
latency.

Streaming completion regressions should include both the pure EOF decision table
and the real response path. Verify that canonical `[DONE]`/`message_stop` streams
complete, markerless payload streams record incomplete EOF, and transcoded streams
do not receive a synthetic terminal marker after premature EOF.

Streaming handoff regressions should invoke the proxy streaming response through a
direct ASGI send collector. `downstream_started` is marked at
`http.response.start`, before the first body iteration; an empty started stream
therefore remains post-handoff with zero emitted bytes.

Database fixtures are bound to the canonical asyncio event loop. Fixtures that
connect a database must yield it from a `try/finally` block and await
`disconnect()` on every teardown path. Tests that exercise an ASGI app with a
migrated database should use an async ASGI client on that loop; do not reuse the
database through synchronous `TestClient` or rebind its lock for convenience.
When investigating worker-thread teardown warnings, promote the exact
`PytestUnhandledThreadExceptionWarning` to an error in the focused suite rather
than adding a repository-wide suppression.

Dispatch-boundary regressions should cover distinct-account failover, the
configured attempt ceiling, cleanup-before-reselection, local request
construction failures without provider health changes, response adaptation
before durable success, native invalid-JSON pass-through, and cancellation
propagation. Run the focused coordinator/proxy/transcoder suites before the
repository-wide CI gate.

Prepared-transcode ownership changes should also run the prepared-transcode,
provider-bound request, transform-pipeline, thinking-budget, cache-translation,
and retry/freeze focused suites. Verify unchanged reuse adopts the translated
generation without a second encode or recursive ownership walk, while
provider-specific mutations leave the prepared source unchanged. Cross-protocol
recompute should pass the provider payload as a read-only `Mapping`, adopt the
fresh translated graph directly, and prove both encoder directions leave the
source message/tool graph unchanged. Media tests should cover strict invalid
base64, URL sources, size boundaries, and obvious encoded-size rejection before
decode; do not add permanent memory thresholds.

Request-estimation changes should run the request-limit, proxy-admission,
prepared-transcode/tool-padding, and body-limit suites. Verify that an
enforced canonical context estimate is counted once and carried into
`ProxyRequestContext`, that unbounded models do not perform an unnecessary
decoded-payload walk, and that translated tool padding uses the shared
structural estimator without serializing each tool. The bounded body reader
and full incoming-header snapshot are intentional: they preserve oversized
body draining/keep-alive behavior and downstream forwarding/finalization
contracts, respectively.

Database ownership changes should run the focused transaction and fault-matrix suites; verify child-task ownership rejection and rollback-failure invalidation without exposing a public rollback helper or production test-injection seam. Patch private callable boundaries from test support when deterministic failure injection is needed.

Finalization round-trip changes should run the request-finalizer, repository,
attempt, reservation, transaction-failure, and retained-terminal-owner suites.
The first-finalization regression should assert application-level convergence
reads, while duplicate and partial-convergence cases must retain their focused
fallback reads.
Also verify that first-attempt timestamp persistence does not issue a separate
parent UPDATE and that only terminal request finalization writes
`last_attempt_id`.

## Planning proportionality

Use a detailed roadmap with child plans when work crosses architectural
boundaries, has ordering/dependencies across phases, risks durable or
request/process ownership state, or redesigns broad protocol/provider
semantics. Use one focused plan for a bounded multi-file corrective pass. A
small deterministic fix local to one or a few helpers may use a direct issue or
concise implementation notes when existing tests and gates protect the seam.
Completing a roadmap does not by itself require a new closure plan; record
closure evidence in the implementing plan unless a genuinely new phase or
defect is discovered.

## Code Style

- Python 3.11+ with `from __future__ import annotations` in all files
- Type hints on all function signatures and return types
- Use `NoReturn` for functions that never return (e.g., `sys.exit`)
- Move type-only imports into `TYPE_CHECKING` blocks
- Follow ruff TCH rules for import organization

## Error Handling

- Use the exception hierarchy in `errors.py`
- Chain exceptions with `raise ... from err` or `raise ... from None`
- Config errors: `ConfigError`
- Database errors: `DatabaseError`
- Upstream errors: `UpstreamError` and subclasses (`AuthenticationError`, `QuotaExhaustedError`, `RateLimitError`, `ModelUnavailableError`)
- Proxy errors: `ProxyError`
- Protocol errors: `ModelNotFoundError`, `NoEligibleAccountError`, `CatalogUnavailableError`, `AuthenticationUnavailableError`, `UpstreamExhaustedError`, `AccountSuspendedError`
- Request errors: `RequestTooLargeError`, `ContextLimitExceededError`

## Git Workflow

- Branch: `main`
- Commit messages: concise, imperative mood
- Never commit secrets, API keys, or `.env` files
