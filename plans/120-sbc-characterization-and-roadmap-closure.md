# Plan 120 — SBC Characterization and Roadmap Closure

Date: 2026-08-11
Status: ready
Parent roadmap: `plans/113-sbc-hotpath-reduction-and-protocol-clarity-roadmap.md`
Planning baseline: `6f4df9bd42b5ca336d3da5ef458ab1793e515185`
Depends on:

- `plans/114-provider-payload-copy-on-write.md`
- `plans/115-prepared-transcode-ownership-reduction.md`
- `plans/116-request-estimation-and-ingress-efficiency.md`
- `plans/117-provider-cache-dialect-correctness.md`
- `plans/118-optional-runtime-surface-and-dependency-reduction.md`
- `plans/119-retained-test-and-planning-surface-reduction.md`

## Purpose

Close Roadmap 113 with aggregate correctness verification and one short target-device characterization focused specifically on the hot-path work that motivated the roadmap.

This is not a performance project. It must not create a benchmark harness, hardware CI, soak suite, profiling service, or permanent thresholds.

The closure should answer four practical questions:

1. Did the new ownership model actually remove the reviewed full-request copy/rematerialization paths?
2. Did request-estimation reuse reduce duplicate whole-payload traversal without weakening context-limit correctness?
3. Did cache capability/dialect corrections preserve intended provider behavior without emitting unverified extension fields?
4. On a Raspberry Pi/SBC-class target, do large native streaming and cross-protocol requests behave stably without obvious monotonic memory/resource growth?

When hardware/provider credentials are not available, deterministic tests remain authoritative and unavailable live measurements must be recorded as `not measured`.

## Governing constraints

1. Do not add benchmark, soak, tracing, telemetry, profiling-agent, hardware-CI, or permanent performance evidence infrastructure.
2. Do not fabricate or extrapolate Raspberry Pi numbers from a workstation.
3. Do not establish hard RSS/CPU/latency/socket/WAL thresholds in CI.
4. Do not treat provider network latency as EggPool local overhead.
5. Do not reopen routing, retry/backoff, finalization, rehash, SQLite durability, provider pool sizing, or framework architecture without direct evidence of a regression caused by Plans 114–119.
6. Do not lower provider connection-pool defaults merely because smaller values look leaner.
7. Do not alter SQLite pragmas from generic tuning advice.
8. Use existing runtime-status/dispatch diagnostics, standard OS tools, and test-local instrumentation. Do not build a new harness when shell commands/existing tests suffice.
9. Provider credentials, prompts, cache keys, account identifiers, and response content must not be committed into closure evidence.
10. Full retained-suite execution is optional/manual.
11. Correct only small demonstrated regressions within Roadmap 113 scope; record unrelated findings separately.
12. Stop when acceptance criteria are met.

## Workstream A — Completion truthfulness audit

Before runtime characterization, inspect Plans 114–119 and their actual implementation commits.

Each completed child plan must record:

- implementation commit SHA;
- exact focused tests/commands run;
- acceptance checklist status;
- any intentionally retained full-copy/serialization path and why;
- any intentionally retained optional subsystem and why;
- any intentionally unsupported/lossy provider-cache mapping;
- no unresolved rejection condition.

Specific expected closure evidence:

### Plan 114

- final provider-bound copy/adopt/COW API;
- stream-options no-op behavior;
- which mutations still intentionally take conservative full ownership;
- safe-compression adoption behavior.

### Plan 115

- final PreparedTranscode semantic ownership contract;
- proof physical recursive freeze/rematerialization was removed or why a remaining part is necessary;
- unchanged translated-body reuse;
- recompute/COW cases.

### Plan 116

- canonical estimate call behavior;
- translated estimate call behavior;
- tool-padding disposition;
- body/header copy audit result.

### Plan 117

- execution-date official OpenAI/Anthropic cache semantics checked;
- final provider capability/dialect representation;
- generic provider behavior;
- retained provider-extension mapping, if any;
- TTL/retention loss behavior.

### Plan 118

- retained/removed optional subsystem table;
- compression tuning disposition;
- synthetic cache disposition;
- DNS cache disposition and justification;
- `granian[pname]` verification/result;
- no new dependency/framework.

### Plan 119

- immediate pre/post test collection counts;
- clusters consolidated/deleted;
- protected regression union result;
- final planning proportionality guidance;
- CI shape unchanged.

