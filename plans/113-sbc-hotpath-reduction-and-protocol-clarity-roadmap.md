# Plan 113 — SBC Hot-Path Reduction and Protocol Clarity Roadmap

Date: 2026-08-11
Status: ready
Planning baseline: `6f4df9bd42b5ca336d3da5ef458ab1793e515185`

Implementation plans:

- `plans/114-provider-payload-copy-on-write.md`
- `plans/115-prepared-transcode-ownership-reduction.md`
- `plans/116-request-estimation-and-ingress-efficiency.md`
- `plans/117-provider-cache-dialect-correctness.md`
- `plans/118-optional-runtime-surface-and-dependency-reduction.md`
- `plans/119-retained-test-and-planning-surface-reduction.md`
- `plans/120-sbc-characterization-and-roadmap-closure.md`

## Purpose

Perform one bounded, reductive follow-up after Roadmap 103 and its corrective closures.

EggPool already satisfies its primary product goal: a local/LAN provider/account router with OpenAI- and Anthropic-compatible endpoints, bounded upstream failure isolation, retry before downstream handoff, durable request/reservation/finalization state, live rehash, and SBC-oriented defaults. This roadmap does **not** redesign those systems.

The remaining high-value work is concentrated in four areas:

1. remove full-request Python object-graph copies and rematerialization from common streaming/transcode/compression paths;
2. avoid repeated O(request-size) estimation and serialization work during request admission/preflight;
3. distinguish first-party protocol semantics from provider-specific OpenAI-compatible cache extensions;
4. delete optional configuration/test/planning machinery whose maintenance cost exceeds demonstrated value for a local Raspberry Pi/SBC appliance.

The desired end state is a smaller and cheaper request hot path with fewer ownership states, fewer full-payload walks, clearer provider capability contracts, and less retained verification/configuration surface — without weakening the reliability work that fixed previously observed poison/restart/database-reset failures.

## Confirmed findings driving the roadmap

### 1. First OpenAI streaming mutation can copy the entire request graph

`ProviderBoundRequest.mutate_provider_payload()` deep-copies the canonical payload on the first mutation. The OpenAI streaming transform uses this path to inject or normalize `stream_options.include_usage`.

For a large coding-agent request, changing one small top-level field can therefore duplicate the full `messages`/`tools` graph. The transform currently also treats invocation as mutation even when the target field already has the desired value.

This undermines the intended native no-transform/original-byte fast path for ordinary OpenAI streaming requests.

### 2. Prepared transcode performs recursive freeze and later recursive ownership rematerialization

Preflight translation produces a translated object graph and encoded body. `PreparedTranscode` recursively converts the graph into mapping proxies/tuples. Reuse later calls `ProviderBoundRequest.set_provider_payload()`, which recursively rebuilds an ordinary mutable JSON graph.

This physical freeze/thaw/rematerialization is unnecessary for request-local data. Logical ownership and a narrow copy-on-write boundary can provide the same safety with less CPU, allocator churn, and peak RSS.

### 3. Safe compression copy-on-write is partly defeated at the provider-bound boundary

The safe compressor already uses path-level copy-on-write, preserving unchanged subtrees by reference. Passing its transformed graph through the current provider-bound ownership helper recursively rematerializes the graph, losing much of the intended allocation benefit.

### 4. Context/token estimation can walk the decoded payload more than once

Context-limit enforcement computes an O(payload-size) decoded-JSON token estimate. The request handler later computes the same estimate again for coordinator use. Large prompts/tool schemas therefore pay repeated recursive traversal before upstream dispatch.

Tool-padding estimation also serializes translated tool definitions individually even though a full translated body is already encoded during preflight.

### 5. Provider-native cache semantics need a dialect boundary

Current first-party OpenAI API documentation exposes automatic prompt caching plus fields such as `prompt_cache_key` and `prompt_cache_retention`. EggPool additionally supports content-level `prompt_cache_breakpoint` / `prompt_cache_options` semantics as though they were generic OpenAI protocol behavior.

Those explicit-breakpoint fields may be valid for specific OpenAI-compatible providers, but they must not be inferred from the protocol family alone. Anthropic's explicit `cache_control` semantics also have provider-specific placement/TTL behavior that should remain capability-gated.

Execution must re-verify current official provider documentation immediately before modifying mappings because these APIs change frequently.

### 6. Optional compression/cache/DNS/config surface remains larger than its demonstrated appliance value

Compression is default-off and therefore not currently a dominant runtime cost, but it retains observe/safe modes, multiple transforms, scoped policies, recommendation tuning, synthetic-cache interaction, segmentation, and reserved/future configuration semantics.

