# Plan 053 — Real-Runtime Validation, Performance Gates, and Exact-Head Closure

Date: 2026-07-30
Status: implementation handoff
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Depends on: Plans 046 through 052
Planning baseline: `216e615d75269cc1471a920ae81ece9ef2d21802`

## Objective

Prove the combined residual-hardening implementation through the real Eggpool application path, close any integration defects exposed by that proof without scope expansion, and mark Plans 045–053 complete only on the exact final source/test head.

This phase owns integration, bounded performance/soak validation, documentation reconciliation, and status closure. It must not introduce another permanent evidence framework, expand ordinary CI, or weaken functional/performance gates to accommodate failures.

## Required operating model

Preserve the repository's reduced verification and release model:

- ordinary CI remains small and fast;
- long/extended runtime validation remains manually invoked or local;
- package publication remains governed by the repository's current manual/limited release policy;
- no new platform or Python matrix is added for this roadmap;
- no checked-in request logs, credentials, raw benchmark dumps, or large artifacts;
- no claim that shared CI hardware represents Raspberry Pi performance.

## Closure baseline and source of truth

At implementation start, record:

- exact current `main` commit;
- completion commits for Plans 046–052;
- supported Python versions from project configuration;
- canonical lint/type/test commands from repository documentation/configuration;
- current ordinary CI workflow names.

If the tree changed after this plan was written, use the implementation-start baseline rather than assuming `216e615...` remains current.

Plan 053 is the closure source of truth for Plans 045–053. Historical Plan 031–038 evidence remains historical and must not be reused as proof for newly corrected behavior.

## Canonical real-runtime harness

Use the repository's real in-process or spawned Eggpool application harness with:

- temporary file-backed SQLite for canonical persistence/recovery cases;
- actual runtime generation factory;
- real account registry, catalog, router, coordinator, provider client pool, finalizer, effects applier, quarantine, metrics buffers, and stream diagnostics;
- deterministic local mock OpenCode Go and native MiniMax providers;
- requests entering `/v1/chat/completions` and `/v1/messages` as appropriate;
- captured mock upstream requests/responses;
- structured runtime/database snapshots before, during, and after each case;
- no direct client-to-mock-upstream substitution for canonical acceptance tests.

Unit tests may supplement the harness but cannot substitute for it.

## Required integration scenarios

### Scenario group A — Provider control correctness

For OpenCode Go MiniMax-M3, test both client protocols, streaming and non-streaming, with:

```json
{"reasoning_effort":"high"}
{"reasoning_effort":"xhigh"}
{"thinking":{"type":"enabled"}}
{"thinking":{"effort":"high"}}
{"thinking":{"budget_tokens":4096}}
{"thinking":{"type":"enabled","budget_tokens":4096}}
{"thinking_budget":4096}
```

For each configured policy:

- reject: local 400, zero upstream requests;
- warn-drop: upstream request contains no unsupported controls;
- map-if-known: only explicit mappings, deterministic fallback otherwise.

Native MiniMax cases must prove accepted controls remain supported and do not resolve the OpenCode fixed contract.

### Scenario group B — Error isolation and terminal convergence

For local capability rejection, upstream generic 400, typed quota/rate-limit error, connect timeout, pre-body EOF, midstream EOF, midstream transport exception, and client cancellation, assert:

- exactly one terminal request outcome;
- exactly one attempt transition per attempt;
- durable reservation released;
- in-memory quota reservation removed;
- active count returns to baseline;
- circuit half-open probe not retained;
- failure effects applied at most once;
- request-local validation creates no account/model suppression;
- unrelated providers/accounts remain eligible;
- next unrelated request succeeds without restart or database modification.

### Scenario group C — Cancellation/fault matrix

Inject cancellation or failure at every Plan 047 seam with deterministic barriers. Run each seam repeatedly, minimum 25 repetitions in focused local validation and a smaller deterministic subset in ordinary tests.

Include:

- database operation delayed/failed before and after durable transition;
- ambiguous commit/recovery path where supported;
- cancellation before/after request response starts;
- duplicate identical terminal submission;
- conflicting terminal submission;
- shutdown while retained finalization is active.

After convergence assert no leaked jobs, reservations, active counts, probes, tasks, or pending requests.

### Scenario group D — Stream completion matrix

For OpenAI and Anthropic upstream protocols, native and both transcode directions:

- canonical terminal event;
- terminal event split at arbitrary byte boundaries;
- final frame without trailing newline;
- clean EOF before first payload;
- content then clean EOF without terminal;
- malformed/incomplete terminal frame;
- idle timeout before first byte;
- idle timeout after emitted bytes;
- remote protocol/read error;
- client cancellation;
- provider compatibility completion where explicitly configured.

