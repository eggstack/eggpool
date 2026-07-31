# Plan 053 — Lean Runtime Validation and Closure

Date: 2026-07-30
Status: corrected by Plan 055; terminal-stream closure claims require the corrective pass
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Depends on: Plans 046 through 052
Verification authority: `plans/054-test-suite-and-verification-reduction.md`
Planning baseline: `216e615d75269cc1471a920ae81ece9ef2d21802`

## Objective

Close the residual upstream/streaming hardening work with the smallest verification set that can catch the confirmed regressions through Eggpool's real request path.

This plan replaces the earlier exhaustive closure model. It does not require:

- every provider-control spelling across every protocol, policy, and stream mode;
- cancellation or database failure at every await boundary;
- repeated 25-run fault campaigns;
- arbitrary/every-byte SSE fragmentation matrices;
- 100–500-request CI benchmarks;
- 5,000-attempt or 10–15-minute mandatory profiles;
- 1–4-hour soak runs;
- p50/p95/p99 performance gates in CI;
- exact-head status-only commits and reruns;
- retained JSON/Markdown evidence bundles.

The purpose is to prove that the specific defects are fixed, not to certify Eggpool as an internet-facing production platform.

## Operating assumptions

Eggpool is a private LAN/SBC deployment with modest concurrency and a lax public-security posture. Verification should prioritize:

1. later requests remain usable after an upstream error;
2. cleanup ownership returns to baseline after representative cancellation;
3. incomplete streams are not reported as successful;
4. provider payload and SSE work are not duplicated;
5. ordinary development remains fast.

Do not introduce a new runtime-validation framework. Reuse existing pytest/respx/in-process application fixtures and the smallest existing real-process smoke path.

## Required closure set

### 1. Focused provider-control regressions

Run a compact set that proves:

- OpenCode Go fixed-thinking reject sends no upstream request;
- OpenCode Go warn/drop sends no unsupported thinking control;
- native MiniMax retains its supported control behavior;
- provider ID specificity is not overridden by a broad URL/kind rule.

A table-driven unit test may cover additional field spellings. Do not duplicate all rows through both public endpoints unless separate endpoint code changes the result.

### 2. Focused terminal-cleanup regressions

Prove:

- streaming upstream 4xx enters terminal finalization once;
- cancellation after durable selection still releases reservation, active count, and probe ownership;
- a local capability rejection applies no provider health/backoff effect;
- one unrelated request succeeds immediately afterward without restart or database deletion.

One deterministic cancellation barrier at each materially different ownership boundary is sufficient. Do not cancel at every internal await.

### 3. Focused stream-completion regressions

Prove:

- OpenAI `[DONE]` completes;
- Anthropic `message_stop` completes;
- one representative fragmented terminal frame completes;
- payload followed by clean EOF without terminal evidence is not `COMPLETED`;
- a transcoded incomplete stream emits no synthetic success marker;
- timeout classification remains distinct from clean EOF.

No every-byte partition test is required. A malformed/incomplete frame case is sufficient for bounded-parser behavior.

### 4. Focused hot-path architecture regressions

Prove deterministically:

- post-selection provider transforms share one decoded payload and dispatch one final serialized body;
- observer and transcoder do not independently frame the same upstream bytes;
- selection performs no SQLite lookup while the claim lock is held;
- an unsampled/off trace path does not rescan all accounts solely for diagnostics.

These should be operation/spy assertions, not timing assertions.

### 5. Compact real request-path smoke

Use the existing in-process or real-process Eggpool harness with temporary SQLite and a deterministic local mock upstream.

The smoke needs only these representative cases:

1. one successful non-stream request;
2. one successful stream with canonical terminal marker;
3. one upstream validation/control error followed by a successful unrelated request;
4. one premature EOF stream recorded as incomplete rather than completed;
5. one basic OpenAI↔Anthropic transcode path when Plan 051 changes streaming framing.

The smoke must exercise the real router, coordinator, HTTP client, persistence, and finalization path. It need not enumerate every policy or provider.

## Canonical commands

