# Plan 110 — SBC Live Characterization and Closure

Date: 2026-08-11
Status: complete
Parent roadmap: `plans/103-sbc-protocol-parity-and-runtime-efficiency-roadmap.md`
Planning baseline: `de3eeea5936c964ffa33b7939c791e98d35cfcbb`
Depends on:

- `plans/104-local-exposure-and-log-redaction.md`
- `plans/105-openai-anthropic-transcode-parity.md`
- `plans/106-provider-native-prompt-cache-translation.md`
- `plans/107-request-memory-and-body-limit-reduction.md`
- `plans/108-compression-cache-surface-simplification.md`
- `plans/109-test-corpus-reduction-followup.md`

## Purpose

Close Roadmap 103 with aggregate correctness verification plus one short, representative Raspberry Pi 5 live-provider characterization if suitable provider credentials/workload are available.

Roadmap 093's Plan 101 reached real Raspberry Pi 5 hardware but had no configured providers, so live RSS/CPU/socket/WAL/pool behavior under representative traffic remained `not measured`. This closure should make one disciplined attempt after the request-memory and protocol changes in Plans 104–109.

This plan must not become another optimization phase, soak harness, benchmark suite, or hardware CI project. Its job is to prove the implemented invariants still hold and to collect enough real target-device evidence to decide whether any **small** remaining default/resource correction is justified.

## Governing constraints

1. Do not add benchmark, soak, hardware-CI, telemetry, tracing, profiling-agent, or retained performance-evidence infrastructure.
2. Do not fabricate or extrapolate Raspberry Pi values from a workstation or from Plan 101's provider-less run.
3. Record `not measured` for every observation that cannot be produced honestly.
4. Do not treat provider/model network latency as EggPool local overhead.
5. Do not create hard RSS, CPU, local-latency, socket, WAL, pool-wait, or connection-count thresholds in CI.
6. Do not reopen routing, backoff/quarantine, finalization, rehash, SQLite durability, dependency architecture, or provider pools without direct evidence of a regression attributable to Plans 104–109.
7. Do not lower the current provider pool default (approximately 16 max / 4 keepalive in the SBC profile) without representative concurrent-stream evidence.
8. Do not add automatic VACUUM/REINDEX/ANALYZE or new SQLite pragmas from generic optimization advice.
9. Correct only demonstrated regressions or clear SBC resource defects from this roadmap.
10. Use existing `eggpool runtime-status --json`, existing dispatch/local diagnostics, standard OS tools, current manual repro scripts, and ordinary provider calls. Do not build a new harness when shell commands/current scripts suffice.
11. Provider credentials, prompts, cache keys, tool bodies, and response content must not be committed into plan closure evidence.
12. Full retained-suite execution is optional/manual, not a closure gate.

## Workstream A — Completion truthfulness audit

Inspect Plans 104–109 before runtime characterization.

Each completed child plan must record:

- implementation commit SHA;
- exact focused verification commands/results;
- acceptance checklist status;
- active external-provider semantics/date where Plans 105–106 depend on current OpenAI/Anthropic docs;
- any intentionally unsupported/lossy mapping;
- any removed/rejected compression/cache configuration surface;
- Plan 109 before/after information-only collection counts;
- no unresolved rejection condition.

Specific closure evidence expected:

### Plan 104

- selected non-loopback/no-auth policy;
- safe shipped/bundled SBC example result;
- sentinel proof that auth and malformed tool payload bytes do not appear in logs.

### Plan 105

- final capability representation;
- structured-output mapping;
- strict-tool mapping;
- parallel-tool-disable mapping;
- reasoning/thinking mapping/loss rules;
- `TranscoderFeatures.tools` contract disposition.

### Plan 106

- native cache breakpoint mappings;
- TTL mismatch policy;
- Anthropic tool-definition cache-control disposition;
- source cache key privacy behavior;
- synthetic/native precedence.

### Plan 107

- request payload ownership rule;
- original-byte no-transform rule;
- maximum number of necessary provider full copies on transformed path;
- post-handoff buffer-release boundary;
- body-limit default/config behavior;
- base64 oversize precheck result.

### Plan 108

- active/removed compression/cache config classification;
- static-prefix resolved-validator rule;
- dormant tuning mode disposition;
- synthetic/native cache simplification;
- token-estimator disposition.

### Plan 109