Assert no false success marker or completed outcome for incomplete cases.

### Scenario group E — Request-body lifecycle

Capture operation counts and final upstream payload for:

- native OpenAI/Anthropic;
- both transcode directions;
- thinking controls present/absent;
- synthetic cache disabled/dry-run/apply;
- stream options absent/present/invalid;
- prepared transcode reused/recomputed.

Required architectural proof:

- one client decode;
- zero/one provider decode as designed, never one per transform;
- one final provider encode for dispatched generation;
- provider-bound/context bytes cannot diverge;
- original client payload remains unchanged.

### Scenario group F — Selection/trace contention

Exercise:

- 1 and 8 accounts;
- healthy, quarantined, open/half-open circuit mixtures;
- concurrency 1, 5, and 20;
- trace off, sampled, and all;
- direct persistence and dispatch writer profile where already supported.

Assert:

- zero SQLite awaits under selection-claim lock;
- no unsampled full-account diagnostic scan;
- fairness/routing sequence parity for deterministic fixtures;
- compensation after persistence/publication cancellation;
- trace queue pressure never fails dispatch.

## Performance profiles

### Profile 1 — Focused deterministic gate

Purpose: ordinary tests/CI-safe operation counts and bounded latency regression checks.

Suggested workload:

- deterministic mock provider;
- 100–500 requests per case where runtime permits;
- concurrency 1 and 5;
- native and transcoded stream/non-stream mix;
- no wall-clock hardware promises.

Mandatory gates:

- decode/encode/framing operation counts;
- no leaked ownership state;
- native p95 regression no worse than 5% against same-run baseline where stable;
- no unbounded queue/buffer growth.

### Profile 2 — Standard local/SBC-reference validation

Purpose: meaningful combined-system measurement.

Suggested minimum:

- 10–15 minutes or at least 5,000 completed/failed attempts, whichever produces a representative sample;
- concurrency 1, 5, and 8 for streaming mixes;
- native and transcoded workloads;
- periodic injected compatibility errors, premature EOF, timeout, and cancellation;
- file-backed SQLite;
- trace sampled default and one trace-all burst.

Record actual duration/request counts, not target-only labels.

### Profile 3 — Extended local soak

Purpose: long-running plateau/leak confidence. Manual/local only.

Suggested:

- 1–4 hours depending deployment hardware;
- realistic private SBC concurrency rather than hyperscale load;
- repeated provider errors and recovery cycles;
- optional rehash during active/idle periods;
- actual hardware/OS/Python metadata.

This profile may be unavailable for a particular closure run, but its absence must be stated. It cannot be simulated or extrapolated.

## Metrics and bounds

Capture bounded before/after snapshots for:

- dispatch and local pre-upstream p50/p95/p99;
- stream first-byte/inter-token/completion latency;
- selection lock wait/hold;
- persistence wait/transaction/commit;
- process CPU or stable proxy;
- RSS;
- thread/task/file-descriptor counts where supported;
- active requests and quota reservations;
- durable active reservations/pending requests;
- finalization job registry/retry queue;
- routing trace queue/depth/drops;
- metrics buffers;
- SSE parser/frame/JSON operation counts;
- stream outcome counters including premature EOF and timeout classes;
- database contention/recovery counters.

Required resource end state:

- active requests: baseline/zero;
- in-memory reservations: baseline/zero;
- durable active reservations: zero;
- pending requests beyond expected active work: zero;
- half-open probe in-flight flags: none retained;
- finalization registry: drained/bounded;
- parser/transcoder buffers: released;
- trace/metrics queues: bounded and drainable;
- no monotonic RSS/task/descriptor growth inconsistent with warm caches.

## Performance acceptance criteria

All comparisons must use the same harness/machine and identify warmup.

- Native dispatch p95 regression from the pre-phase same-run baseline: no worse than 5%.
- Native stream inter-token p95 regression: no worse than 10%.
- Transcoded 5–8 stream CPU/throughput: at least 15% material improvement from the duplicate-framing/request-reparse baseline, or an explicitly justified equivalent reduction in measured JSON/framing operations with no latency regression.
- One SSE framing pass per upstream byte/chunk: mandatory.
- Provider payload final encode count: one per dispatched final generation.
- Zero database awaits under selection-claim lock: mandatory.
- No late/early resource or latency trend indicating progressive degradation in standard profile.

Do not weaken deterministic architectural gates because timing is noisy. Timing thresholds may remain manual/local if shared CI variance is excessive.

## Repository verification

Run the canonical commands defined by the repository. At minimum, the closure record must include the actual equivalents of:

```text
format check
lint
static type check
focused Plans 046-052 tests
canonical non-slow/ordinary test suite
real-runtime process/integration smoke
rehash/control-plane tests affected by provider timeout/account identity changes
```