The custom DNS cache is also default-off while carrying positive/negative caching, stale fallback, singleflight, timeout and detailed diagnostics. It should survive only if a real deployment use case justifies its maintenance burden.

### 7. Core dependency and SQLite architecture are already appropriately small

Core runtime dependencies are FastAPI, Granian, HTTPX, aiosqlite, Pydantic, and Click. `orjson` and `pproxy` are optional. Replacing core frameworks would create more code and risk than likely SBC benefit.

SQLite's one primary aiosqlite connection, WAL, `synchronous=NORMAL`, task-owned transactions, explicit ambiguity/fail-closed behavior, and low-wear analytics buffering are protected by this roadmap.

The one small dependency question worth verifying is whether `granian[pname]` is required by EggPool. Do not remove it without dependency/runtime verification.

### 8. CI is already lean; the retained corpus and planning process remain disproportionate

Ordinary CI is one Python 3.11 job running Ruff format/lint, Pyright, and 14 smoke tests. Keep this shape.

The retained corpus still contains more than 8,000 tests after previous reduction. The repository has also accumulated repeated roadmap/closure/corrective-closure documents for small local fixes. This roadmap should reduce future verification/process burden without deleting the high-severity regressions that justified EggPool's reliability architecture.

## Governing constraints

1. Preserve EggPool's intended deployment model: single-process local/LAN appliance, usually Raspberry Pi/SBC class hardware.
2. Preserve bounded 1,800-second transient suppression/backoff, model/account failure isolation, typed failure effects, and retry only before downstream response handoff.
3. Preserve generation-owned finalization, claim compensation, crash reconciliation, rehash generation leases, and terminal ownership semantics.
4. Preserve one-thread Granian topology unless direct evidence proves otherwise.
5. Preserve SQLite WAL, `synchronous=NORMAL`, task-owned transactions, single primary aiosqlite worker/connection by default, fail-closed ambiguity semantics, and startup crash recovery.
6. Do not add PostgreSQL, Redis, an ORM, a durable queue, multiprocessing, a second service, native extensions, or a Rust rewrite.
7. Do not replace FastAPI, HTTPX/httpcore, Pydantic, Click, aiosqlite, or SQLite in this roadmap.
8. Do not add a new core runtime dependency.
9. Prefer ownership/API simplification over new cache/arena/object-pool abstractions.
10. Do not use weak references, custom allocators, object pools, persistent copy-on-write libraries, immutable collection libraries, or another JSON representation to solve request ownership.
11. A provider transform must never be able to mutate the canonical client payload.
12. A native no-transform request should preserve original client bytes when no provider-specific normalization actually changes the request.
13. Full graph copying is allowed only where a transform genuinely needs ownership of a broad mutable graph and no narrower copy-on-write path is practical; such cases must be explicit and rare.
14. Provider-native fields may be emitted only from an explicit provider/model capability or dialect contract; protocol name alone is insufficient when semantics are not first-party-standard.
15. Do not add OpenAI Responses API support as part of this roadmap. If a real intended client requires Responses later, plan it as a separate protocol surface rather than smuggling Responses-only semantics into Chat Completions.
16. Compression/cache/DNS simplification must not remove documented behavior that active configuration examples rely on without a clear validation/deprecation path.
17. Keep `config.sbc.example.toml` conservative and copyable.
18. Keep ordinary CI one job. Do not add coverage thresholds, matrices, benchmark jobs, soak jobs, hardware jobs, scheduled full-suite jobs, or release automation.
19. Full retained-suite execution remains optional/manual. Child plans run focused owning tests plus the ordinary gate.
20. Do not introduce permanent profiling/benchmark evidence files or a performance framework. Temporary local instrumentation is allowed during implementation.
21. Planning should become smaller after this roadmap, not larger. Do not create plan-numbered tests, evidence schemas, or a permanent optimization program.
22. Stop when explicit acceptance criteria are satisfied. Do not opportunistically refactor protected systems.

## Roadmap phases

### Plan 114 — Provider Payload Copy-on-Write

Remove the common streaming full-graph deepcopy and preserve the compressor's existing path-level COW. Introduce the smallest explicit provider-bound ownership APIs needed to distinguish trusted EggPool-owned transformed graphs from untrusted/shared input. Make mutation helpers truthfully report no-op versus changed state.

### Plan 115 — Prepared Transcode Ownership Reduction

Delete recursive physical freezing/rematerialization from request-local prepared transcodes. Keep one authoritative translated payload/body generation and establish safe copy-on-write only when later provider-specific transforms require mutation. Preserve retry reuse and source-payload immutability.

