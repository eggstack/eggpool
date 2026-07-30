# Residual Upstream, Streaming, and Hot-Path Hardening Roadmap

Date: 2026-07-30
Status: closed at 3b8976d5
Plan: 045

Planning baseline:

- `216e615d75269cc1471a920ae81ece9ef2d21802`

Related plans:

- `plans/046-provider-thinking-control-normalization.md`
- `plans/047-terminal-lifecycle-and-cancellation-safety.md`
- `plans/048-stream-completion-and-premature-eof.md`
- `plans/049-provider-timeout-policy-and-stream-diagnostics.md`
- `plans/050-provider-bound-request-single-decode-lifecycle.md`
- `plans/051-unified-sse-framing-and-transcode-hotpath.md`
- `plans/052-selection-persistence-and-trace-hotpath.md`
- `plans/053-real-runtime-validation-and-exact-head-closure.md`
- `plans/054-test-suite-and-verification-reduction.md`

## Purpose

Close the remaining request-local compatibility, streaming termination, cancellation cleanup, and measured hot-path defects without rebuilding production-grade verification infrastructure around a private, LAN/SBC-oriented proxy.

The motivating defects remain:

1. OpenCode Go MiniMax-M3 may reject client thinking controls that native MiniMax accepts.
2. An upstream validation or cancellation path can leave runtime ownership inconsistent and degrade later traffic.
3. A native or transcoded stream can end cleanly at the transport layer before protocol completion and be reported as successful.
4. Request and streaming paths perform avoidable duplicate parsing, serialization, database, and trace work.

The corrective work is still required. The original verification model was not. This revision removes exhaustive Cartesian matrices, repeated fault campaigns, percentage latency gates in CI, mandatory soak runs, and closure-evidence bureaucracy.

## Authoritative verification amendment

Plan 054 is authoritative for testing, CI, performance validation, soak validation, handoff evidence, and closure mechanics across Plans 045–053.

Where Plans 046–052 contain broader `Required tests`, performance matrices, repetition counts, exhaustive chunk-boundary cases, handoff tables, or evidence requirements, interpret them as candidate coverage, not cumulative mandatory work. Plan 054 supersedes those requirements with a fixed verification budget.

The implementation must not add or preserve infrastructure merely to satisfy an older plan sentence.

## Design center

Eggpool is a private-deployment proxy intended primarily for SBC and LAN use. It is not an internet-facing multi-tenant control plane. The hardening target is therefore:

- request-local upstream failures;
- no persistent routing/database poisoning;
- no silent stream success after truncation;
- bounded local resources;
- correct common provider/protocol behavior;
- maintainable code and fast iteration.

It is explicitly not:

- adversarial public-internet fuzz coverage;
- exhaustive provider/protocol permutation proof;
- hyperscale concurrency certification;
- formal exactly-once infrastructure beyond what the local process needs;
- release-grade benchmark automation;
- permanent evidence production.

## Governing principles

1. Correct the confirmed defect at the narrowest ownership boundary.
2. Prefer deleting special cases over adding supervisors, registries, adapters, or policy layers.
3. Reuse existing HTTPX, pytest, respx, SQLite, and runtime fixtures.
4. Add one regression test near the defect and at most one real request-path smoke when layer interaction matters.
5. Do not duplicate the same assertion across every protocol, streaming mode, policy mode, and Python version unless code paths are materially different.
6. No property-test dependency, fuzz framework, benchmark framework, test scheduler, evidence manifest, or new CI matrix.
7. No mandatory live provider credentials.
8. Performance work is accepted primarily through deterministic operation removal and one optional local comparison, not noisy CI percentiles.
9. No mandatory soak or long-running validation for ordinary development.
10. Ordinary CI remains a small smoke gate; release and extended validation remain manual.

## Revised phase sequence

### Plan 054 — Test Suite and Verification Reduction

Apply this plan immediately as the verification contract for all subsequent phases. Reduce CI to one fast job, collapse or delete redundant tests and markers, remove mandatory soak/evidence apparatus, and establish a small canonical smoke suite.