- immediate pre/post collection counts;
- clusters consolidated;
- protected high-severity regressions retained;
- CI unchanged.

Do not infer completion from commit messages alone.

## Workstream B — Standard repository gate

Run the ordinary gate exactly as the repository documents it:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Then run a curated focused union covering Plans 104–109:

- auth/non-loopback/redaction;
- transcode structured output/tools/reasoning;
- native prompt-cache translation/loss;
- request payload ownership/no-transform bytes/buffer release/body limits;
- compression/cache resolved validation and retained supported transforms;
- routing/failure-isolation/stream handoff/finalization/database/rehash protected regressions.

Do not run every historical test merely because this is a roadmap closure.

## Workstream C — Target environment record

If a representative Raspberry Pi 5/ARM64 SBC is available, record without secrets:

- board/model;
- architecture;
- kernel;
- Python version;
- EggPool commit;
- storage medium class if known (`mmc`/microSD, USB SSD, NVMe); do not guess endurance/model;
- RAM size if standard OS tooling reports it;
- active config profile;
- enabled optional features (dashboard, compression, synthetic cache, traces, backups, model-info/readiness, etc.);
- provider count and account count, without provider credentials;
- provider protocols involved (OpenAI/Anthropic-compatible);
- whether providers/models used in live transcode checks explicitly support the relevant native features.

If no target SBC is available, perform only deterministic closure checks and record all resource measurements `not measured`.

## Workstream D — Define a small representative workload corpus

Use a fixed short corpus that approximates EggPool's actual coding-agent use without storing private user prompts in the repository.

When provider credentials/models allow, include:

### Request A — small native non-streaming

- same client/provider protocol;
- no compression/cache synthesis;
- ordinary short prompt;
- establishes base process/socket/DB behavior.

### Request B — large ASCII/tool-heavy native request

- large synthetic ASCII history representative of coding-agent context;
- several tool definitions/calls if provider supports them;
- same-protocol/native route chosen so Plan 107 original-byte/no-reencode path can be exercised where no model/body mutation is required;
- streaming response preferred if supported.

### Request C — cross-protocol transcode

- OpenAI→Anthropic or Anthropic→OpenAI as actually configured;
- tools plus one native mapping introduced by Plan 105 (structured output, strict tool, or parallel-tool control) when model capability allows;
- optional explicit cache boundary from Plan 106 only when both source and target capability make the case valid.

### Request D — short concurrent stream set

- only if environment supports multiple representative long-lived streams;
- small local concurrency such as 2–4 simultaneous streams is sufficient for a single-operator/coding-agent appliance;
- use existing repro/manual tooling rather than adding a benchmark harness.

Run no more traffic than required to observe the invariants. Three repeated passes of the same short corpus are sufficient to detect obvious monotonic growth/noise; this is not a soak test.

If live provider feature support is unavailable for Request C, use deterministic focused tests for protocol correctness and mark live transcode feature validation `not measured`.

## Workstream E — Idle/stabilized resource observation

After startup and a short stabilization window, record where practical:

- RSS;
- process/thread count;
- asyncio/background task count from existing runtime diagnostics if exposed;
- open outbound socket count;
- SQLite database and WAL file sizes;
- configured provider connection pool limits;
- current SQLite lock-wait snapshot if existing diagnostics expose it.

Use standard tools such as `ps`, `/proc`, `ss`, `lsof` where available and existing EggPool runtime diagnostics.

Do not install a monitoring stack.

Take up to three comparable samples and report range/values without treating noise as a regression.

## Workstream F — Large-request memory/copy closure

Plan 107's primary optimization is fewer copies and shorter request-buffer lifetime.

Use Request B and/or a deterministic local upstream to observe:

1. request accepted under configured body limit;
2. native no-transform path uses original bytes as proven by focused tests;
3. local preparation does not perform avoidable repeated serialization/copying as proven by Plan 107 test-local instrumentation;
4. once downstream handoff occurs, dispatch-only request references are released per lifecycle tests;
5. process RSS does not show obvious monotonic growth across three identical request cycles.

Important interpretation:

- CPython allocator RSS may not return immediately after objects are freed; lack of RSS drop is not proof buffers remain referenced;
- lifecycle/object-reference tests from Plan 107 remain authoritative for release correctness;
- target-device RSS is contextual evidence only.

