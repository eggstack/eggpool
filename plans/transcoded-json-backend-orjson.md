# JSON Backend Migration Plan for Transcoding Hot Paths

## Context

EggPool's OpenAI/Anthropic proxy path performs JSON work in several latency-sensitive places: request body parsing, upstream body encoding, transcoder preflight, non-streaming response decode/re-encode, streaming SSE data decode, and streaming SSE frame emission. During active transcoded streams, the streaming path serializes one or more JSON SSE frames for many upstream chunks. This is a plausible contributor to event-loop CPU pressure when several streams are active.

This plan evaluates and implements an internal JSON backend migration centered on `orjson`, while preserving protocol behavior and keeping rollback straightforward. It is intentionally separate from `plans/transcoded-stream-dispatch-fixes.md`: duplicate SSE observation, sync feed/flush, and coalesced output should be fixed regardless of the JSON backend.

## Goals

Introduce a centralized JSON helper layer so EggPool can use `orjson` for hot-path JSON serialization/deserialization while retaining a stdlib fallback where appropriate.

Reduce per-chunk serialization overhead for transcoded streaming responses.

Reduce request/response body parse and encode overhead in request-path code.

Avoid bytes/str churn. Prefer byte-returning helpers for wire bodies and SSE frames.

Preserve OpenAI and Anthropic protocol compatibility, error response shapes, usage accounting, and dashboard stats.

Make the migration testable by comparing semantic JSON/SSE events, not raw whitespace.

## Non-goals

Do not change request/response schemas.

Do not change routing, quota, reservation, cost, compression, cache, or model-info semantics.

Do not use hand-rolled JSON byte templates in this pass.

Do not require `orjson` in a way that breaks supported lightweight installs until packaging on Linux aarch64/armv7 deployment targets is verified.

## Design principles

Centralize JSON access. Do not scatter direct `orjson` imports throughout the codebase.

Use bytes at wire boundaries. `orjson.dumps()` returns bytes, which should be treated as an advantage. Avoid `.decode()` followed by `.encode()`.

Preserve compact output. For stdlib fallback, always use `separators=(",", ":")` in byte-oriented helpers.

Keep the fallback deterministic. If `orjson` is unavailable or explicitly disabled during tests, the stdlib path should still pass the same semantic test suite.

Treat behavior differences as compatibility risks. `orjson` is stricter in some cases and has different options from stdlib `json`; every affected API boundary needs tests.

## Phase 1: Add a JSON compatibility module

Create a small module, tentatively `src/eggpool/jsonx.py`, with a narrow API:

```python
from __future__ import annotations

from typing import Any

JsonInput = bytes | bytearray | memoryview | str

USING_ORJSON: bool

def loads(data: JsonInput) -> Any: ...
def dumps_bytes(obj: Any) -> bytes: ...
def dumps_str(obj: Any) -> str: ...  # only for existing DB/log string fields that require str
```

Implementation requirements:

- Prefer `orjson` when installed.
- Fall back to stdlib `json` if unavailable or if an internal test override disables it.
- `dumps_bytes()` must return UTF-8 JSON bytes.
- `dumps_str()` should be used sparingly for DB/log fields that require strings. It may decode `dumps_bytes()`.
- Provide equivalent compact separators in stdlib fallback.
- Keep default output compatible with current API expectations.

Consider an environment override for validation:

```text
EGGPOOL_JSON_BACKEND=stdlib|orjson|auto
```

This is useful for A/B testing and rollback. Default should be `auto` if adding `orjson` as an optional dependency first, or `orjson` if it becomes a required dependency after package validation.

## Phase 2: Decide dependency mode

Start with one of two paths:

Preferred conservative path:

- Add `orjson` as an optional dependency group, for example `[project.optional-dependencies] fast = ["orjson>=..."]`.
- Keep `jsonx` fallback active.
- Document that `pip install eggpool[fast]` enables the faster backend.
- After deployment validation on Raspberry Pi/Ubuntu Linux aarch64 or armv7, promote `orjson` to a normal dependency if install reliability is good.

Preferred performance path if package validation is already acceptable:

- Add `orjson` to normal dependencies.
- Keep fallback only as defensive code and test override.

Because EggPool targets lightweight Raspberry Pi/SBC deployments, validate wheel availability and installation before making `orjson` mandatory. If wheel installation falls back to source builds on common Pi targets, keep it optional until the install path is documented.

## Phase 3: Convert request/response body helpers first

Update `eggpool.request.body.encode_json_body()` to call `jsonx.dumps_bytes()`.

Review all call sites of `encode_json_body()` and ensure they expect bytes. This should cover many response/error/rewrite payloads with minimal code churn.

Replace direct `json.loads(body)` with `jsonx.loads(body)` in request-path code where the input is already bytes:

