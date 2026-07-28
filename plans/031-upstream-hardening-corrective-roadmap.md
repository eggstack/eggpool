# Upstream Hardening Corrective Roadmap

Date: 2026-07-28
Status: implementation handoff

Corrective baseline:

- `cb7407b2114eb8aab5bc536d5b1e3b200afcaa56`

Supersedes closure claims in:

- `plans/022-upstream-error-isolation-and-hotpath-hardening-roadmap.md`
- `plans/030-hardening-integration-soak-and-rollout-closure.md`
- `artifacts/plan-030-exact-head-evidence.md`

The implementation from Plans 023–030 is substantial and should be preserved where correct. This roadmap exists because the prior closure evidence overstated what was actually exercised, the OpenCode Go MiniMax-M3 contract is keyed to the wrong provider identity, the canonical/performance/soak tests bypass the Eggpool runtime, and the request-side payload pipeline still delegates to legacy context-mutating helpers.

## Objective

Close the remaining correctness and evidence gaps without redesigning the already-landed failure-effects, finalization, database-recovery, or observability systems.

The final state must prove, through the real Eggpool application path, that:

1. OpenCode Go MiniMax-M3 resolves the correct provider-bound thinking-control contract from the actual configured provider identity.
2. Unsupported thinking controls are rejected or removed before upstream dispatch according to explicit policy.
3. A compatibility error cannot mutate unrelated health, quarantine, circuit, account, catalog, reservation, or database state.
4. Process-owned finalization and database recovery work under deterministic cancellation and transaction-fault injection.
5. Provider-bound request transformations use one authoritative decoded payload lifecycle rather than a no-op pipeline wrapper around legacy reparsing helpers.
6. Performance measurements exercise the Eggpool proxy path and compare measured values against a committed baseline.
7. Long-running tests are real duration/request-count tests with bounded resource assertions, not short direct-upstream approximations.
8. Closure evidence references the exact final implementation tree and is regenerated after every source or test change.

## Non-goals

- Rewriting the routing architecture.
- Replacing SQLite.
- Adding new provider integrations unrelated to the defect.
- Reimplementing the failure-effects or database-recovery systems from scratch.
- Enabling the dispatch writer by default without evidence.
- Requiring live provider credentials as the sole acceptance mechanism.
- Broad dashboard redesign.

## Corrective phases

### Plan 032 — Provider Identity and MiniMax Contract Correction

Fix provider-contract matching so the OpenCode Go MiniMax-M3 contract is selected from the actual OpenCode Go provider identity and endpoint configuration. Remove the overlapping URL rules that currently make native MiniMax behavior require an operator override. Add narrow resolver and full request-path tests.

Primary ownership boundary: provider-contract keying and resolution only.

### Plan 033 — Real Eggpool Runtime Test Harness

Create one reusable in-process Eggpool application harness with temporary SQLite, actual routing/account/catalog state, provider clients directed to mock upstreams, and structured state snapshots. Replace direct `httpx -> MockUpstream` claims with requests entering Eggpool's ASGI proxy endpoint.

Primary ownership boundary: test infrastructure only.

### Plan 034 — Error Isolation, Finalization, and Recovery Closure Matrix

Using the Plan 033 harness, implement the actual canonical MiniMax scenario, health/quarantine assertions, reservation/active-count checks, deterministic cancellation seams, and database commit/rollback/invalidation fault cases.

Primary ownership boundary: correctness verification and narrowly necessary fixes discovered by those tests.

### Plan 035 — Provider-Bound Request Pipeline Completion

Make `ProviderBoundRequest` the actual mutable/serialized request payload used by all post-selection transforms. Remove the no-op request wrapper and stop legacy transform helpers from independently parsing and serializing the same body.

Primary ownership boundary: request payload lifecycle only.

### Plan 036 — Proxy-Path Performance and Writer Benchmark Correction

Replace direct-upstream performance tests with measurements through the real Eggpool proxy runtime. Exercise dispatch writer disabled/enabled profiles, JSON operation counts, transaction scope, queue age, span sampling, and request latency using explicit baseline and comparison artifacts.

Primary ownership boundary: bounded performance tests and benchmark evidence only.

### Plan 037 — Real Soak and Resource Plateau Validation

Add short, standard, and extended soak modes that run the actual proxy path for stated durations/request counts. Measure RSS, tasks, threads, descriptors, writer/recorder bounds, finalization registry, database recovery cycles, lock wait, and latency windows.

Primary ownership boundary: long-running validation and leak fixes directly exposed by it.

