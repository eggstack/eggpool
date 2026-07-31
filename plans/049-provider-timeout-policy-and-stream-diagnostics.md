# Plan 049 — Provider Timeout Policy and Stream Diagnostics

Date: 2026-07-30
Status: closed at 3b8976d5
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Depends on: Plan 048 protocol completion classification
Planning baseline: `216e615d75269cc1471a920ae81ece9ef2d21802`

## Objective

Make upstream timeout behavior explicit, provider-bound, and evidence-driven. Distinguish first-byte delay, inter-chunk idle timeout, connection/pool/write failure, total stream lifetime, and clean premature EOF so MiniMax direct can be tuned without weakening all providers or masking protocol truncation.

## Problem statement

Provider clients currently use HTTPX connect/read/write/pool timeouts, with a default provider read timeout of 300 seconds. HTTPX read timeout is an inactivity timeout while receiving response data, not an absolute generation duration. The reported MiniMax direct symptom can therefore arise from at least two different conditions:

1. an actual inter-chunk idle period exceeding the read timeout, which raises a timeout exception;
2. a clean premature upstream close, which Plan 048 must classify separately and which no longer timeout can repair.

Without first-class diagnostics, increasing the timeout globally is speculative and can increase recovery latency for genuinely dead streams.

## Ownership boundary

Primary modules:

- `src/eggpool/models/config.py`
- `src/eggpool/providers/client_pool.py`
- `src/eggpool/request/coordinator.py` send/stream timing wrappers
- `src/eggpool/request/stream_diagnostics.py`
- runtime stats/config documentation required to expose the settings
- focused timeout and diagnostics tests

Do not change provider control adaptation, terminal lifecycle ownership, or SSE parser architecture in this phase.

## Required timeout model

### 1. Preserve existing transport controls

Continue to support:

- connect timeout;
- write timeout;
- pool-acquisition timeout;
- keepalive/connection limits.

Existing configuration must remain valid.

### 2. Separate response-header/first-byte and stream-idle semantics

Define explicit provider settings for streaming behavior. A suitable model is:

```toml
[providers.minimax.stream_timeouts]
first_byte_timeout_s = 300
idle_timeout_s = 300
max_lifetime_s = 0
```

The exact nesting may differ, but semantics must be unambiguous:

- `first_byte_timeout_s`: maximum time from successful request send/headers phase to first response payload byte, or clearly define whether response headers are included;
- `idle_timeout_s`: maximum time between successive payload chunks after streaming begins;
- `max_lifetime_s`: optional absolute stream lifetime; `0` or `null` disables it;
- transport `connect_timeout_s`, `write_timeout_s`, and `pool_timeout_s` retain their current meanings.

If HTTPX's client-level `read_timeout_s` remains, document whether it is the lower-level guardrail and how the explicit streaming idle timeout composes with it. Avoid two competing timers with contradictory values.

### 3. Provider-bound defaults and compatibility

- Existing provider config without `stream_timeouts` must retain current effective behavior.
- Do not increase every provider's read/idle timeout.
- MiniMax-specific changes require measured evidence from the diagnostics introduced here.
- Unknown/custom providers inherit conservative defaults equivalent to current behavior.
- Validation must reject non-positive enabled durations, contradictory settings, and unbounded values that exceed a documented safety ceiling unless explicitly allowed.

### 4. Timer implementation

Use monotonic time and cancellation-safe async timeout primitives.

Required states:

- awaiting response headers/send completion;
- awaiting first payload byte;
- streaming with last-byte timestamp;
- terminal event observed;
- terminal job submitted.

Timers must not:

- reset because telemetry code ran;
- include local finalization/database time;
- fire after canonical protocol completion;
- turn client cancellation into provider timeout;
- leave the upstream response unclosed.

### 5. Outcome taxonomy

Record distinct outcomes for:

- `connect_timeout`;
- `pool_timeout`;
- `write_timeout`;
- `response_header_timeout` or the existing send/read-header equivalent;
- `first_byte_timeout`;
- `stream_idle_timeout`;
- `stream_lifetime_timeout`;
- `premature_eof_before_body`;
- `premature_eof_midstream`;
- `remote_protocol_error`;
- `read_error`/`write_error`;
- canonical completion;
- compatibility completion;
- client cancellation.

Do not collapse all timeout subclasses into `TimeoutException` in operator-facing diagnostics.

### 6. Retry/effect policy

- Connect, pool, write, response-header, and first-byte timeout before downstream bytes may remain retryable according to existing bounded policy.
- Stream idle/lifetime timeout after downstream bytes is non-retryable and becomes a midstream error.
- Client cancellation has no provider health penalty.
- Repeated transport/idle timeout may create bounded provider/account cooldown according to typed failure effects.
- Premature EOF remains governed by Plan 048 and must not be mislabeled timeout.
- Database/finalization timeout creates no provider penalty.

### 7. Diagnostics payload

For every stream outcome retain bounded metadata only:

- request ID;
- provider/account/model/protocol;
- outcome class;
- elapsed time;
- first-byte time;
- last-byte-to-failure idle time where applicable;
- bytes emitted/observed;
- terminal marker seen;
- compatibility policy used;
- configured timeout values;
- attempt number;
- upstream request ID when available.

Never persist stream content, API keys, headers with credentials, or arbitrary exception bodies.

### 8. Runtime summary

Expose aggregate counters and bounded rolling latency/idle observations sufficient to answer:

- Which providers time out before first byte?
- Which providers stall midstream?
- Which providers close prematurely without timeout?
- What idle intervals occur near the configured limit?
- Did a provider-specific timeout change reduce errors or merely prolong failures?

Do not add a high-cardinality per-request dashboard table solely for this phase.

## Implementation sequence

### Workstream A — Characterize current behavior

Add deterministic tests proving the current HTTPX read timeout semantics and current outcome mapping. Capture baseline default values from config.

### Workstream B — Configuration model

Add backward-compatible provider stream timeout config with validation and serialization/documentation tests.

### Workstream C — First-byte and idle timers

Implement explicit timers around the streaming iterator, integrated with Plan 048 completion state and Plan 047 terminal ownership.

### Workstream D — Typed diagnostics

Extend stream diagnostics and failure observations without introducing duplicate classification tables. The typed failure-effects classifier remains authoritative for shared-state consequences.

### Workstream E — MiniMax evidence profile

Create a deterministic mock MiniMax profile capable of:

- delayed headers;
- delayed first byte;
- regular long stream beyond 300 seconds in accelerated/unit form;
- idle gap over threshold;
- clean premature EOF;
- canonical completion.

For optional live validation, document a local command that records only bounded timing/outcome metadata and requires explicit credentials. Live validation is supplementary, not the canonical gate.

### Workstream F — Provider tuning decision

Do not change MiniMax defaults until deterministic classification is working. Then document one of:

- no timeout change; symptom was premature EOF;
- MiniMax-specific larger first-byte timeout;
- MiniMax-specific larger idle timeout;
- separate first-byte and idle values;
- permissive-observe completion policy pending more evidence.

The implementation handoff must include the evidence supporting the choice.

## Required tests

### Configuration tests

- old TOML parses with unchanged effective behavior;
- custom first-byte/idle/lifetime values parse;
- invalid zero/negative/contradictory values fail cleanly;
- provider reload/rehash applies new timeout config transactionally;
- provider client/reload generation ownership closes old clients/timers.

### Timing tests

Use a fake clock or very short deterministic timers:

- headers delayed past limit;
- first payload delayed past limit;
- chunks arrive just below idle threshold repeatedly and complete successfully;
- one gap exceeds idle threshold before downstream output;
- one gap exceeds idle threshold after downstream output;
- total lifetime exceeds optional cap despite active chunks;
- canonical terminal arrives immediately before timeout;
- client cancellation wins over timeout classification;
- premature EOF is not timeout.

### Retry/effects tests