### Plan 046 — Provider Thinking-Control Normalization

Correct the known fixed-contract control leaks and contract resolution ordering.

Verification budget:

- table-driven unit coverage for the distinct field shapes;
- one OpenCode Go reject/drop request-path case;
- one native MiniMax preservation case;
- no full cross-product of every field, policy, client protocol, stream mode, and contract kind.

### Plan 047 — Terminal Ownership and Cancellation Cleanup

Remove the streaming 4xx double-finalization path and ensure complete cleanup survives request cancellation.

Implementation guidance:

- extend the existing retained finalization mechanism rather than creating another supervisor or persistence framework;
- keep the terminal command/result shape no larger than required by actual cleanup call sites;
- do not build generic workflow orchestration.

Verification budget:

- one upstream 4xx single-finalization regression;
- one capability-rejection cancellation regression;
- one post-durable-transition cancellation regression;
- one subsequent-request-health smoke;
- no cancellation at every await and no repeated 25-run campaign in ordinary tests.

### Plan 048 — Stream Completion and Premature EOF

Retain OpenAI `[DONE]` and Anthropic `message_stop`, and classify payload-without-terminal EOF as incomplete.

Verification budget:

- canonical OpenAI and Anthropic completion;
- one representative fragmented terminal frame;
- one premature EOF before downstream bytes;
- one premature EOF after downstream bytes;
- one transcoded no-false-terminal case;
- no every-byte-boundary matrix or fuzz/property corpus.

### Plan 049 — Minimal Timeout Evidence and Provider Tuning

First distinguish timeout exceptions from clean premature EOF using existing HTTPX semantics and bounded diagnostics.

Do not introduce separate first-byte, idle, and lifetime timer machinery unless captured evidence proves the current provider `read_timeout_s` cannot express the required behavior. A provider-specific read-timeout change is acceptable when the observed failure is actually `ReadTimeout`; no global increase is allowed.

Verification budget:

- old configuration remains valid;
- one timeout classification case;
- one premature-EOF-not-timeout case;
- one provider-specific override case;
- no accelerated 300-second simulation framework, timer state machine, or exhaustive timeout taxonomy unless implementation evidence requires it.

### Plan 050 — Single Provider Payload Lifecycle

Use one decoded provider payload through post-selection transforms and serialize once at dispatch.

Prefer a simple authoritative dictionary plus final serialization over a large production mutation framework. A generation/freeze counter is permitted only where it prevents a demonstrated stale-byte defect; production mutation logs and test-only runtime telemetry are not required.

Verification budget:

- one native request;
- one transcoded request;
- one transformed request proving no repeated parse/encode;
- one stale/final-body regression if needed;
- no full native/transcoded × streaming/non-streaming × cache-policy matrix.

### Plan 051 — Shared SSE Framing

Extract one bounded incremental SSE framer and share frames between completion observation and transcoding.

Do not benchmark three output-coalescing architectures by default. Preserve the current emission strategy unless a simple local profile shows it is material. Do not add a lazy JSON envelope or frame abstraction beyond what the two consumers actually need.

Verification budget:

- one native observation case;
- one OpenAI→Anthropic and one Anthropic→OpenAI transcode case;
- one malformed/incomplete frame case;
- one deterministic assertion that raw bytes are framed once;
- optional local concurrency comparison, not a CI gate.

### Plan 052 — Selection and Trace Hot-Path Cleanup

Perform only the two confirmed low-risk changes:

1. prehydrate the durable account identity needed under the selection lock;
2. stop rescanning all accounts solely to construct discarded trace detail.

Do not create a broad selection instrumentation suite, fairness benchmark matrix, or dispatch-writer comparison project.

Verification budget:

- one assertion that the database is not awaited under the claim lock;
- one trace-off/unsampled no-extra-scan assertion;
- one deterministic routing parity fixture;
- one reload identity-map case only if production code changes that path.

### Plan 053 — Lean Runtime Closure