Do not infer completion from commit messages alone.

## Workstream B — Standard repository gate

Run the ordinary repository gate exactly as currently documented:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Then run a curated protected union covering:

- provider/canonical payload isolation;
- stream-options no-op and path COW;
- prepared transcode reuse/recompute/retry;
- request/context/body limits;
- cache dialect capability gating/loss/privacy;
- retained compression/cache/DNS behavior after Plan 118;
- routing/failure isolation;
- pre-handoff retry/post-handoff no-retry;
- streaming EOF/cancellation;
- finalization/database ambiguity/crash recovery;
- rehash generation publication/retirement.

Do not run every historical test solely because this is roadmap closure.

## Workstream C — Deterministic copy/walk closure tests

Before relying on process RSS, use focused deterministic/test-local instrumentation to prove the actual expensive paths were removed.

### Native OpenAI streaming

Use a synthetic large request containing:

- a large messages history;
- several tool definitions;
- `stream=true`;
- case 1: `stream_options.include_usage=true` already present;
- case 2: stream options absent or missing `include_usage`.

Verify:

- case 1 uses no full graph deepcopy/rematerialization and no provider encode if no other transform requires it;
- case 1 retains generation zero/original client bytes where applicable;
- case 2 copies only the affected root/path under the Plan 114 contract;
- canonical nested content remains unchanged;
- provider serialization occurs once only when mutation is required.

Test-local monkeypatch/counters/identity checks are acceptable. Remove any instrumentation that would otherwise become production telemetry.

### Prepared transcode

Use a synthetic large cross-protocol request and verify:

- one preflight translation;
- no recursive physical freeze/rematerialization cycle after preflight;
- unchanged prepared body reused directly;
- provider-specific mutation, when forced in a focused case, produces one new provider generation without changing prepared source;
- retry after freeze reuses the same bytes.

### Estimation

Verify call counts/behavior for:

- one canonical model-input estimate;
- one translated estimate when cross-protocol preflight is required;
- no duplicate estimate solely because stream metadata changed;
- correct context-limit rejection at a boundary.

These deterministic tests are the authoritative proof of eliminated copies/walks. RSS measurements are contextual only.

## Workstream D — Target environment record

If an appropriate Raspberry Pi/SBC target is available, record without secrets:

- board/model;
- architecture;
- kernel;
- RAM size from OS tooling;
- Python version;
- EggPool commit;
- storage class if known (`microSD`/MMC, USB SSD, NVMe); do not guess device endurance/model;
- active config profile;
- provider/account count without names/keys if sensitive;
- protocols exercised;
- enabled optional features relevant to Roadmap 113;
- JSON backend (`stdlib`/`orjson`);
- whether DNS/synthetic/tuning were retained and enabled;
- provider connection pool settings.

If no target is available, record target measurements as `not measured` and complete deterministic closure only.

## Workstream E — Representative workload corpus

Use a short fixed corpus. Do not commit private prompts.

### Request A — small native baseline

- same client/provider protocol;
- non-streaming;
- short synthetic prompt;
- compression/cache synthesis disabled unless intentionally testing retained behavior.

Purpose: confirm ordinary request correctness and stable process baseline.

### Request B — large native OpenAI streaming

- OpenAI client to OpenAI-compatible upstream;
- synthetic ASCII/code-heavy history large enough to exercise object-graph ownership materially but below configured body/model limits;
- several tool definitions;
- `stream=true`;
- include a case with already-correct `stream_options.include_usage` if provider accepts it.

Purpose: exercise Plan 114 native/no-op/COW behavior on the intended coding-agent workload.

### Request C — large cross-protocol transcode

- OpenAI→Anthropic or Anthropic→OpenAI as actually configured;
- synthetic messages + tools large enough to exercise PreparedTranscode reuse;
- one current capability mapping supported by selected provider/model;
- cache-extension field only if Plan 117 says the selected provider explicitly supports it.

Purpose: exercise Plan 115 translated-body reuse and Plan 117 provider semantics.

### Request D — small concurrent stream set

Only if live provider/environment supports it:

- 2–4 simultaneous streams;
- use existing scripts/manual calls, not a benchmark framework;
- enough duration to observe sockets/RSS/task cleanup.

Three repeated cycles are sufficient for obvious monotonic-growth detection. This is not a soak test.

## Workstream F — Resource observation