- `eggpool.api.proxy_request.handle_proxy_request()` request parse.
- `_prepare_transcode_preflight()` only if it parses indirectly through helpers; otherwise leave encode/decode surfaces clear.
- `RequestCoordinator.execute()` recompute path when parsing `context.body_for_upstream`.
- `_execute_non_streaming()` success/error decode paths.
- `_execute_streaming()` stream-options injection path.
- `_build_stream_generator()` include-usage parse should ideally be removed by the stream fixes plan. If it remains, use `jsonx.loads()`.

Do not blindly replace every `json.loads` in the repository. Start with request/transcoder/proxy hot paths; leave config parsing, docs tooling, or DB JSON fields for later unless they sit on dispatch hot paths.

## Phase 4: Convert streaming frame serialization

Update `eggpool.transcoder.streaming._BaseStreamingTranscoder._anthropic_frame()` and `_openai_frame()` to use `jsonx.dumps_bytes()` directly:

```python
def _anthropic_frame(event: str, data: dict[str, Any]) -> bytes:
    return b"event: " + event.encode("ascii") + b"\ndata: " + jsonx.dumps_bytes(data) + b"\n\n"

def _openai_frame(data: dict[str, Any]) -> bytes:
    return b"data: " + jsonx.dumps_bytes(data) + b"\n\n"
```

The event name should be ASCII by construction. If that invariant is not already guaranteed, assert or sanitize it in tests rather than silently changing behavior.

Update any other hot SSE/event helpers that build JSON bytes via `json.dumps(...).encode()`.

Ensure coalescing from the stream-fixes plan still joins bytes without converting through strings.

## Phase 5: Convert streaming SSE JSON parsing

Update `_BaseStreamingTranscoder._safe_json()` to use `jsonx.loads(data)`.

Because SSE parser currently produces `str` data, this remains a str parse unless the parser itself is later made bytes-native. That is acceptable for this pass.

Update `IncrementalSSEObserver._flush_event()` to use `jsonx.loads(data)` for usage-bearing frames.

Keep the existing fast path that skips OpenAI ordinary content chunks without usage. This avoids JSON parsing most common native OpenAI content chunks and should not be removed.

## Phase 6: Convert body transcoders and usage helpers selectively

Inspect modules under `src/eggpool/transcoder/` for direct `json.dumps`/`json.loads` in request/response translation:

- tool-call argument parsing/stringification,
- structured output coercion,
- error re-rendering,
- response decode paths,
- usage conversion helpers,
- ID map diagnostics only if hot.

Replace hot and wire-facing conversions with `jsonx`.

Be careful with places that need a string JSON value, especially OpenAI tool-call `function.arguments`, which is specified as a string containing JSON. For those, use `jsonx.dumps_str()` and add tests to confirm the value remains a string, not bytes.

Do not change warning payload persistence or DB JSON serialization unless the target column expects compact JSON and tests cover it. `jsonx.dumps_str()` is acceptable for DB fields, but this migration is primarily about dispatch/streaming hot paths.

## Phase 7: Compatibility tests

Add backend-parametrized tests where practical:

- Run key JSON helper tests with stdlib fallback forced.
- Run the same tests with `orjson` forced when installed.
- If `orjson` is not installed in a test environment, skip only the forced-orjson tests, not the stdlib semantic tests.

JSON helper tests should cover:

- bytes input to `loads`,
- str input to `loads`,
- dict/list/scalar output from `loads`,
- compact byte output from `dumps_bytes`,
- valid string output from `dumps_str`,
- non-ASCII content round trip,
- invalid JSON error handling as expected by the caller.

Request-path tests should cover:

- invalid request JSON still returns the same protocol-specific error envelope.
- non-dict JSON body still returns 400.
- request too large path remains unchanged.
- OpenAI and Anthropic error response bodies remain valid JSON and protocol-shaped.

Transcoding tests should cover:

- OpenAI-to-Anthropic request translation with tool arguments remains semantically equivalent.
- Anthropic-to-OpenAI request translation emits tool-call `function.arguments` as a JSON string.
- Non-streaming response decode/re-encode remains semantically equivalent.
- Loss warnings remain collected and bounded as before.

Streaming tests should decode emitted SSE events and compare JSON objects, event names, terminal markers, and order. Avoid raw whitespace comparisons.

## Phase 8: E2E capability regression acceptance criteria

All existing request-path, transcoding, dashboard, and performance tests must pass under the default JSON backend.

The following E2E flows must pass with both stdlib fallback and `orjson` backend when available:

- OpenAI client to native OpenAI upstream, non-streaming and streaming.
- Anthropic client to native Anthropic upstream, non-streaming and streaming.
- OpenAI client to Anthropic upstream, non-streaming and streaming.
- Anthropic client to OpenAI upstream, non-streaming and streaming.
- Tool-call streaming in both transcoding directions.
- Thinking/reasoning streaming from Anthropic to OpenAI when enabled.
- OpenAI reasoning controls to Anthropic thinking request when enabled.
- Structured outputs/json schema coercion when enabled.
- Vision/document feature-gated paths if currently tested.
- Non-retryable upstream error re-rendering in the client protocol.
- Retryable upstream error classification remains unchanged.
- Stream cancellation finalizes request and reservation correctly.
- Midstream upstream error records stream diagnostics and does not leak pending requests.