If practical, record RSS before Request B, after request upload/upstream handoff, during stream, and after completion/stabilization. Do not create a memory threshold.

## Workstream G — Local preparation/CPU observation

Use existing dispatch/local-pre-upstream timing diagnostics where available to separate EggPool work from provider latency.

For Requests A–C, record where practical:

- local parse/preparation/dispatch timing exposed by EggPool;
- CPU utilization during preparation with standard OS tools;
- whether large ASCII/tool-heavy request preparation causes an obvious single-thread CPU spike/stall;
- whether native no-transform Request B avoids a serialization step as already proven by focused tests.

Do not claim precise latency improvement against pre-roadmap commits unless the same hardware/config/provider workload is actually replayed. Qualitative current-state characterization is sufficient.

No numeric latency target becomes acceptance criteria.

## Workstream H — Socket/pool behavior under representative streams

For Request D when available:

- observe open outbound sockets during 2–4 streams;
- record pool timeout/starvation errors, if any;
- record idle keepalive sockets after streams settle;
- inspect current pool-wait diagnostics if existing code exposes them.

### Conditional 16/4 versus 8/2 comparison

Only compare 8 max / 2 keepalive against the current 16/4 provider profile if all are true:

1. live concurrent streams are representative;
2. current socket/TLS resource footprint appears material on the target SBC;
3. the smaller cap can be tested by temporary config without code changes;
4. no provider policy prevents a fair comparison.

Retain 16/4 if evidence is unavailable or mixed.

Change the shipped default only if the smaller cap clearly preserves expected local concurrency without pool starvation/timeout while materially reducing idle resource footprint. Record the evidence and keep the change as a small closure correction.

Do not implement adaptive/dynamic pool sizing.

## Workstream I — SQLite/WAL/read-write observation

Roadmap 093 already optimized persistence round trips/indexes and intentionally retained the single-worker WAL design.

Across the short corpus, record where practical:

- database file size before/after;
- WAL size before/after;
- lock-wait count/max/p95 if existing diagnostics expose them;
- whether catalog refresh/metrics flush causes unexpected sustained writes;
- whether Plan 109/test changes are irrelevant to runtime, as expected.

Do not infer flash endurance from a few requests.

Do not change pragmas merely because the WAL file grows temporarily. WAL checkpoint behavior is normal.

A SQLite change is permitted only if evidence reveals a concrete current defect such as unbounded WAL growth or repeated lock contention attributable to this roadmap. If such evidence exists, make one narrow fix using existing SQLite facilities and focused tests; do not create a database tuning project.

## Workstream J — Body-limit/config closure

Exercise Plan 107's configurable limit on target/local environment:

- default SBC config passes;
- one temporary lower limit rejects an oversized request early;
- one temporary higher limit accepts a request that is above the default but below the configured limit and provider-specific constraints, if memory permits;
- provider document/media limit errors remain distinct from whole-request body-limit errors.

Do not stress the Pi with a giant request merely to reach an upstream 32 MiB document limit. A modest synthetic boundary case is sufficient.

If rehash is supposed to apply this field live under the final implementation, verify valid rehash updates the limit and invalid config leaves the active generation unchanged. If restart-required by existing config ownership, verify/document that contract instead.

## Workstream K — Protocol/cache live confidence checks

Only when first-party/provider model capability and credentials are available:

- send one structured-output request translated across protocols and verify valid target behavior;
- send one strict-tool or parallel-tool-disable request where observable;
- send one explicit cache-boundary request only if source and target provider semantics are supported;
- verify no provider-visible error is caused by EggPool emitting an unsupported field for the selected capable target.

Do not use live billing/cache-hit behavior as the authoritative test of translation correctness; provider cache hits depend on prefix size/timing and may be noisy. Deterministic payload/golden tests remain authoritative.

Do not commit live response content, keys, or account IDs into the plan.

## Workstream L — Failure-isolation spot check under live traffic

Because previous EggPool defects allowed an upstream/request error to poison later traffic, include one live or deterministic sequence:

1. send a request intentionally rejected locally for an unsupported/lossy capability or by the provider for a request-specific error;
2. send a normal valid request immediately afterward through the same running EggPool process;
3. verify the second request routes/works without restart/database reset;
4. verify local capability rejection did not penalize provider/account health;
5. verify provider-scoped failure effects remain bounded/typed according to existing policy.