Do not invent a new verification script unless existing commands cannot compose the required focused cases. Any new helper must remain a thin local runner, not a second test framework.

Audit:

- skipped/xfail tests touching this roadmap;
- warnings from affected tests;
- test-only branches in production code;
- stale plan/status/documentation claims;
- duplicated parser/finalizer/request-payload code that should have been removed.

## Documentation reconciliation

Update only affected documentation:

- provider thinking-control behavior;
- timeout semantics/config examples;
- stream outcome/diagnostic meaning;
- runtime tuning guidance for MiniMax;
- architecture notes for terminal ownership, provider-bound request lifecycle, and shared SSE decoder;
- operator troubleshooting for incomplete streams without suggesting database deletion as routine recovery.

Documentation must distinguish:

- request-local provider validation error;
- provider health/backoff error;
- premature EOF;
- idle timeout;
- client cancellation;
- database/finalization failure.

## Exact-head closure procedure

1. Implement and verify Plans 046–052 without marking Plan 045 or Plan 053 complete.
2. Run all focused and repository gates on a candidate implementation commit.
3. Apply any required source/test fixes.
4. Rerun every affected gate after the final source/test change.
5. Update Plans 045–053 statuses in a status/documentation-only commit.
6. Run ordinary CI/checks on that exact status commit SHA.
7. If source or test code changes afterward, reopen closure and repeat.

No new committed evidence artifact is required. The final commit/PR/handoff message may contain the exact command/result table. If the repository requires a retained runtime-validation JSON, use the existing single artifact mechanism; do not create a parallel Markdown/manifest/checksum bundle.

## Required closure record

Record in the final handoff:

- audit baseline SHA;
- final implementation SHA;
- final status SHA;
- changed files grouped by plan;
- Python/OS/hardware used for performance and soak;
- focused test counts/results;
- canonical test result;
- cancellation/fault repetition counts;
- provider-control upstream-capture matrix;
- stream completion/timeout outcome matrix;
- operation-count before/after table;
- native/transcoded performance table;
- actual soak duration/request count/resource deltas;
- exact CI/check status on the final status SHA;
- explicit unavailable evidence, including extended soak/live MiniMax, if not run.

## Acceptance criteria

- [ ] Every Plan 046 control case is exercised through the real Eggpool proxy path.
- [ ] Native MiniMax and OpenCode Go MiniMax contracts remain distinct.
- [ ] Every terminal/cancellation/fault case converges with no ownership residue.
- [ ] The next unrelated request succeeds after every request-local/error injection.
- [ ] Every native/transcoded stream completion case has the correct terminal outcome and downstream marker behavior.
- [ ] Premature EOF and timeout remain distinguishable in diagnostics.
- [ ] Provider timeout tuning is supported by recorded outcome data.
- [ ] Provider-bound request operation-count gates pass.
- [ ] One shared SSE framing pass is proven.
- [ ] Selection lock contains no database I/O and unsampled trace work is eliminated.
- [ ] Native performance regression remains within gates.
- [ ] Transcoded streaming shows the required material improvement or justified equivalent architectural gain.
- [ ] Standard runtime validation shows bounded resource/latency behavior with actual duration and request count reported.
- [ ] No raw request content, response content, credentials, or secrets appear in retained evidence/logs.
- [ ] Canonical lint/type/test/control-plane checks pass.
- [ ] Plans 045–053 are marked complete only after implementation and verification.
- [ ] Ordinary CI/checks pass on the exact final status commit SHA.

## Explicit rejection conditions

Do not close Plan 053 if:

- canonical tests call mock providers directly rather than entering Eggpool;
- a local capability error can still require restart/database deletion for recovery;
- cancellation assertions omit any ownership component;
- incomplete EOF is counted completed;
- timeout tuning is unclassified/speculative;
- request JSON or SSE framing operation counts are inferred rather than measured;
- performance results omit baseline, request count, concurrency, or hardware;
- an extended soak is claimed from a shorter run or extrapolation;
- ordinary CI is expanded to host unstable long benchmarks;
- a source/test commit follows the final verification without rerunning affected gates;
- plan statuses claim closure before exact-head checks finish.

## Definition of done

Plan 053 and the parent roadmap are complete when the real Eggpool runtime proves that unsupported thinking controls are deterministic and request-local; terminal cleanup converges exactly once under cancellation and database faults; valid protocol completion is distinguished from premature EOF and timeout; provider request and stream parsing are single-owner; selection avoids unnecessary serialized work; native performance remains stable; transcoded concurrency improves materially; resource state plateaus under realistic private-SBC workloads; documentation is truthful; and all reduced canonical checks pass on the exact final status head.