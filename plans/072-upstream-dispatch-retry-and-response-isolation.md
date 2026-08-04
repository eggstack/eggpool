# Plan 072 — Upstream Dispatch, Retry, and Response Isolation

Date: 2026-08-04
Status: ready for implementation
Parent roadmap: `plans/070-failure-resilience-router-recovery-and-sbc-simplification-roadmap.md`
Depends on: `plans/071-attempt-scoped-failure-classification-and-effects.md`
Planning baseline: `e73db213e7e381043cda3cfb8a3dd8109f3f39ca`

## Purpose

Make the request boundary resilient to malformed client input, provider transport failure, unusual HTTP responses, local request-construction faults, response adaptation faults, cancellation, and streaming failure while preserving correct rerouting and terminal ownership.

The goal is a proxy that continues serving unrelated requests after ordinary exceptions. It is not to hide internal defects, retry unsafe requests after handoff, or convert every exception into provider blame.

## Confirmed defects and risks

### 1. Upstream try blocks are too broad

The non-streaming and streaming execution paths catch generic `Exception` around operations that include:

- provider client lookup;
- URL and header preparation;
- `httpx.Request` construction;
- transport send;
- response header/body read;
- local response parsing and portions of adaptation.

Unexpected local exceptions can be wrapped as retryable upstream failures. This can reroute the request and penalize provider accounts for an EggPool defect.

### 2. Retry safety depends on implicit boundaries

The coordinator correctly excludes attempted accounts and avoids midstream retry, but the rules are distributed across `_execute_upstream()`, `_execute_streaming()`, the returned stream generator, and `_handle_exhausted()`.

The contract must be explicit:

- retry only before downstream handoff;
- retry only a different account;
- no in-request sleep;
- no more attempts than both the distinct eligible account count and configured ceiling;
- cleanup of one attempt must converge before the next account is selected.

### 3. Local response adaptation can fail after durable success

The non-streaming path finalizes a completed request before all response transcoding is finished. A response-adaptation exception can therefore make the client receive a proxy error after durable state records completion.

### 4. Unexpected response shapes need bounded handling

Providers can return:

- a nominal 2xx with invalid JSON;
- a 2xx body that does not match the configured protocol;
- malformed or duplicate headers;
- an SSE content type with incomplete frames;
- a streaming request answered as non-stream JSON;
- an error body too large or not decodable;
- a status code with contradictory body evidence.

These must not crash the worker or leave selected-attempt state pending.

### 5. A final request-level exception boundary is needed

Ordinary exceptions should be converted to a stable local error with a proxy request ID after owned cleanup is submitted. The boundary must not swallow `CancelledError`, process termination, or programming failures so broadly that diagnostics disappear.

## Scope

Primary files:

- `src/eggpool/request/coordinator.py`
- `src/eggpool/proxy_request.py`
- `src/eggpool/proxy/client.py`
- `src/eggpool/request/parsed_upstream_response.py`
- `src/eggpool/request/stream_completion.py`
- `src/eggpool/request/stream_diagnostics.py`
- `src/eggpool/transcoder/`
- `src/eggpool/providers/client_pool.py`
- relevant API exception rendering/middleware files.

Focused existing tests:

- request coordinator retry tests;
- provider client pool tests;
- non-streaming and streaming proxy tests;
- transcoder response tests;
- stream completion/EOF tests;
- smoke upstream failure and recovery tests.

## Explicitly out of scope

- hedged requests or simultaneous dispatch to multiple accounts;
- replay after any downstream handoff;
- automatic replay of tool calls based on semantic analysis;
- retry sleeps or request-local exponential backoff;
- changing non-idempotent HTTP method semantics beyond EggPool's existing POST proxy contract;
- buffering an entire streaming response before delivery;
- adding a durable request queue;
- adding provider-specific subprocess isolation;
- catching `BaseException` at the ASGI request boundary;
- suppressing traceback logging for genuine internal defects;
- adding a new HTTP framework or replacing Granian/FastAPI;
- live-provider or chaos tests.

## Governing decisions