Using standard tools (`ps`, `/proc`, `ss`, `lsof` where available) and existing `eggpool runtime-status --json`, record where practical:

### Idle/stabilized

- RSS;
- process/thread count;
- bounded background task count if runtime-status exposes it;
- open outbound socket count;
- database/WAL size;
- provider pool limits.

### Request B/C

Record contextual samples:

- RSS before request;
- RSS during/after upload/preparation if observable;
- RSS during stream;
- RSS after completion/stabilization;
- CPU utilization during local preparation;
- EggPool `local_pre_upstream`/dispatch timing if already exposed;
- open outbound sockets.

Do not interpret CPython RSS retention as proof that Python objects remain referenced. Deterministic ownership/release tests remain authoritative.

Do not claim pre/post percentage improvement unless the same hardware/config/workload is actually replayed against the planning baseline or a known pre-change commit. Current-state characterization is sufficient.

## Workstream G — Buffer-release/lifecycle observation

Verify existing lifecycle tests and one representative stream show:

- dispatch-only original/prepared/provider payload references are releasable after downstream handoff when retry is impossible;
- stream lease remains active until terminal stream cleanup;
- finalization/usage accounting still completes after dispatch buffers are released;
- client cancellation does not leave the generation/request buffers retained indefinitely;
- repeated identical Request B/C cycles do not show obvious monotonic resource growth attributable to retained request references.

Again, object-reference tests are authoritative over RSS return-to-OS behavior.

## Workstream H — Cache dialect live confidence check

Only when provider credentials and a verified supported capability are available:

- send one first-party-standard cache-field request if applicable;
- send one provider-specific explicit breakpoint request only if Plan 117 verified that provider/model extension;
- verify EggPool does not send extension fields to a generic/unverified target;
- verify no cache key/prompt content is emitted in logs.

Do not use cache-hit billing behavior as the authoritative correctness test because provider caching depends on prefix length/timing/provider state. Deterministic outgoing-payload tests remain authoritative.

If no suitable provider capability is available, record live cache dialect as `not measured`.

## Workstream I — Failure-isolation spot check

Because EggPool's reliability work must survive optimization, include one safe sequence using live traffic or deterministic local upstream:

1. send a request that fails locally or upstream in a request-specific/non-poisoning way;
2. immediately send a valid request through the same running process;
3. verify the valid request succeeds/routes without restart/database reset;
4. verify local transcode/capability failure did not penalize unrelated provider/account health;
5. verify the process remains ready unless the injected failure was deliberately a fatal database ambiguity test (which should remain deterministic-only, not live).

Do not trigger provider credential lockout or abuse/rate-limit traffic deliberately.

## Workstream J — Optional subsystem closure checks

Depending on Plan 118 decisions:

### If DNS cache removed

- verify normal provider resolution/connection/reuse works on target/local runtime;
- verify no stale DNS config is silently accepted;
- inspect task/socket count for unexpected replacement machinery (there should be none).

### If DNS cache retained

- verify disabled SBC profile constructs no custom resolver backend/task;
- if enabled for a justified repro, verify retained behavior without expanding scope.

### If compression tuning removed

- verify no tuning task/state/diagnostic object exists at runtime;
- old tuning-only config fails clearly if removed.

### If synthetic cache removed

- verify native cache translation/pass-through still works;
- segmentation is not run solely for removed synthetic behavior.

### If `granian[pname]` removed

- verify actual foreground startup and a health/proxy smoke with plain Granian installation.

## Workstream K — SQLite/write-wear sanity check

Roadmap 113 should not change database architecture. Confirm that remains true:

- one primary aiosqlite worker/connection in SBC profile;
- WAL + `synchronous=NORMAL` retained;
- low-wear analytics behavior retained unless unrelated config simplification explicitly touched it;
- no new migrations/tables solely for hot-path optimization;
- no new request-time diagnostics writes introduced.

Record database/WAL sizes only as contextual observations. Do not infer flash endurance from a few requests.

## Workstream L — Regression correction rule

If closure finds a defect:

1. reproduce deterministically where possible;
2. identify whether it is caused by Plans 114–119;
3. if small and within scope, correct only that defect and add/update one semantic regression;
4. rerun affected focused tests and ordinary gate;
5. if substantial/unrelated, record it separately rather than expanding Plan 120 into another roadmap.

Do not optimize from noisy one-off RSS/CPU samples.