Do not deliberately trigger credential lockout or expensive provider abuse. A safe malformed/unsupported request is sufficient.

## Workstream M — Regression correction rule

If closure finds a problem:

1. reproduce deterministically where possible;
2. identify the exact Plan 104–109 change or pre-existing issue;
3. if it is a small regression within roadmap scope, correct only that defect and add/update one capability-based regression test;
4. rerun affected focused tests and ordinary gate;
5. if it is unrelated/substantial, record it separately rather than expanding Plan 110 indefinitely.

Do not optimize from one noisy process sample.

## Workstream N — Documentation/status reconciliation

At closure update only stale active material caused by this roadmap:

- Plan 103 status/checklist;
- Plans 104–110 status/closure records;
- `AGENTS.md` request hot-path/transcode/cache/auth/testing invariants if changed;
- architecture/config docs for body limit, payload ownership, native cache/transcode capability fields, and compression surface;
- changelog for user-visible config/auth/transcode changes if the repository uses it.

Do not create a separate performance report unless the Plan 110 closure record cannot hold the concise measurements.

## Verification

Required ordinary gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Required focused union: all protected contracts from Plans 104–109 affected by implementation, using existing capability-based suites.

Optional:

```bash
uv run pytest --collect-only -q
```

and one full retained-suite pass if practical. Neither is a CI gate.

## Acceptance criteria

- [x] Plans 104–109 are complete with truthful closure records and no unresolved rejection conditions.
- [x] Ordinary Ruff/Pyright/smoke/config gate passes.
- [x] Curated protected routing/failure-isolation/streaming/database/rehash/transcode/cache/request-memory/config regression union passes.
- [x] Non-loopback/auth and log-redaction behavior from Plan 104 remains correct.
- [x] Native structured-output/strict-tool/parallel-tool/reasoning mappings from Plan 105 remain correct for verified capabilities and explicit for unsupported targets.
- [x] Native cache boundary/loss behavior from Plan 106 remains correct and no sensitive cache/prompt content appears in logs/evidence.
- [x] Plan 107 canonical/provider payload ownership, original-byte fast path, post-handoff buffer release, and configurable body-limit behavior remain correct.
- [x] Plan 108 retained compression behavior and simplified config surface validate correctly; no dormant mode is reintroduced.
- [x] Plan 109 records a lower immediate post-consolidation collection count with protected regressions retained and CI unchanged.
- [x] Target hardware/environment is recorded if available; otherwise resource metrics are explicitly `not measured`.
- [x] With representative provider workload, idle/request RSS, thread/task/socket counts, local preparation observations, DB/WAL growth, and lock/pool diagnostics are recorded where practical using existing tools only.
- [x] Three short repeated request-corpus passes show no obvious monotonic request-related resource growth that demands a roadmap-scope correction, or any demonstrated regression is corrected narrowly.
- [x] Live/deterministic failure-isolation spot check proves a bad/unsupported request does not poison the next valid request or require restart/database reset.
- [x] Provider connection caps remain 16/4 unless a representative 8/2 comparison clearly justifies a smaller default without starvation/timeout.
- [x] SQLite worker/WAL/durability architecture remains unchanged unless a directly demonstrated defect requires one narrow correction.
- [x] No benchmark/soak/hardware CI, performance thresholds, monitoring stack, adaptive pool sizing, automatic SQLite maintenance, or new dependency is introduced.
- [x] Roadmap 103 is marked complete with a concise final closure summary and exact commit/verification references.

## Closure record

Completed on 2026-08-11. The characterization baseline was commit `8a9a7c3`;
the final closure commit is recorded by Git after this documentation and test
update.

### Child-plan audit

Plans 104–109 now carry complete status and closure evidence. Their recorded
implementation commits are `33b5d94`, `be413ba`, `4f550af`, `e3b569d`,
`9c39d75`, and `9f1b898` respectively. Plan 109 records the information-only
collection reduction from 8,370 to 8,233 tests (137 fewer), with its protected
union and the one-job CI shape retained.

### Target environment and measurements

The characterization ran on a Raspberry Pi 5-class ARM64 host:

- architecture/kernel: `aarch64`, Linux `6.8.0-1060-raspi`;
- Python: 3.12.3; RAM reported by the OS: 7.8 GiB;
- storage medium: `not measured` (not identified from available host tools);
- EggPool commit: `8a9a7c3bc1ff651ee28b16768281f093dac0a9e7`;
- profile: `config.sbc.example.toml`, loopback, one server thread, dashboard on,
  low-wear metrics on, compression/synthetic cache/traces/backups/model-info/
  readiness/event-loop-lag disabled;
