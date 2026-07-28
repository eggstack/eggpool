# Real Eggpool Runtime Test Harness

Date: 2026-07-28
Status: implementation handoff

Parent roadmap:

- `plans/031-upstream-hardening-corrective-roadmap.md`

Implementation baseline:

- `cb7407b2114eb8aab5bc536d5b1e3b200afcaa56`

May run in parallel with:

- `plans/032-opencode-minimax-provider-contract-correction.md`

## Objective

Build one reusable, deterministic test harness that sends requests through the actual Eggpool ASGI proxy path and exposes structured snapshots of durable and runtime state.

The harness must replace the false “end-to-end” pattern in which tests send `httpx` requests directly to `MockUpstream`. Direct mock-upstream tests remain useful as helper tests, but they cannot prove routing, provider selection, request persistence, reservation ownership, health effects, quarantine, finalization, or database recovery.

This phase owns test infrastructure only. It must not attempt to close every correctness scenario; Plan 034 consumes the harness for those scenarios.

## Scope

### In scope

- Temporary configuration generation.
- Temporary SQLite database creation and migrations.
- Actual Eggpool application/runtime construction.
- Real account registry, router, catalog cache, health manager, quota estimator, finalization supervisor, database recovery controller, and provider client pool.
- ASGI requests entering Eggpool's authenticated proxy endpoint.
- Mock upstream responses behind provider clients.
- Deterministic provider/account/model seeding.
- Structured state snapshots and diffs.
- Deterministic lifecycle startup and shutdown.
- Narrow harness self-tests.

### Out of scope

- Fixing provider-contract matching; Plan 032 owns it.
- Completing the provider payload pipeline; Plan 035 owns it.
- Long-duration soak loops; Plan 037 owns them.
- Performance thresholds; Plan 036 owns them.
- New production endpoints.
- A second application factory used only by tests if the existing factory can be parameterized safely.
- Live network/provider credentials.

## Required harness architecture

Create a test support module, preferably:

- `tests/support/eggpool_runtime_harness.py`

Supporting modules may be added only when one file would become unmaintainable. Keep the public test API small.

Recommended primary interface:

```python
@dataclass(slots=True)
class EggpoolRuntimeHarness:
    config: AppConfig
    app: Any
    client: httpx.AsyncClient
    database: Database
    runtime: ProcessRuntime
    upstreams: dict[str, MockUpstream]

    @classmethod
    async def create(cls, scenario: HarnessScenario) -> "EggpoolRuntimeHarness": ...

    async def request_openai(self, payload: dict[str, Any], **kwargs: Any) -> httpx.Response: ...
    async def request_anthropic(self, payload: dict[str, Any], **kwargs: Any) -> httpx.Response: ...
    async def snapshot(self) -> RuntimeStateSnapshot: ...
    async def close(self) -> None: ...
```

The exact type names may differ. Tests must not reach into dozens of private fields to use the harness.

## Workstream A — Scenario declaration

Define an immutable scenario declaration that describes:

- providers;
- provider IDs and kinds;
- base URLs and protocol paths;
- accounts and API keys;
- model IDs and provider-model mappings;
- thinking capability metadata;
- upstream response rules;
- server API key;
- optional dispatch-writer profile;
- optional database recovery settings.

Example shape:

```python
@dataclass(frozen=True, slots=True)
class HarnessProvider:
    provider_id: str
    kind: str
    base_url: str
    openai_path: str
    anthropic_path: str

@dataclass(frozen=True, slots=True)
class HarnessAccount:
    name: str
    provider_id: str
    api_key: str
    models: tuple[str, ...]
```

Use existing production configuration models to validate generated configuration. Do not bypass `AppConfig` validation with hand-built mocks.

## Workstream B — Temporary database lifecycle

For every harness instance:

1. Create a unique temporary directory.
2. Create a new SQLite database file.
3. Run the same migration path used by production startup.
4. Seed only the minimum account/provider/model data through production repositories/services.
5. Start process-owned database recovery infrastructure.
6. Close all database connections and background tasks at teardown.
7. Assert the temporary directory can be removed, proving no open file handle remains.

Do not copy a prebuilt database fixture. The migration path itself is part of the runtime contract.

Expose the database path for fault-injection tests, but keep direct SQL out of ordinary scenario tests.

## Workstream C — Actual application startup

Construct the real application and runtime using the repository's production factory/startup code.

Requirements:

- use the same `ProcessRuntime`/generation factory path as `eggpool serve` where practical;
- execute startup hooks or their explicit production equivalents;
- attach process-owned background services exactly once;
- use `httpx.ASGITransport` or an equivalent in-process ASGI client;
- send through `/v1/chat/completions` and `/v1/messages` rather than calling the coordinator directly;
- authenticate through the normal server API-key middleware;
- verify `/readyz` before sending requests;
- execute application shutdown hooks and then explicit harness cleanup.

If the current app factory cannot accept injected provider transports, add a narrow test seam at the provider-client factory boundary. Do not add a broad “test mode” that changes routing or finalization behavior.

## Workstream D — Mock upstream integration

Reuse `tests/helpers/mock_upstream.py` where possible, but route production provider clients to it.

Acceptable mechanisms:

- `respx` matching the real provider URL built by `compose_provider_url`;
- an in-process local ASGI upstream with a custom `httpx` transport injected into `ProviderClientPool`;
- a local ephemeral HTTP server when transport behavior itself must be exercised.

The default harness should remain fast and network-free. It must capture:

- provider ID/account selected;
- URL and protocol path;
- authorization-header fingerprint, never raw key in evidence;
- exact request body bytes;
- normalized parsed request fields;
- request sequence;
- streaming/non-streaming mode;
- response rule selected.

Provider A and Provider B must be distinguishable so unrelated-provider isolation can be proved.

## Workstream E — Structured runtime state snapshot

Create a typed snapshot, extending the useful Plan 023 state-audit concepts but reading actual runtime/repositories.

Required fields:

### Durable state

- request row count by status;
- attempt row count by status/error class;
- pending request IDs;
- pending attempt IDs;
- active reservation IDs and totals;
- account backoff rows;
- model quarantine rows/state;
- terminal model-unavailable state where applicable;
- database lifecycle state and connection epoch;
- pending ambiguous database operations.

### Runtime state

- router active request counts by account;
- quota reservations by account;
- health/circuit state by account/model;
- acquired half-open/probe slots;
- in-memory quarantine entries;
- finalization supervisor active jobs and terminal-history size;
- database recovery state/attempt count;
- dispatch-writer queue depth and active drain state;
- background task count by named service where exposed.

### Process/resource state

- asyncio task count, with harness-owned tasks identifiable;
- thread count;
- file descriptor count where supported;
- RSS when `psutil` is available;
- recorder sample lengths.

Provide a deterministic `diff(before, after)` that reports only changed fields. Tests should assert named invariants rather than comparing a giant serialized object blindly.

## Workstream F — Deterministic barriers

Expose test barriers for high-risk lifecycle points by reusing existing cancellation/fault seams:

- after selection before persistence;
- after persistence before runtime publication;
- after runtime publication before upstream send;
- after response headers;
- after first stream chunk;
- before finalization transaction;
- after finalization commit before runtime release;
- during database recovery replacement;
- before readiness restoration.

The harness must provide an event-based API such as:

```python
barrier = harness.barriers.pause("after_finalization_commit")
await barrier.reached.wait()
barrier.release.set()
```

Do not use arbitrary sleeps to coordinate correctness tests.

## Workstream G — Test cleanup guarantees

`close()` must be idempotent. It must:

1. Close the ASGI client.
2. Drain or cancel request tasks owned by the test.
3. Drain the finalization supervisor with a bounded timeout.
4. Stop dispatch/routing/metrics writers.
5. Stop database recovery controller.
6. Run application shutdown.
7. Close provider clients.
8. Close SQLite.
9. Assert no harness-owned task remains.
10. Remove temporary files.

If cleanup fails, surface all remaining resource identities in the assertion rather than swallowing exceptions.

## Required harness self-tests

Create:

- `tests/unit/test_plan_033_runtime_harness_lifecycle.py`
- `tests/integration/test_plan_033_runtime_harness_proxy_path.py`
- `tests/integration/test_plan_033_runtime_harness_state_snapshot.py`

### Lifecycle tests

- create and close one harness;
- close twice safely;
- create/close ten sequential harnesses;
- two harnesses can exist concurrently without sharing DB, ports, registries, or provider rules;
- startup failure cleans partial resources;
- teardown failure reports retained resources.

### Proxy-path tests

- OpenAI request enters Eggpool, selects configured provider/account, reaches the correct mock upstream, persists/finalizes, and returns response;
- Anthropic request follows the analogous path;
- streaming request emits at least two chunks through Eggpool;
- authorization middleware rejects missing client API key before upstream;
- upstream sees provider account credential while the client never receives it;
- `/readyz` reflects runtime readiness.