For every completed E2E stream:

- request status is finalized,
- active request count is decremented,
- reservation is released,
- usage counters match fixture expectations,
- cache read/write counters match fixture expectations,
- reasoning/thinking counters match fixture expectations,
- emitted SSE events are parseable by the target protocol client.

## Phase 9: Performance acceptance criteria

Establish baseline before migration using the performance tests from `plans/transcoded-stream-dispatch-fixes.md` or add an equivalent JSON-focused subset.

For streaming transcoder microbenchmarks:

- JSON backend migration should reduce CPU time for high-frame-count translated streams versus stdlib compact JSON.
- Target at least 15-25% improvement in per-1,000-frame translated streaming CPU time after switching frame emission and `_safe_json()` to `orjson`.
- If the stream-fixes plan lands first, compare against that improved baseline rather than the older duplicate-observer baseline.

For request dispatch under 5-8 active transcoded streams:

- p95 fresh dispatch overhead should improve or remain neutral compared with the stream-fixes baseline.
- p99 fresh dispatch overhead must not regress.
- Native request dispatch must remain neutral within normal measurement noise.
- Memory growth during long streams must not increase materially.

For body encoding/decoding microbenchmarks:

- `encode_json_body()` should improve over stdlib for representative OpenAI and Anthropic payloads.
- Streaming frame helpers should allocate fewer intermediate strings/bytes where measurable.

## Phase 10: Packaging and deployment validation

Validate install on supported environments:

- macOS arm64 dev machine,
- Linux x86_64,
- Linux aarch64 if available,
- Raspberry Pi / armv7 or arm64 target if this is part of the intended default install.

Check whether `pip install eggpool` or `pip install eggpool[fast]` pulls a wheel or attempts a source build. If source build is required on common SBC targets, keep `orjson` optional and document the fallback.

Update `pyproject.toml` only after choosing dependency mode.

If optional:

- Add docs noting how to enable the fast JSON backend.
- Ensure `eggpool runtime-status` or a debug log can reveal active JSON backend for diagnostics.

If required:

- Update deployment docs to mention native wheel dependency.
- Confirm installer scripts do not need Rust tooling for normal installs.

## Phase 11: Observability

Add a lightweight diagnostic surface for the active JSON backend:

- startup log: `json_backend=orjson` or `json_backend=stdlib`,
- optional runtime status field,
- optional debug endpoint if consistent with existing runtime stats.

Do not add per-request logs for JSON backend use; that would add noise and overhead.

If a fallback occurs because `orjson` import fails, log once at startup/debug level. Do not warn loudly if optional mode is intended.

## Rollback strategy

Because all call sites use `jsonx`, rollback can be done by setting `EGGPOOL_JSON_BACKEND=stdlib` if the environment override is implemented.

If a specific call site breaks due to bytes/str expectations, revert that call site to stdlib while keeping the rest of the helper migration.

If packaging breaks on target devices, move `orjson` from normal dependencies to optional `[fast]` dependencies and leave `jsonx` fallback active.

If protocol clients fail because they compare raw response whitespace, prefer documenting compact JSON as acceptable API behavior only if those clients are out of spec. If compatibility with that client is operationally necessary, add a targeted compatibility mode rather than reverting the whole migration.

## Suggested implementation sequence

1. Add `jsonx` with stdlib fallback and tests.
2. Convert `encode_json_body()`.
3. Convert streaming frame emission helpers.
4. Convert streaming transcoder `_safe_json()` and usage observer parse path.
5. Convert selected request-path body parses.
6. Convert hot body-transcoder JSON stringification/parsing, especially tool arguments.
7. Add backend observability.
8. Add dependency as optional or required after packaging validation.
9. Run full E2E, performance, and packaging validation.

## Manual validation commands

```bash
ruff check src tests
pyright
pytest -m request_path
pytest -m dashboard
pytest -m performance
pytest tests -k 'transcod or json or stream'
```

Run tests twice if backend override is implemented:

```bash
EGGPOOL_JSON_BACKEND=stdlib pytest -m request_path tests -k 'transcod or json or stream'
EGGPOOL_JSON_BACKEND=orjson pytest -m request_path tests -k 'transcod or json or stream'
```

Then run a local load profile with active transcoded streams and inspect dispatch runtime metrics, request finalization state, and stream diagnostics.

## Deliverables

- `eggpool.jsonx` or equivalent centralized JSON backend module.
- Tests for backend parity and bytes/str behavior.
- Hot-path request/proxy/transcoder call sites migrated to helper APIs.
- Streaming frame emission migrated to byte-native JSON serialization.
- Optional or required `orjson` dependency decision documented.
- Startup/runtime diagnostic for active JSON backend.
- E2E tests covering OpenAI/Anthropic native and transcoded request paths under both JSON backends where available.
- Performance comparison notes or benchmark artifacts showing impact against baseline.