- pre-body timeout can retry another eligible account;
- post-body idle timeout never retries;
- timeout effects are applied once;
- provider cooldown is bounded and scoped correctly;
- next unrelated provider request is unaffected;
- database finalization delay does not generate provider timeout/cooldown.

### Diagnostics tests

- each outcome has the correct counter/class;
- configured limits and observed idle duration are recorded;
- content and credentials are absent;
- cardinality/buffer bounds hold under many requests;
- runtime summary can distinguish timeout from premature EOF.

## Acceptance criteria

- [ ] First-byte and inter-chunk idle timeout semantics are explicit and separately configurable per provider.
- [ ] Existing provider configurations preserve their current effective behavior.
- [ ] Premature EOF is never labeled timeout.
- [ ] Client cancellation is never labeled provider timeout.
- [ ] Pre-body timeout retry and post-body no-retry behavior are proven.
- [ ] Timeout subclasses remain distinct in stream diagnostics.
- [ ] Configured timeout values and observed timing are available in bounded diagnostics.
- [ ] No global timeout increase is introduced without evidence.
- [ ] Any MiniMax-specific tuning is documented with measured outcome data.
- [ ] Long active streams are not terminated merely because total duration exceeds the old read timeout when chunks continue within the idle limit.
- [ ] Dead/stalled streams remain bounded by an idle or lifetime guardrail.
- [ ] Rehash/reload applies timeout changes without leaking old timer/client state.
- [ ] Focused tests are deterministic and do not require multi-minute sleeps.

## Explicit rejection conditions

Do not close Plan 049 if:

- the only change is increasing `read_timeout_s`;
- first-byte, idle timeout, and premature EOF share one outcome label;
- timers use wall-clock time;
- timeout after downstream bytes triggers retry;
- provider tuning is based solely on anecdotal live behavior;
- diagnostics store request/response content or credentials;
- the phase introduces a second failure-effects policy table;
- tests rely on real 300-second waits.

## Handoff record

Record:

- implementation commit SHA;
- final config schema/default table;
- old-config compatibility result;
- timeout decision table;
- deterministic timing test matrix;
- MiniMax classification/tuning evidence;
- diagnostics fields and bounds;
- reload/rehash validation commands;
- any provider left in permissive-observe completion mode.

## Definition of done

Plan 049 is complete when Eggpool can tell a slow first byte, an idle stream, a transport failure, a clean premature EOF, a valid long-running stream, and a client cancellation apart; applies provider-specific bounded timeout policy without global speculation; exposes enough safe diagnostics to tune MiniMax rationally; and preserves retry, health, and cleanup correctness.

## Implementation handoff

The implementation adds `ProviderStreamTimeoutConfig` under
`[providers.<id>.stream_timeouts]`. Defaults are unset, preserving the
historical provider `read_timeout_s` behavior. Explicit first-byte and idle
values are bounded to 86,400 seconds and raise the provider HTTPX read
guardrail only for that provider; `max_lifetime_s = 0` disables the absolute
cap. `max_lifetime_s` must not be shorter than `idle_timeout_s`.

The coordinator now prefetches the first non-empty payload under the explicit
first-byte timer, applies monotonic inter-chunk idle and absolute-lifetime
timers, and closes the upstream response on every timeout path. First-byte
timeouts become retryable pre-body attempt failures. Idle and lifetime timeout
after downstream output are midstream failures and never retry. Response-header
timeouts, transport timeout subclasses, protocol completion, premature EOF,
and client cancellation remain separate diagnostics outcomes.

MiniMax defaults were not changed: no live evidence was available, and the
deterministic classification surface is now in place to support a measured
provider-specific decision later. Diagnostics retain only bounded counters,
the last redacted metadata event, configured limits, and timing fields; no
stream content, credentials, or arbitrary exception bodies are persisted.

Local verification before handoff:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run pytest tests/unit/test_config.py tests/unit/test_provider_client_pool.py \
  tests/unit/test_stream_diagnostics.py tests/integration/test_proxy_integration.py \
  tests/integration/test_high_concurrency_streaming.py -q --tb=short --maxfail=1
```