### Snapshot tests

- baseline has no pending request/attempt/reservation;
- a paused in-flight request appears in active and pending fields;
- after completion, pending/active fields return to baseline;
- a known health mutation appears in the diff;
- snapshot redacts credentials and request content;
- database epoch and recovery state are visible.

## Required fixture ergonomics

Provide pytest fixtures with explicit scopes, for example:

```python
@pytest.fixture
async def eggpool_runtime(tmp_path: Path) -> AsyncIterator[EggpoolRuntimeHarness]:
    harness = await EggpoolRuntimeHarness.create(default_scenario(tmp_path))
    try:
        yield harness
    finally:
        await harness.close()
```

Avoid session-scoped mutable runtime fixtures. Every correctness test should receive isolated state unless it explicitly tests multi-request behavior within one harness instance.

## Files expected to change

Primary:

- `tests/support/eggpool_runtime_harness.py`
- `tests/helpers/mock_upstream.py` only for reusable capture/transport support
- focused Plan 033 tests

Possible narrow production seams:

- provider client-pool factory or generation factory injection point;
- app factory parameter used solely to supply an `httpx` transport/client factory;

Any production seam must preserve production defaults and be covered by a test proving no behavior change when omitted.

Do not modify provider contract policy, failure classification, or finalization semantics in this plan.

## Implementation steps

1. Inventory existing app/generation test fixtures and reuse rather than duplicating.
2. Define immutable scenario types.
3. Implement temporary config and database creation.
4. Build the actual application/runtime through production construction paths.
5. Add provider transport injection at the narrowest boundary if required.
6. Implement ASGI client helpers.
7. Implement typed state snapshots and diffs.
8. Connect existing deterministic cancellation/database fault seams.
9. Implement idempotent teardown and leak reporting.
10. Add lifecycle self-tests.
11. Add OpenAI/Anthropic/stream proxy-path self-tests.
12. Add snapshot self-tests.
13. Run focused and existing application lifecycle suites.
14. Record evidence in `artifacts/plan-033-evidence.md`.

## Focused verification commands

```bash
uv run pytest \
  tests/unit/test_plan_033_runtime_harness_lifecycle.py \
  tests/integration/test_plan_033_runtime_harness_proxy_path.py \
  tests/integration/test_plan_033_runtime_harness_state_snapshot.py \
  tests/integration/test_proxy_integration.py \
  tests/integration/test_coordinator_lifecycle.py \
  -q --tb=short

uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run pyright src/ tests/support/
```

## Acceptance criteria

### Runtime fidelity

- [ ] Requests enter the real Eggpool ASGI proxy endpoint.
- [ ] The production router, coordinator, repositories, finalizer, health manager, and provider clients participate.
- [ ] A real migrated temporary SQLite database is used.
- [ ] Provider upstream requests are intercepted behind the production client boundary.
- [ ] Startup and shutdown use production lifecycle paths.

### State visibility

- [ ] Snapshot includes all required durable/runtime ownership fields.
- [ ] In-flight and completed state transitions are observable.
- [ ] Snapshot diffs are deterministic.
- [ ] Credentials and request content are redacted.
- [ ] Database lifecycle and recovery state are visible.

### Determinism

- [ ] Correctness barriers use events, not timing sleeps.
- [ ] Two concurrent harnesses do not share mutable state.
- [ ] Repeated create/close cycles leave no harness-owned tasks or open database files.
- [ ] Teardown is idempotent and reports leaks precisely.

### Compatibility and quality

- [ ] Production defaults are unchanged when test injection is absent.
- [ ] No alternate routing/finalization behavior exists in “test mode.”
- [ ] Focused tests pass on Python 3.11 and 3.12.
- [ ] Existing proxy/application lifecycle tests remain green.
- [ ] Ruff and Pyright are clean.
- [ ] `artifacts/plan-033-evidence.md` records exact implementation SHA and test results.

## Explicit rejection conditions

Do not mark this plan complete if:

- tests still send directly to `MockUpstream` and call that an Eggpool end-to-end path;
- the coordinator is invoked directly instead of through ASGI for proxy-path tests;
- the database is mocked or copied from a prebuilt fixture;
- state assertions infer ownership only from upstream request counts;
- arbitrary sleeps coordinate finalization or recovery;
- a global singleton leaks across harness instances;
- cleanup suppresses retained task/connection errors;
- a broad production “test mode” bypasses real routing, persistence, finalization, or health behavior.