## Workstream M — Documentation/status closure

After successful verification:

- mark Plans 114–120 complete with truthful closure records;
- mark Plan 113 complete and update its checklist;
- update active architecture/AGENTS/docs only where final ownership/cache/optional-surface behavior differs from current documentation;
- keep planning proportionality guidance from Plan 119;
- do not create a separate performance report if concise observations fit in Plan 120 closure.

## Explicit acceptance criteria

- [ ] Plans 114–119 are complete with implementation SHAs, focused verification, and no unresolved rejection conditions.
- [ ] Ordinary Ruff/Pyright/14-smoke/config gate passes.
- [ ] Curated protected routing/failure-isolation/stream/database/finalization/rehash/transcode/config union passes.
- [ ] Deterministic large native streaming test proves already-correct stream options cause no full graph copy/rematerialization and preserve original-byte/no-op semantics where applicable.
- [ ] Deterministic stream-options insertion test proves only the affected COW path changes and canonical nested content remains unchanged.
- [ ] Deterministic PreparedTranscode test proves no recursive physical freeze/rematerialization cycle remains solely for request-local ownership.
- [ ] Unchanged prepared transcode reuses its encoded body without a second encode.
- [ ] Provider-specific mutation after prepared transcode cannot mutate prepared/canonical source state and serializes one new generation only when needed.
- [ ] Canonical context-input estimation occurs once per canonical request and translated estimate occurs once when needed.
- [ ] Context-limit boundary behavior remains correct.
- [ ] Generic compatible provider cache tests prove no unverified extension field emission.
- [ ] Verified provider-extension mapping remains correct where intentionally supported.
- [ ] Cache keys/request content remain absent from logs/evidence.
- [ ] Optional subsystem/dependency decisions from Plan 118 are verified in actual startup/runtime paths.
- [ ] SQLite one-worker/WAL/NORMAL/durability architecture remains unchanged.
- [ ] Ordinary CI remains one Python 3.11 Ruff/Pyright/smoke job.
- [ ] Retained test corpus/process guidance reflect Plan 119 without introducing numerical gates or planning bureaucracy.
- [ ] If Raspberry Pi/SBC target is available, Request B and Request C are exercised and contextual RSS/CPU/socket/local-preparation observations are recorded.
- [ ] If live provider/target capability is unavailable, each unavailable measurement is explicitly `not measured` rather than extrapolated.
- [ ] Repeated representative cycles show no obvious monotonic request-reference/resource growth; deterministic lifecycle tests prove buffer release regardless of RSS allocator behavior.
- [ ] A request-specific failure followed by a valid request does not poison the running proxy or require restart/database reset.
- [ ] No permanent benchmark, soak, telemetry, hardware-CI, performance threshold, new dependency, or architecture framework is introduced.
- [ ] Plan 113 is marked complete only after all above conditions are satisfied.

## Rejection conditions

Do not close the roadmap if:

- canonical/provider aliasing permits mutation leakage;
- unchanged OpenAI streaming still performs a full request deepcopy/rematerialization solely for `include_usage`;
- PreparedTranscode still performs physical freeze + full mutable rebuild solely for request-local ownership without documented necessity;
- duplicate whole-payload context estimation remains on the ordinary admission path;
- generic providers receive unverified cache-extension fields;
- an optional deleted subsystem is silently replaced by equally complex machinery;
- SQLite/routing/finalization/provider-pool architecture changes without direct closure evidence;
- protected high-severity regressions no longer have direct/stronger coverage;
- CI grows benchmark/full-suite/coverage/hardware gates;
- target values are fabricated, extrapolated, or turned into brittle thresholds.

## Handoff sequence

1. Read Plan 113 and completed Plans 114–119 with closure records.
2. Verify actual implementation commits, not just plan checkboxes.
3. Run ordinary gate and curated protected union.
4. Run deterministic copy/walk/estimate closure tests with test-local instrumentation.
5. If target hardware is available, record environment and run the small Request A–D corpus as applicable.
6. Collect bounded contextual RSS/CPU/socket/DB observations with existing/OS tools only.
7. Spot-check failure isolation and optional subsystem startup behavior.
8. Correct only small in-scope regressions; otherwise record separately.
9. Reconcile active docs/status and mark Plan 113/children complete only when evidence is truthful.
10. Stop. Do not create a follow-on optimization roadmap solely because more micro-optimizations could theoretically exist.