### Plan 116 — Request Estimation and Ingress Efficiency

Compute decoded context estimates once and reuse them through limit enforcement/coordinator admission. Replace per-tool JSON serialization used only for rough token padding with the shared structural estimator where semantically acceptable. Audit request body/header copies and remove only demonstrated redundant copies without introducing a custom ASGI body-buffer system.

### Plan 117 — Provider Cache Dialect Correctness

Re-verify current official OpenAI/Anthropic caching semantics and reclassify any non-standard explicit breakpoint fields as provider-specific capabilities/dialects. Ensure generic OpenAI-compatible providers never receive extension fields from protocol name alone. Keep Anthropic cache-control placement/TTL loss explicit. Correct stale hard-coded TTL labels or capability metadata.

### Plan 118 — Optional Runtime Surface and Dependency Reduction

Inventory compression recommendation tuning, synthetic cache controls, DNS cache, optional observability/config fields, and the `granian[pname]` extra. Delete/reject dormant or unused surfaces where evidence shows no active product value; keep useful deterministic safe compression and other proven features. No core framework replacement.

### Plan 119 — Retained Test and Planning Surface Reduction

After production surfaces settle, remove redundant implementation-detail tests and obsolete optional-feature permutations while protecting high-severity routing/stream/database/finalization/rehash/transcode regressions. Formalize a lightweight repository convention that small one-boundary corrective work does not require roadmap + closure-plan chains.

### Plan 120 — SBC Characterization and Roadmap Closure

Run aggregate correctness verification and one short Raspberry Pi/SBC characterization if an appropriate host/provider workload is available. Focus measurements on large streaming native requests and cross-protocol requests so the copy-reduction work is actually exercised. Record unavailable evidence as `not measured`; do not build a retained benchmark harness.

## Dependency order

```text
114 provider COW ---------+
                          +--> 115 prepared transcode ownership --+
116 estimation reuse -----+                                      |
                                                                 +--> 119 tests/process --> 120 closure
117 cache dialect -----------------------------------------------+
                                                                 |
118 optional surface/deps ---------------------------------------+
```

Plan 114 should precede Plan 115 because prepared transcode reuse should target the final provider-bound ownership API. Plan 116 can proceed in parallel conceptually but should land after/around 114 to avoid conflicting request-context edits. Plan 117 is independent from memory ownership. Plan 118 should follow the production hot-path/cache work so deletion decisions reflect the final supported surface. Plan 119 is last among production-independent changes so it deletes tests for the actual final architecture. Plan 120 closes the roadmap.

## Cross-phase invariants

- Canonical decoded client payload remains immutable by convention and unmodified in every supported path.
- Provider-side mutation cannot leak back into client/canonical state.
- Native no-op paths do not mark the provider payload mutated merely because a transform function ran.
- `payload_generation` changes only when provider-visible structural content changes.
- Cached provider bytes correspond exactly to the current generation.
- Retry reuses the already-frozen provider-visible generation and does not rerun transforms after dispatch freeze.
- Prepared transcode reuse does not require recursive immutable-copy + mutable-copy cycles.
- Compression keeps cache-protected/stable-prefix invariants and does not silently broaden mutation scope.
- Context-limit rejection behavior remains at least as conservative/correct as before; optimization may not skip enforcement.
- No local preparation/transcode failure penalizes a provider/account unless existing typed policy explicitly classifies provider-authoritative evidence.
- No generic compatible provider receives an unverified extension field.
- Database/finalization/rehash behavior is unchanged except for incidental compile/type fixes required by narrower request APIs.
- Optional disabled subsystems construct no new background work.
- CI remains one Python 3.11 format/lint/type/smoke job.

## Standard verification policy

Every child plan must run focused owning tests plus:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Do not make the full 8k+ retained suite a child-plan gate. A focused protected union is sufficient. Full-suite execution is optional/manual confidence evidence.

For Plans 114–116, temporary local instrumentation may count full-graph copy/encode/walk events in tests. Such instrumentation should be test-local or removed before closure; do not add permanent telemetry solely to prove the optimization.

For Plan 117, implementation must re-check official provider documentation on the execution date and record the relevant documented field names/semantics in the plan closure. Provider docs, not this roadmap's August 11 snapshot, are authoritative.

## Roadmap acceptance criteria