Use focused test paths chosen by the implementation, then run the repository's reduced canonical gate established by Plan 054.

Expected shape:

```text
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest <focused Plan 046-052 regression files> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Do not run the entire historical non-slow suite merely because it exists. Run additional tests only for directly touched behavior.

## Optional local/SBC check

For request lifecycle, streaming, or parser changes, a short manual run on representative hardware is useful but not a closure blocker when unavailable.

Preferred characteristics:

- 60–300 seconds;
- realistic private deployment concurrency, usually 1–8;
- deterministic local mock provider;
- no live credentials required;
- check that requests finish, no obvious progressive latency appears, and active/reservation state drains.

A simple console summary is sufficient. Do not require a structured evidence schema, percentile gate table, artifact upload, or checksum manifest.

The existing long stability/soak apparatus should not be extended for this roadmap. Plan 054 decides whether to simplify or delete it.

## Performance validation

Performance acceptance is primarily architectural:

- one provider payload parse lifecycle rather than one per transform;
- one SSE framing pass rather than observer plus transcoder parsing;
- no database await under the claim lock;
- no discarded full-account trace scan.

One same-machine local comparison may be recorded for confidence. There is no fixed 5%, 10%, or 15% timing threshold, because those values are noisy, hardware-dependent, and likely to produce more harness work than product value.

A measurable regression large enough to be obvious in repeated local use should be investigated. Small percentile variation is not a closure failure.

## Documentation scope

Update only documentation made false by implementation:

- OpenCode Go/native MiniMax thinking controls;
- premature EOF versus timeout meaning;
- provider-specific timeout override if changed;
- concise terminal cleanup/request payload/SSE ownership notes where operators or maintainers need them.

Do not add architecture narratives, evidence reports, operator dashboards, or troubleshooting matrices solely to close this plan.

## Handoff record

The final handoff needs only:

- implementation commit SHA;
- files changed;
- focused test commands and pass/fail result;
- smoke command and result;
- optional local/SBC observation when run;
- any unresolved provider-specific behavior.

No status-only closure commit or retained evidence file is required. Plan statuses may be updated in the final implementation commit or a normal documentation follow-up.

## Acceptance criteria

- [ ] Focused OpenCode Go and native MiniMax control regressions pass.
- [ ] Streaming upstream 4xx is finalized once.
- [ ] Representative cancellation cleanup returns reservation, active count, and probe state to baseline.
- [ ] A subsequent unrelated request succeeds without restart/database repair.
- [ ] OpenAI and Anthropic canonical terminal events complete correctly.
- [ ] Clean premature EOF is not recorded as completed.
- [ ] Transcoding does not synthesize a success terminal after incomplete EOF.
- [ ] Timeout and premature EOF remain distinguishable.
- [ ] Provider transforms use one authoritative decoded payload and final serialization.
- [ ] Upstream SSE bytes are framed once when observation and transcoding are active.
- [ ] No SQLite operation occurs under the selection claim lock.
- [ ] Unsampled/off trace handling performs no diagnostic full-account rescan.
- [ ] Focused regressions and `tests/smoke/` pass.
- [ ] Ordinary CI is not expanded and no long/performance/live tests are added to it.
- [ ] No mandatory soak, timing percentage gate, evidence artifact, or exact-head rerun remains.

## Rejection conditions

Do not close Plan 053 if:

- known upstream validation can still poison later requests;
- cancellation leaves request ownership behind;
- clean EOF can still be finalized as success without terminal evidence;
- duplicate parsing/serialization remains in the specific paths targeted by Plans 050–051;
- the implementation adds a new test runner, fault-injection framework, benchmark framework, background supervisor, or evidence format;
- closure depends on exhaustive matrices rather than representative defect regressions;
- the historical full non-slow suite is still treated as a mandatory per-push gate after Plan 054.

## Definition of done

Plan 053 is complete when the confirmed defects are exercised through a small set of focused regressions and a compact real request-path smoke, all request ownership drains correctly, truncated streams cannot masquerade as successful, the deterministic hot-path operation reductions are present, and closure requires no new CI, soak, benchmark, or evidence bureaucracy.
