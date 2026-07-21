# Phase 5 — Shared Runtime-Generation Factory

Date: 2026-07-19
Status: complete
Roadmap: `plans/001-reload-correctness-performance-roadmap.md`
Prerequisites: Phases 1–4.

## Objective

Eliminate behavior drift between startup-created and reload-created runtimes by extracting one authoritative production factory for the complete generation-owned service graph.

Startup may still perform database initialization, crash recovery, initial synchronization, and process-service startup around the factory. It must not maintain a second coordinator, health, metrics, or provider graph with different dependency wiring.

## Defects this phase must close

The current construction paths differ in several material ways:

- startup can supply the dispatch writer while reload candidate construction omits it;
- startup supplies local pre-upstream and stream-diagnostic dependencies that reload may omit;
- startup applies configured detailed-span sampling while reload can revert to the recorder default;
- startup hydrates persisted backoffs into the health manager while reload can construct a fresh unsuppressed manager;
- future service additions can land in one path and silently be absent from the other.

These are correctness and performance defects, not merely duplication.

## Ownership boundary

Before extraction, update the ownership inventory from Phase 4.

### Process-owned inputs

The factory should receive, not construct or close:

- database/repositories or a repository bundle;
- shared dispatch writer;
- shared routing-trace writer;
- process-wide metrics/coalescers;
- stream diagnostics if process-owned;
- process supervisor interface used to create generation task plans;
- clock and random/test dependencies;
- immutable application facilities.

### Generation-owned outputs

The factory should construct and return:

- provider/account registry;
- model catalog and provider model views;
- health manager and hydrated backoff state;
- routing policy/router and quota estimator;
- provider client pool and outbound manager;
- DNS backend where generation-specific;
- cost calculator and pricing view;
- request coordinator;
- generation-local recorders and stats services;
- generation task supervisor/specification;
- immutable runtime configuration snapshot;
- closeable generation resource bundle.

## Factory interface

Introduce a typed API, for example:

```python
class RuntimeGenerationFactory:
    async def prepare(
        self,
        *,
        config: AppConfig,
        config_digest: str,
        generation_id: int,
        process: ProcessRuntime,
        candidate: RuntimeGenerationCandidate,
    ) -> PreparedRuntimeGeneration:
        ...
```

The exact type names may follow repository conventions. Required properties:

- all closeable outputs register with the candidate owner from Phase 4;
- no active runtime pointer or process-owned configuration is mutated;
- all configuration values are explicit inputs;
- the result is complete and publication-ready;
- startup and reload call the same method.

## Startup-only orchestration

Keep these outside the shared generation factory unless current architecture clearly makes them generation-owned:

- opening SQLite and applying migrations;
- recovery of interrupted requests/reservations;
- initial provider/account persistence bootstrap;
- initial catalog refresh that is intentionally remote;
- process-level update checker and maintenance workers;
- control socket startup;
- dispatch writer worker startup;
- routing-trace writer process worker startup;
- initial readiness/writable probe worker.

Startup sequence should become:

1. build process runtime;
2. complete bootstrap/recovery;
3. create a candidate owner;
4. call the shared generation factory;
5. publish as initial generation;
6. transfer ownership;
7. start serving.

## Dependency parity requirements

The factory must wire all coordinator dependencies identically for startup and rehash:

- dispatch writer and enabled-selection policy;
- local pre-upstream recorder;
- dispatch overhead and detailed span recorders;
- configured `detailed_span_sample_rate`;
- stream diagnostics;
- routing-trace writer or guard;
- cost calculator;
- repositories and finalization queue;
- provider clients and DNS/outbound configuration;
- immutable config snapshot.

Do not rely on constructor defaults for production settings that are present in configuration. Pass them explicitly.

## Persisted health/backoff hydration

Before publication, load persisted account/model suppression state into the candidate health manager.

Requirements:

- use batched repository reads where available;
- preserve expiry timestamps and reason classes;
- discard expired records consistently with startup policy;
- do not mutate the active health manager;
- failure follows a documented policy: reject candidate for required state, or continue only if startup already treats it as optional;
- tests prove a suppressed account remains ineligible immediately after rehash.

## Configuration snapshot

Attach an immutable, validated configuration snapshot and digest to every generation. Avoid generation services reading mutable `app.state.config` after construction.

The snapshot should support:

- active config diagnostics;
- routing and metrics behavior;
- comparison during reload;
- safe handoff to dashboard/readiness consumers in Phase 7.

## Construction contract tests

Create a test that builds equivalent generations through:

- initial startup orchestration;
- reload candidate orchestration.

Compare a normalized service-graph manifest containing:

- generation field names;
- dependency types;
- process-owned object identity where sharing is intended;
- configured values such as sample rates and writer selection;
- resource ownership registrations;
- health/backoff entries;
- task specifications.

The manifest should fail when a future dependency is added to one path only.

## Additional tests

- Dispatch writer enabled/disabled parity across startup and reload.
- Detailed-span sample rate remains configured after multiple reloads.
- Local pre-upstream recorder receives samples after reload.
- Stream diagnostics remain shared/available after reload.
- Persisted account and model backoffs survive reload.
- Candidate construction failure still triggers Phase 4 cleanup.
- Startup failure during factory construction closes candidate resources.
- No remote refresh is unintentionally duplicated during reload.
- Repeated no-op reload does not rebuild a generation.

## Implementation sequence

1. Produce a startup-vs-reload construction diff and dependency inventory.
2. Define process-runtime inputs and generation outputs.
3. Introduce the factory without changing startup behavior.
4. Move startup construction into the factory in small commits.
5. Migrate reload candidate construction to the same factory.
6. Add persisted backoff hydration as a factory preparation step.
7. Add explicit configuration parameters currently relying on defaults.
8. Remove duplicate construction code.
9. Add service-graph manifest and parity tests.
10. Run full startup, reload, routing, metrics, and streaming suites.

## Acceptance criteria

- Startup and reload invoke one production runtime-generation factory.
- No duplicate coordinator/service-graph construction remains in `app.py` and `reload_manager.py`.
- Coordinator dependency wiring is identical across paths.
- Configured detailed-span sampling survives reload.
- Dispatch writer, local recorder, routing trace, and stream diagnostics are present consistently.
- Persisted account/model backoffs are hydrated before publication.
- Candidate resources remain owned and cleaned by Phase 4 mechanisms.
- A structural parity test fails if future construction paths diverge.
- No configuration-dependent production behavior relies on unintended constructor defaults.

## Handoff evidence

**Service-graph manifest**: `test_construction_parity_manifest` in `tests/unit/test_generation_factory.py` compares normalized manifests between startup and reload paths, failing if future dependencies are added to one path only.

**Startup/reload parity test commands**:
```bash
uv run pytest tests/unit/test_generation_factory.py -v
```
14 tests covering: service-graph manifest, dispatch writer, detailed span sample rate, local pre-upstream recorder, stream diagnostics, backoff hydration, candidate cleanup, no-op reload, and no remote refresh.

**Before/after constructor call sites**:
- `src/eggpool/app.py`: Startup now calls `factory.prepare()` (line ~945) instead of inline construction. Process-owned services (MetricsWriteCoalescer, DispatchPersistenceWriter, RoutingTraceWriter) created before factory call.
- `src/eggpool/control/reload_manager.py`: `_build_candidate_generation()` replaced with single `factory.prepare()` call (line ~1009). ~400 lines of duplicated service construction removed.

**Backoff survival test**: `test_backoff_hydration` verifies persisted account/model backoffs are loaded into the health manager during factory preparation, ensuring suppressed accounts remain ineligible after rehash.

**Process-owned workers not recreated**: Factory accepts `ProcessRuntime` as input and does not construct MetricsWriteCoalescer, DispatchPersistenceWriter, or RoutingTraceWriter. These remain process-owned and survive generation swaps.