Run focused regressions, the reduced smoke suite, and one compact real-process/request-path smoke. Optional local/SBC measurement may be recorded, but soak, percentile gates, exact-head status-only commits, and evidence bundles are not closure requirements.

## Dependency order

```text
054 verification reduction applies immediately

046 control normalization ----+
047 terminal cleanup ---------+--> 053 lean closure
048 stream completion --------+
049 minimal timeout evidence -+
050 single payload -----------+
051 shared SSE framing -------+
052 selection cleanup --------+
```

Plans 046, 047, 050, and 052 may proceed independently. Plan 048 should follow the terminal-owner decision from Plan 047. Plan 049 follows Plan 048 so timeout and EOF are not conflated. Plan 051 consumes the completion model from Plan 048. Plan 053 closes the combined work.

## Cross-phase correctness invariants

- Generic provider validation errors remain request-local unless typed evidence says otherwise.
- Unsupported thinking controls do not suppress accounts or models.
- Every selected attempt reaches one terminal state and releases its owned reservation/count/probe.
- No retry occurs after downstream bytes are emitted.
- Clean EOF is not success unless protocol completion or an explicit provider compatibility rule is present.
- Original request content and credentials are not persisted in diagnostics.
- Request and stream buffers remain bounded.
- No database I/O is introduced under the selection claim lock.
- No new background service, queue, supervisor, or database schema is introduced solely for verification.

## Verification budget

For each implementation phase:

- normally modify no more than one focused unit test file and one smoke/integration file;
- add no more than roughly 4–8 distinct regression cases unless materially separate code paths require more;
- prefer parameterization over copied tests;
- do not repeat a unit assertion through the real process unless integration can invalidate it;
- do not require more than one deterministic cancellation point per materially different cleanup boundary;
- do not add timing assertions to ordinary CI;
- do not run live, network, performance, soak, or extended-soak tests in ordinary CI;
- do not require retained evidence files.

These are complexity limits, not minimum test quotas. A phase may need fewer tests.

## Roadmap acceptance criteria

- [ ] Plan 054 establishes a smaller canonical CI/smoke contract and a concrete deletion plan for redundant tests and validation infrastructure.
- [ ] Plan 046 prevents unsupported OpenCode Go thinking controls from reaching upstream while preserving native MiniMax behavior.
- [ ] Plan 047 removes duplicate terminal ownership and closes the demonstrated cancellation cleanup gap.
- [ ] Plan 048 distinguishes protocol completion from premature clean EOF.
- [ ] Plan 049 classifies timeout versus EOF before any provider-specific timeout change.
- [ ] Plan 050 removes repeated provider-body decode/encode work without creating a second request framework.
- [ ] Plan 051 removes duplicate SSE framing without building a generalized streaming platform.
- [ ] Plan 052 removes database-under-lock and discarded trace work only.
- [ ] Plan 053 closes with focused regressions and a compact smoke, not an exhaustive matrix.
- [ ] No new CI job, Python matrix, coverage threshold, benchmark service, soak gate, or evidence bundle is introduced.
- [ ] The resulting ordinary development loop is faster and simpler than the current full non-slow-suite gate.

## Rejection conditions

Do not close this roadmap if:

- a known request can still poison unrelated routing/runtime state;
- a truncated stream is still recorded as completed;
- the fix adds a parallel finalization, request-payload, parser, or timeout framework without deleting equivalent legacy machinery;
- implementation follows the old exhaustive test matrices despite Plan 054;
- ordinary CI still runs the entire historical non-slow suite by default after Plan 054;
- a mandatory performance percentage, soak duration, or evidence artifact blocks ordinary handoff;
- test-support code grows materially for a narrowly scoped defect.

## Definition of done

This roadmap is complete when the confirmed provider-control, terminal cleanup, premature EOF, and measured hot-path defects are corrected; the focused regressions and compact smoke suite pass; unrelated requests remain healthy after representative failures; and the repository has fewer mandatory tests, less validation machinery, and a faster single-job CI path than at the planning baseline.