### Plan 038 — Exact-Head Corrective Closure

Run all focused and repository-wide gates on the final tree, regenerate truthful exact-head evidence, update plan statuses, and ensure no source/test change follows verification without rerunning affected gates.

Primary ownership boundary: evidence, CI partition, documentation, and final status only.

## Dependency graph

```text
031 roadmap
  |
  +--> 032 provider identity correction
  |
  +--> 033 real runtime harness
           |
           +--> 034 correctness/recovery matrix
           |
           +--> 036 real proxy performance
           |
           +--> 037 real soak validation
  |
  +--> 035 provider request pipeline completion
           |
           +--> 036 real proxy performance
           +--> 037 real soak validation

032 + 034 + 035 + 036 + 037 --> 038 exact-head closure
```

Plan 032 and Plan 033 may be implemented independently. Plan 034 requires both. Plan 035 may begin after Plan 033 but must finish before final performance and soak evidence. Plan 038 is blocked until every earlier plan is complete.

## Small-model execution rules

Each detailed plan is intentionally narrow. Implementers must follow these rules:

1. Do not opportunistically refactor adjacent subsystems.
2. Do not weaken assertions to make existing code pass.
3. Do not replace real-runtime tests with direct helper/unit tests.
4. Do not claim a duration or request count that was not actually executed.
5. Do not mark a plan complete from commit messages alone.
6. Commit code and tests together for each phase.
7. Record exact commands and numeric results in a phase artifact.
8. If a phase exposes a defect outside its ownership boundary, document it and stop rather than expanding scope silently.
9. Preserve backward-compatible configuration unless the plan explicitly authorizes a migration.
10. Keep secrets, request content, and provider credentials out of evidence artifacts.

## Global invariants

The following invariants apply to every phase:

- A client/provider compatibility error is request-local.
- Failure effects are applied at most once per attempt.
- A model is not indefinitely withdrawn from one ambiguous observation.
- Every selected attempt reaches a terminal durable state.
- Every runtime ownership token is released exactly once.
- Database invalidation is recoverable when the database itself is healthy.
- Readiness remains false during uncertain database state.
- The common provider request path decodes once and serializes once after final mutation.
- Diagnostic storage is bounded by configuration.
- Tests use deterministic barriers and fault seams instead of sleeps where correctness depends on ordering.
- Evidence distinguishes measured results from configured thresholds.

## Required final evidence

Plan 038 must produce a new artifact, not overwrite the old claim without explanation:

- `artifacts/plan-038-exact-head-evidence.md`

It must contain:

- exact 40-character implementation commit and tree SHA;
- audit baseline and final head;
- Python 3.11 and 3.12 versions actually used;
- focused results for Plans 032–037;
- standard repository suite result;
- reload/control-plane result;
- measured performance table with raw sample counts;
- actual soak durations, request counts, and resource deltas;
- cancellation and database fault repetition counts;
- lint, formatting, typecheck, skip/xfail audit results;
- CI run identifiers or an explicit statement that CI evidence is unavailable;
- a file-diff proof that no source/test files changed after verification.

## Roadmap acceptance criteria

- [ ] Plan 032 is completed with actual OpenCode Go identity tests.
- [ ] Plan 033 provides a reusable real Eggpool runtime harness.
- [ ] Plan 034 proves error isolation, finalization, and database recovery through that harness.
- [ ] Plan 035 removes the no-op provider request pipeline and duplicate request parsing/serialization.
- [ ] Plan 036 measures the real proxy path and writer profiles.
- [ ] Plan 037 executes truthful short, standard, and extended soak modes.
- [ ] Plan 038 regenerates exact-head evidence after all implementation changes.
- [ ] Old Plan 030 evidence is marked superseded, not silently treated as valid.
- [ ] No correctness item is deferred while the roadmap is marked complete.

## Explicit rejection conditions

Do not close this roadmap if any of these remain:

- OpenCode Go contract matching depends on `api.minimax.io` rather than actual provider identity/configuration.
- Native MiniMax behavior requires an unrelated explicit override because built-in patterns overlap.
- Canonical closure tests send requests directly to mock upstreams.
- Runtime-ownership assertions are inferred only from request counts.
- Database recovery claims are made without a real temporary SQLite database and injected transaction faults.
- Performance tests bypass the Eggpool proxy or do not instantiate the dispatch writer profile they claim to test.
- Extended soak evidence is simulated, extrapolated, or described as “equivalent.”
- `ProviderBoundRequest` remains a no-op wrapper around context-mutating transforms.
- Exact-head evidence references a commit predating source/test changes.