1. The Plan 071 decision is the sole retry/effect input.
2. Local preparation faults and provider faults are different categories.
3. `CancelledError` always propagates after terminal ownership is submitted or rejoined.
4. Retry occurs only while `downstream_started` is false.
5. One account is attempted at most once per client request.
6. Failed-attempt durable/runtime cleanup converges before the next account is selected.
7. The existing `routing.max_retries_before_stream` remains the operator safety ceiling; do not add a second retry-count setting.
8. Total attempts are bounded by `min(distinct eligible accounts, 1 + max_retries_before_stream)`.
9. No delay occurs between accounts in the same request.
10. Non-streaming success finalization follows required response adaptation.
11. Streaming cannot be fully validated before handoff; frame and EOF failures after handoff are terminal stream errors, not retry candidates.
12. Unexpected request exceptions return a bounded local error and do not mutate provider health.

## Phase A — Narrow dispatch exception boundaries

### Required changes

Split upstream execution into explicit stages with narrow exception handling:

1. **Local provider preparation**
   - provider/client lookup;
   - URL composition;
   - header construction;
   - provider transform pipeline;
   - request body serialization;
   - `client.build_request()`.
2. **Transport before headers**
   - pool acquisition;
   - DNS/connect/TLS;
   - write;
   - response-header read.
3. **Upstream HTTP classification**
   - status, relevant headers, bounded body evidence.
4. **Response body/stream read**
   - non-stream body read;
   - stream first-byte and idle reads.
5. **Local client-facing response adaptation**
   - response JSON decode when required;
   - transcoder decode/error re-encode;
   - response construction.
6. **Terminal finalization**.

Each stage must produce either a successful value or a typed Plan 071 decision/local invariant error. A generic exception caught at one stage must retain that stage as its source.

### Local failures

These are local/internal unless direct upstream evidence proves otherwise:

- missing provider client after validated configuration;
- invalid composed URL;
- invalid local header value;
- provider transform exception not caused by a classified client capability error;
- request serialization failure;
- `httpx.Request` construction failure;
- response transcoder programming error;
- local JSON encoder failure;
- database/finalization error.

Local failures must:

- not suppress any account/model;
- release or finalize selected ownership;
- render HTTP 500 or 503 according to the existing error hierarchy;
- include `x-proxy-request-id` where a response can still be built;
- log the exception with bounded redacted context.

### Acceptance criteria

- Only HTTPX transport exceptions are classified as transport solely from exception type.
- Request construction and local transform exceptions do not become `_RetryableUpstreamError` provider failures.
- Generic catches are limited to one stage and preserve source.
- `CancelledError` is not converted into an ordinary failure.
- Every selected local-failure path submits terminal cleanup.

## Phase B — Make the retry budget and account traversal explicit

### Required changes

1. At each selection, continue using `context.attempted_accounts` as the distinct-account exclusion set.
2. Derive the request attempt ceiling from the existing config:

```text
configured_total = 1 + max_retries_before_stream
actual_total = min(configured_total, distinct eligible accounts reachable for the request)
```

3. Do not retry an account that already received an upstream dispatch for this request.
4. Do not count a failed pre-dispatch local selection that never claimed an account as a provider attempt.
5. Do count a selected attempt once durable selection and runtime publication have completed, even if request construction later fails; cleanup still belongs to that attempt.
6. A retryable provider decision with another eligible account continues immediately.
7. A retryable provider decision with no remaining account returns the last useful upstream error/response where safe.
8. If the configured ceiling truncates remaining eligible accounts, expose a bounded diagnostic reason such as `attempt_ceiling_reached`; do not add a metrics subsystem.
9. Preserve routing priority tiers and fairness semantics. Retry exclusion must not mutate the global rotor or permanently demote an account beyond the Plan 071/073 effects.
10. The current request must not wait for the failed account's future backoff; it selects another account after cleanup.

### Required status behavior

- No enabled/supported account before dispatch: existing 503 semantics.
- All reachable eligible accounts attempted or safety ceiling reached after upstream failures: existing 502/exhausted semantics, preserving last upstream response when policy permits.
- Client/capability error: 4xx with no provider retry unless the canonical decision explicitly identifies an account-specific provider incompatibility safe for another account.
- Local internal error: 500 or 503, no provider retry by default.

### Acceptance criteria