- [ ] Ordinary OpenAI streaming requests that already contain the desired `stream_options.include_usage` do not mutate provider payload state, bump generation, deep-copy the request graph, or force reserialization.
- [ ] When EggPool must inject/modify `stream_options`, only the root and affected path are copied; large unchanged `messages`/`tools` subtrees remain shared read-only with the canonical graph.
- [ ] Safe compression output can be adopted without recursively rematerializing all unchanged subtrees.
- [ ] Provider transforms cannot mutate the canonical client payload after the ownership simplification.
- [ ] Prepared transcode no longer recursively freezes the translated graph and later recursively rebuilds it solely to cross an ownership boundary.
- [ ] Prepared transcode still reuses the already encoded translated body when no later provider-specific mutation is needed.
- [ ] Later provider-specific mutation after prepared transcode uses a bounded copy-on-write/owned path and preserves retry/freeze semantics.
- [ ] Decoded context-input estimation is computed at most once per request generation unless a provider-visible transform changes the semantics requiring recomputation.
- [ ] Context-limit enforcement and reservation/admission behavior remain correct.
- [ ] Tool-token padding no longer serializes every tool independently if the shared structural estimator can provide equivalent guardrail behavior.
- [ ] Generic first-party OpenAI Chat Completions semantics are not conflated with provider-specific explicit cache-breakpoint extensions.
- [ ] `prompt_cache_key`/retention and any extension breakpoint fields have explicit capability/dialect treatment based on current official/provider documentation.
- [ ] Anthropic cache-control mapping remains capability-gated with explicit unrepresentable TTL/placement loss.
- [ ] No provider-native cache extension is emitted solely because an upstream is labeled `openai` or `anthropic` compatible.
- [ ] Compression/cache/DNS/observability configuration no longer accepts dormant/future behavior that production does not actually implement unless the retained behavior is explicitly documented as intentional.
- [ ] Any removal of custom DNS cache or recommendation tuning is evidence-based and leaves default/SBC behavior correct.
- [ ] `granian[pname]` is either verified necessary and retained, or removed in favor of plain Granian with install/startup verification.
- [ ] No core runtime dependency is added; no core framework is replaced.
- [ ] SQLite architecture and durability semantics remain unchanged.
- [ ] Retained tests are materially smaller/simpler around touched surfaces while all protected high-severity regressions survive.
- [ ] Ordinary CI remains one Python 3.11 Ruff/Pyright/smoke job.
- [ ] Repository planning guidance explicitly permits small corrective changes to use one focused plan/issue or direct implementation without mandatory roadmap/closure chains.
- [ ] Final SBC characterization exercises a large native streaming request and a cross-protocol request when suitable hardware/provider credentials are available, or records those measurements as `not measured` without extrapolation.
- [ ] No permanent benchmark/soak/hardware-CI framework is created.

## Rejection conditions

Do not close the roadmap if any of the following occurs:

- copy reduction introduces shared mutable aliases between client and provider payloads;
- generation/serialized-body caches can become stale after a mutation;
- retry reruns transforms or uses a different provider-visible body after dispatch freeze;
- a no-op transform still forces full graph ownership/serialization on the common path;
- token-estimate reuse weakens context-limit enforcement or uses stale estimates after meaningful transforms;
- generic OpenAI-compatible providers receive undocumented explicit cache-breakpoint fields without a provider/model capability;
- Responses-API semantics are silently added to Chat Completions instead of being treated as a distinct future protocol;
- optional subsystem simplification removes active documented behavior without config migration/rejection clarity;
- DNS/cache/compression simplification adds another abstraction framework to replace what it deletes;
- SQLite/provider-pool/finalization architecture is changed without direct evidence;
- CI grows full-suite/benchmark/coverage/hardware gates;
- test deletion removes all direct or stronger coverage for previously observed poison/restart/finalization/stream/database/rehash failures;
- SBC measurements are fabricated or converted into permanent thresholds.

## Handoff execution protocol

For every child plan:

1. Read this roadmap, the child plan, `AGENTS.md`, and the directly affected production modules before editing.
2. Re-check `main` for changes since the planning baseline and adapt only where necessary; do not blindly restore baseline implementation shapes.
3. Use `rg` to locate all callers before changing an ownership/config/capability helper.
4. Prefer deleting work/state over adding a generalized optimization abstraction.
5. Add or retain tests for externally meaningful ownership/protocol behavior, not private physical representation.
6. Run the smallest focused tests while iterating, then the ordinary gate.
7. Record implementation commit SHA, exact focused verification, and acceptance status in the child plan closure.
8. One implementation commit per child plan is preferred where practical.
9. If a plan discovers that the reviewed issue has already been corrected on newer `main`, verify the acceptance criteria and close it as no-op/partially already satisfied rather than recreating work.
10. Stop at plan scope. Substantial unrelated findings belong in a separate future issue/plan, not an expanded child plan.