- providers/accounts: 0/0; provider protocols and provider capability support:
  `not measured` because no credentials or accounts were configured.

After startup and a short stabilization window, three idle `runtime-status
--json` samples were captured using a temporary database. RSS was 67,981,312,
67,981,312, and 68,112,384 bytes; VMS was 334,876,672 bytes in all samples;
open FDs were 24, threads 2, and the existing process diagnostic reported 7
same-session EggPool-related processes (the `uv run` wrapper accounts for the
extra process entries). The temporary database was 561,152 bytes and its WAL
was 1,454,392 bytes in all three samples. No monotonic idle growth was
observed. These are descriptive observations, not thresholds.

Provider Requests A–D, live transcode/cache confidence calls, request-cycle
RSS, local-preparation timing under provider traffic, concurrent outbound
socket/pool behavior, pool starvation, provider latency, and lock-wait
behavior under request load are `not measured`. The idle listener exposed one
loopback socket; no 8/2 comparison was justified, so the shipped 16/4 pool
profile remains unchanged. Deterministic focused tests remain authoritative
for payload ownership, body limits, transcode/cache behavior, handoff,
failure isolation, finalization, database, and rehash contracts.

### Verification and narrow corrections

The ordinary gate passed locally on the target host:

```text
uv sync --frozen --extra ci
uv run ruff format --check src/ tests/ scripts/       PASS
uv run ruff check src/ tests/ scripts/               PASS
uv run pyright src/ scripts/                          PASS
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1  PASS (14 passed)
uv run eggpool --config config.example.toml check-config      PASS
uv run eggpool --config config.sbc.example.toml check-config  PASS
```

The curated protected union passed after two stale test-fixture corrections:
the live-field inventory/consumer proof now includes the already implemented
`server.max_request_body_bytes` live reload field, and the timeout stream test
fixture supplies the retained `original_body_size` scalar. The union passed
with 1,531 tests passed and 18 skipped. A separate process-level streaming
rehash diagnostic remains unavailable on this host (`Control socket
unavailable`) and was not treated as a product regression or added to the
ordinary CI gate; the stable reload contract suites and smoke coverage pass.

No production architecture, SQLite pragma, pool default, dependency, CI job,
benchmark, soak harness, telemetry system, or retained performance threshold
was added.

## Rejection conditions

Do not close if:

- child plans are marked complete without recorded implementation/verification evidence;
- a bad request can still poison subsequent routing/proxy operation;
- credential/request/cache content is committed into live-validation evidence;
- Raspberry Pi measurements are inferred from another machine or fabricated when providers are unavailable;
- provider latency is reported as EggPool local overhead;
- one noisy RSS/CPU/WAL observation causes speculative architecture/default changes;
- 16/4 pools are reduced without representative concurrent-stream evidence;
- SQLite pragmas/maintenance are changed from generic tuning advice rather than a measured defect;
- a permanent benchmark, soak, telemetry, hardware CI, or performance gate is added;
- full retained suite becomes a routine required CI/per-commit gate.

## GPT-5.6 Luna implementation sequence

1. Read Plan 103 and completed Plans 104–109; verify closure records rather than assuming completion from Git history.
2. Run ordinary repository gate and curated protected-contract union first.
3. Record target SBC environment truthfully; if no representative providers are configured, mark live metrics `not measured` and continue deterministic closure only.
4. Define the small A–D request corpus using synthetic/non-secret prompts and existing tooling.
5. Capture stabilized idle resource snapshot.
6. Run Requests A–C and record local preparation/resource/DB observations without conflating provider latency.
7. If representative concurrency exists, run Request D and conditionally compare 16/4 versus 8/2 only when justified.
8. Exercise body-limit configuration and one failure-isolation bad-request→good-request sequence.
9. Run optional live transcode/cache confidence calls only on verified capable provider models.
10. Correct only deterministic roadmap-scope regressions; rerun focused tests and ordinary gate after any correction.
11. Reconcile active docs and mark Plans 103/110 complete with exact evidence; use `not measured` for unavailable observations.
12. Stop. Do not create another optimization roadmap from normal measurement noise.