- One request never dispatches twice to the same account.
- Attempt cleanup fully converges before the next durable selection.
- With three eligible accounts and sufficient ceiling, two failures can route to the third account.
- With more eligible accounts than the ceiling, dispatch stops at the configured ceiling and reports that reason diagnostically.
- No sleep or jitter occurs inside the request retry loop.
- Priority and fairness behavior remain unchanged for each fresh selection excluding attempted accounts.

## Phase C — Move non-streaming terminal success after response adaptation

### Required changes

1. Read and bound the upstream body using the existing HTTPX response handling.
2. Parse once through `ParsedUpstreamResponse`.
3. Extract usage and provider cost as currently done.
4. Perform any required client-facing success response adaptation before durable `COMPLETED` finalization.
5. If adaptation succeeds:
   - finalize `COMPLETED`;
   - clear matching transient backoff;
   - return the adapted response.
6. If adaptation fails because the upstream response is malformed or violates the provider protocol:
   - create a provider-attributable protocol/malformed-response decision only when the malformed upstream evidence is clear;
   - otherwise classify as local adapter failure;
   - finalize the selected attempt/request with the truthful error outcome;
   - do not report durable completion.
7. Preserve upstream response pass-through for native protocol paths where invalid JSON is allowed by the current API contract. Do not force JSON validation merely for usage extraction.
8. Do not parse the body twice.
9. Do not persist raw malformed body content.

### Error response adaptation

- Upstream 4xx/5xx error re-encoding must occur before the final client response is returned.
- Failure to re-encode an error should fall back to a bounded safe generic error or original filtered upstream body according to the existing loss/security policy.
- It must not crash the worker or skip terminal cleanup.

### Acceptance criteria

- A response-transcoder exception cannot leave the durable request marked completed.
- Native pass-through 2xx with non-JSON content retains existing behavior where supported.
- Transcoded protocol 2xx with malformed required structure returns a truthful proxy/upstream protocol error.
- Usage extraction failure alone remains nonfatal and does not change a successful response.
- The response body is decoded at most once on the normal path.

## Phase D — Preserve safe streaming handoff semantics

### Required changes

1. Before returning the stream iterator, classify:
   - provider client preparation;
   - connect/write/header timeout;
   - upstream HTTP error status;
   - configured first-byte timeout when prefetch is enabled.
2. These pre-handoff failures may reroute to another account.
3. Once the downstream stream iterator has begun response delivery, set and preserve the explicit `downstream_started` fact.
4. After handoff:
   - do not reroute;
   - finalize `MIDSTREAM_ERROR`, `CLIENT_CANCELLED`, or `COMPLETED` through the retained owner;
   - close the upstream response in `finally`;
   - release runtime ownership exactly once.
5. Keep strict/compatible/permissive EOF policy behavior.
6. A streaming request answered with a complete non-SSE body retains the current bounded compatibility path.
7. A streaming transcoder frame exception is local unless it is a direct consequence of malformed provider framing; either way, it is terminal after handoff.
8. Do not synthesize a success terminal frame after malformed/premature EOF.
9. Do not add an absolute maximum stream lifetime.

### Acceptance criteria

- Connect/header/first-byte failure before handoff can route to another account.
- Idle timeout after some downstream bytes never reroutes.
- Premature EOF never clears transient backoff as success.
- Client cancellation never penalizes provider health.
- Upstream response close occurs on completion, cancellation, malformed EOF, timeout, and local translation error.
- A healthy stream can exceed the ordinary read-timeout duration as long as its idle policy is satisfied.

## Phase E — Add a bounded request-level safety boundary

### Required changes

At the ASGI proxy request boundary, add or tighten one final exception renderer.

It should catch ordinary `Exception` values not already mapped by the existing hierarchy and:

- log a full traceback server-side with redacted request/provider metadata;
- attempt to join or submit selected terminal cleanup when ownership exists;
- return a stable JSON error with a proxy request ID if no response has started;
- use HTTP 500 for internal invariants and 503 for explicit temporary local unavailability;
- avoid leaking upstream credentials, request bodies, raw provider bodies, filesystem paths, or tracebacks;
- not mutate provider health without a canonical provider-attributable decision.

It must not catch:

- `asyncio.CancelledError` as an ordinary response;
- `KeyboardInterrupt`;
- `SystemExit`;
- other `BaseException` subclasses.

If the downstream response has already started, log and allow the stream to terminate through its existing finalization path; do not attempt to send a second HTTP response.

### Acceptance criteria

- An injected ordinary local exception returns one stable error and the next request is still served.
- The proxy request ID is present in the error response where headers are still available.
- Secrets and raw bodies are absent.
- Provider health is unchanged.
- Cancellation still propagates normally.

## Phase F — Focused regression consolidation

Required representative regressions:

1. first account connect failure, second account success;
2. first and second account 5xx, third account success;
3. no account is attempted twice;
4. configured ceiling stops traversal without attempting all accounts;
5. request construction exception produces local 500/503 and no account penalty;
6. response transcoder exception does not record `COMPLETED`;
7. malformed transcoded 2xx produces truthful terminal state;
8. invalid JSON usage extraction on native pass-through remains nonfatal;
9. first-byte timeout reroutes before handoff;
10. midstream idle timeout does not reroute;
11. client cancellation releases ownership without provider penalty;
12. unexpected local exception does not terminate handling of a subsequent request.

Use existing test modules and simulated transports. No live credentials, sleeps, or multi-process harness.

## Verification

```bash
uv run ruff format src/eggpool/request src/eggpool/proxy src/eggpool/providers src/eggpool/transcoder tests/
uv run ruff check src/eggpool/request src/eggpool/proxy src/eggpool/providers src/eggpool/transcoder tests/
uv run pyright src/eggpool/request src/eggpool/proxy src/eggpool/providers src/eggpool/transcoder
uv run pytest <affected coordinator/proxy/stream/transcoder tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Then run the normal repository formatting/lint/type gate. Do not add a CI job or require live providers.

## Recommended implementation sequence

1. Introduce stage-local helpers without changing behavior.
2. Narrow catches and attach Plan 071 decisions.
3. make distinct-account/attempt-budget behavior explicit and test it.
4. move non-stream response adaptation before terminal success.
5. prove streaming pre/post-handoff behavior.
6. add the final request-level ordinary-exception renderer.
7. consolidate focused regressions.
8. delete obsolete broad catches and comments.
9. run focused checks and smoke.

## Plan acceptance criteria

- [ ] Local preparation failures cannot be labeled as provider transport failures.
- [ ] Retry uses the Plan 071 decision and occurs only before handoff.
- [ ] One request attempts each account at most once.
- [ ] Total attempts respect both distinct candidates and the existing configured ceiling.
- [ ] Failed-attempt cleanup converges before another account is selected.
- [ ] No in-request retry sleep is added.
- [ ] Non-stream response adaptation completes before durable success.
- [ ] Malformed/transcoder failure produces truthful terminal state.
- [ ] Streaming pre-handoff failures can reroute and post-handoff failures cannot.
- [ ] Upstream responses close on every terminal path.
- [ ] An unexpected ordinary request exception cannot terminate proxy service.
- [ ] Local errors do not mutate provider health.
- [ ] Focused regressions and smoke pass.
- [ ] No hedge, replay queue, full-stream buffer, framework replacement, chaos harness, or CI expansion is introduced.

## Rejection conditions

Do not close this plan if:

- a generic local exception is still converted to a provider failure;
- one account can receive two dispatches for one client request;
- cleanup can overlap selection of the next account;
- a retry can occur after downstream handoff;
- a response adaptation error can leave the request durable status completed;
- a midstream failure silently retries or emits a synthetic success marker;
- the safety boundary catches `BaseException` or suppresses cancellation;
- the safety boundary leaks raw request/provider content;
- implementation adds another retry-count configuration or request queue;
- verification becomes a live, soak, or multi-process gate.

## Definition of done

Plan 072 is complete when each request stage has a truthful exception boundary, retryable provider failures traverse distinct accounts only before response handoff, attempt cleanup precedes reselection, non-streaming terminal state matches the actual client-visible response adaptation, streaming failures preserve no-retry-after-handoff semantics, and unexpected ordinary exceptions are safely contained without poisoning provider state or terminating proxy service.