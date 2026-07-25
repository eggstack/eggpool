"""Reusable mock upstream service for error-isolation reproducer (Plan 023).

Extends the existing mock SSE/provider infrastructure (``respx`` +
``httpx.Response`` with async generators) rather than adding a parallel
test-server framework.  The service supports:

- OpenAI-compatible ``/chat/completions`` and Anthropic-compatible
  ``/messages`` endpoints.
- Streaming (SSE) and non-streaming responses.
- Configurable response status, headers, JSON body, plain-text body,
  delayed headers, delayed body, and connection termination.
- Per-request capture of received model, thinking/reasoning fields,
  request body bytes, and request sequence number.
- Deterministic rule matching by model and request field.
- A structured request log exposed to tests (no log scraping).

MiniMax-M3 scenarios are provided as named presets via
:func:`minimax_thinking_rules`.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import respx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

UPSTREAM_BASE = "https://test-upstream.example.com"

# Endpoint paths
OPENAI_PATH = "/chat/completions"
ANTHROPIC_PATH = "/messages"


# ---------------------------------------------------------------------------
# Response mode
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MockResponseSpec:
    """Declarative description of a mock upstream response.

    Exactly one of ``json_body``, ``text_body``, ``stream_chunks``, or
    ``transport_error`` should be set.  ``status_code`` defaults to 200.
    ``headers`` are merged with sensible defaults.
    """

    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    json_body: dict[str, Any] | None = None
    text_body: str | None = None
    stream_chunks: list[bytes] | None = None
    stream_content_type: str = "text/event-stream"
    transport_error: type[Exception] | None = None
    delay_before_headers_s: float = 0.0
    delay_before_body_s: float = 0.0
    drop_after_headers: bool = False
    request_sequence: int | None = None


# ---------------------------------------------------------------------------
# Request capture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapturedRequest:
    """Structured record of a single upstream request received by the mock.

    Tests assert on these fields directly — never on application logs.
    """

    sequence: int
    url: str
    method: str
    model: str | None
    reasoning_effort: str | None
    thinking_budget_tokens: int | None
    has_thinking_field: bool
    thinking_field_value: Any
    has_reasoning_content: bool
    request_body_bytes: bytes
    headers: dict[str, str]
    timestamp: float


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MockUpstreamRule:
    """A rule that matches incoming requests and produces a response.

    Rules are evaluated in order; the first matching rule wins.  A rule
    matches when all of its non-None matchers match the request.

    ``model`` matches the request's ``model`` field (exact, case-insensitive).
    ``reasoning_effort`` matches the top-level ``reasoning_effort`` field.
    ``has_thinking`` matches whether the request contains a ``thinking``
    object (Anthropic) or ``reasoning`` object (OpenAI nested form).
    ``min_sequence`` / ``max_sequence`` bound the request sequence number.
    ``custom_matcher`` allows arbitrary predicate matching for complex
    scenarios.
    """

    response: MockResponseSpec
    model: str | None = None
    reasoning_effort: str | None = None
    has_thinking: bool | None = None
    min_sequence: int | None = None
    max_sequence: int | None = None
    custom_matcher: Callable[[CapturedRequest], bool] | None = None


# ---------------------------------------------------------------------------
# Helpers for parsing request fields
# ---------------------------------------------------------------------------


def _extract_request_fields(body_bytes: bytes) -> dict[str, Any]:
    """Extract thinking/reasoning fields from a request body.

    Handles both OpenAI top-level ``reasoning_effort`` and nested
    ``reasoning`` forms, as well as Anthropic ``thinking`` with
    ``budget_tokens``.
    """
    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}

    result: dict[str, Any] = {}
    result["model"] = payload.get("model")

    # OpenAI top-level reasoning_effort
    result["reasoning_effort"] = payload.get("reasoning_effort")

    # OpenAI nested reasoning object
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        result["has_reasoning_object"] = True
        result["reasoning_effort_nested"] = reasoning.get("effort")
    else:
        result["has_reasoning_object"] = False

    # Anthropic thinking with budget_tokens
    thinking = payload.get("thinking")
    if isinstance(thinking, dict):
        result["has_thinking"] = True
        result["thinking_budget_tokens"] = thinking.get("budget_tokens")
        result["thinking_type"] = thinking.get("type")
    else:
        result["has_thinking"] = False

    # Historical assistant reasoning_content
    messages = payload.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict) and "reasoning_content" in msg:
                result["has_reasoning_content"] = True
                break

    return result


# ---------------------------------------------------------------------------
# Mock upstream service
# ---------------------------------------------------------------------------


class MockUpstream:
    """Configurable mock upstream service backed by ``respx``.

    Usage::

        upstream = MockUpstream(rules=[...])
        with upstream:
            # make requests through the coordinator
            ...
        assert upstream.requests[0].model == "mini-max-m3"
    """

    def __init__(
        self,
        rules: Sequence[MockUpstreamRule] | None = None,
        base_url: str = UPSTREAM_BASE,
    ) -> None:
        self._base_url = base_url
        self._rules: list[MockUpstreamRule] = list(rules or [])
        self._requests: list[CapturedRequest] = []
        self._sequence_counter = 0
        self._respx_mock: respx.mock | None = None

    @property
    def requests(self) -> list[CapturedRequest]:
        """Return a copy of the captured request log."""
        return list(self._requests)

    @property
    def request_count(self) -> int:
        return len(self._requests)

    def get_request(self, index: int) -> CapturedRequest:
        """Get a specific captured request by index."""
        return self._requests[index]

    def reset(self) -> None:
        """Clear the request log and reset the sequence counter."""
        self._requests.clear()
        self._sequence_counter = 0

    def add_rule(self, rule: MockUpstreamRule) -> None:
        """Append a rule to the end of the rule list."""
        self._rules.append(rule)

    def _build_response(self, spec: MockResponseSpec) -> httpx.Response:
        """Build an ``httpx.Response`` from a ``MockResponseSpec``."""
        headers = dict(spec.headers)
        if spec.stream_chunks is not None:
            headers.setdefault("content-type", spec.stream_content_type)

            async def _stream_gen() -> AsyncIterator[bytes]:
                if spec.delay_before_headers_s > 0:
                    await asyncio.sleep(spec.delay_before_headers_s)
                if spec.delay_before_body_s > 0:
                    await asyncio.sleep(spec.delay_before_body_s)
                for chunk in spec.stream_chunks or []:
                    yield chunk
                if spec.drop_after_headers:
                    raise httpx.RemoteProtocolError("Connection dropped after headers")

            return httpx.Response(
                status_code=spec.status_code,
                headers=headers,
                stream=_stream_gen(),
            )

        if spec.transport_error is not None:
            raise spec.transport_error("Mock transport error")

        if spec.json_body is not None:
            headers.setdefault("content-type", "application/json")
            return httpx.Response(
                status_code=spec.status_code,
                headers=headers,
                json=spec.json_body,
            )

        if spec.text_body is not None:
            headers.setdefault("content-type", "text/plain")
            return httpx.Response(
                status_code=spec.status_code,
                headers=headers,
                content=spec.text_body.encode(),
            )

        return httpx.Response(status_code=spec.status_code, headers=headers)

    def _match_rule(self, captured: CapturedRequest) -> MockUpstreamRule | None:
        """Find the first matching rule for a captured request."""
        for rule in self._rules:
            if rule.min_sequence is not None and captured.sequence < rule.min_sequence:
                continue
            if rule.max_sequence is not None and captured.sequence > rule.max_sequence:
                continue
            if rule.model is not None and (
                captured.model is None or captured.model.lower() != rule.model.lower()
            ):
                continue
            if rule.reasoning_effort is not None and (
                captured.reasoning_effort != rule.reasoning_effort
            ):
                continue
            if rule.has_thinking is not None and (
                captured.has_thinking_field != rule.has_thinking
            ):
                continue
            if rule.custom_matcher is not None and not rule.custom_matcher(captured):
                continue
            return rule
        return None

    def _handler(self, request: httpx.Request) -> httpx.Response:
        """Respx handler: capture the request and return the matched response."""
        self._sequence_counter += 1
        seq = self._sequence_counter

        body_bytes = request.content
        fields = _extract_request_fields(body_bytes)

        captured = CapturedRequest(
            sequence=seq,
            url=str(request.url),
            method=request.method,
            model=fields.get("model"),
            reasoning_effort=fields.get("reasoning_effort"),
            thinking_budget_tokens=fields.get("thinking_budget_tokens"),
            has_thinking_field=fields.get("has_thinking", False),
            thinking_field_value=fields.get("thinking"),
            has_reasoning_content=fields.get("has_reasoning_content", False),
            request_body_bytes=body_bytes,
            headers=dict(request.headers),
            timestamp=time.monotonic(),
        )
        self._requests.append(captured)

        rule = self._match_rule(captured)
        if rule is None:
            # Default: successful empty response
            return httpx.Response(
                status_code=200,
                headers={"content-type": "application/json"},
                json={"id": "mock-default", "object": "chat.completion"},
            )

        return self._build_response(rule.response)

    def __enter__(self) -> MockUpstream:
        self._respx_mock = respx.mock
        self._respx_mock.__enter__()
        # Match any POST to the upstream base URL (both /chat/completions
        # and /messages paths).
        self._respx_mock.post(f"{self._base_url}/chat/completions").mock(
            side_effect=self._handler
        )
        self._respx_mock.post(f"{self._base_url}/messages").mock(
            side_effect=self._handler
        )
        return self

    def __exit__(self, *exc: object) -> None:
        if self._respx_mock is not None:
            self._respx_mock.__exit__(*exc)
        self._respx_mock = None


# ---------------------------------------------------------------------------
# MiniMax-M3 thinking-control scenario presets
# ---------------------------------------------------------------------------

# MiniMax-M3 model identifiers
MINIMAX_M3_NATIVE = "MiniMax-M3"
MINIMAX_M3_QUALIFIED = "MiniMax-M3/opencode-go"

# Thinking effort values
EFFORT_LOW = "low"
EFFORT_MEDIUM = "medium"
EFFORT_HIGH = "high"
EFFORT_XHIGH = "xhigh"
EFFORT_UNKNOWN = "ultra-mega"
EFFORT_NULL = "null"
EFFORT_OMITTED = "omitted"


def _openai_non_stream_success(model: str = MINIMAX_M3_NATIVE) -> MockResponseSpec:
    """Standard non-streaming OpenAI success response."""
    return MockResponseSpec(
        status_code=200,
        json_body={
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1700000000,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hello from MiniMax-M3",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        },
    )


def _openai_stream_success(model: str = MINIMAX_M3_NATIVE) -> MockResponseSpec:
    """Standard streaming OpenAI success response (SSE)."""
    return MockResponseSpec(
        status_code=200,
        stream_chunks=[
            b'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk",'
            b'"choices":[{"index":0,"delta":{"role":"assistant","content":""},'
            b'"finish_reason":null}]}\n\n',
            b'data: {"choices":[{"index":0,"delta":{"content":"Hello"},'
            b'"finish_reason":null}]}\n\n',
            b'data: {"choices":[{"index":0,"delta":{"content":" from "'
            b'MiniMax-M3"},"finish_reason":null}]}\n\n',
            b'data: {"usage":{"prompt_tokens":10,"completion_tokens":5,'
            b'"total_tokens":15},"choices":[]}\n\n',
            b"data: [DONE]\n\n",
        ],
    )


def _thinking_400_error(model: str = MINIMAX_M3_NATIVE) -> MockResponseSpec:
    """HTTP 400 validation error for unsupported thinking level."""
    return MockResponseSpec(
        status_code=400,
        json_body={
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_parameter",
                "message": (
                    "Unsupported thinking level. The 'reasoning_effort' "
                    "parameter 'xhigh' is not supported by this model. "
                    "Supported values: low, medium, high."
                ),
            }
        },
    )


def _thinking_422_error(model: str = MINIMAX_M3_NATIVE) -> MockResponseSpec:
    """HTTP 422 provider-specific validation error for unsupported thinking."""
    return MockResponseSpec(
        status_code=422,
        json_body={
            "error": {
                "type": "unprocessable_entity",
                "code": "invalid_thinking_level",
                "message": (
                    "Unsupported thinking level 'xhigh'. MiniMax-M3 supports "
                    "low, medium, high only."
                ),
                "param": "reasoning_effort",
            }
        },
    )


def _misleading_404_error(model: str = MINIMAX_M3_NATIVE) -> MockResponseSpec:
    """Misleading 404 body containing 'unsupported model' plus thinking explanation."""
    return MockResponseSpec(
        status_code=404,
        json_body={
            "error": {
                "type": "model_not_found",
                "code": "model_not_found",
                "message": (
                    "The model 'MiniMax-M3' does not exist or you do not "
                    "have access to it. Note: the thinking level 'xhigh' "
                    "is also not supported."
                ),
            }
        },
    )


def _unrelated_success(model: str = "gpt-4") -> MockResponseSpec:
    """Successful response for an unrelated model."""
    return _openai_non_stream_success(model=model)


def minimax_scenario_rules(scenario: str) -> list[MockUpstreamRule]:
    """Return rules for a single named MiniMax-M3 scenario.

    Each scenario maps to one of the nine required Workstream A scenarios.
    Tests can use this to set up a focused rule set for a single scenario
    without overlapping matchers.
    """
    if scenario == "no_thinking_success":
        return [
            MockUpstreamRule(
                model=MINIMAX_M3_NATIVE,
                has_thinking=False,
                response=_openai_non_stream_success(MINIMAX_M3_NATIVE),
            )
        ]
    if scenario == "accepted_thinking_success":
        return [
            MockUpstreamRule(
                model=MINIMAX_M3_NATIVE,
                reasoning_effort=EFFORT_LOW,
                response=_openai_non_stream_success(MINIMAX_M3_NATIVE),
            )
        ]
    if scenario == "unsupported_400":
        return [
            MockUpstreamRule(
                model=MINIMAX_M3_NATIVE,
                reasoning_effort=EFFORT_UNKNOWN,
                response=_thinking_400_error(MINIMAX_M3_NATIVE),
            )
        ]
    if scenario == "unsupported_422":
        return [
            MockUpstreamRule(
                model=MINIMAX_M3_NATIVE,
                reasoning_effort=EFFORT_XHIGH,
                response=_thinking_422_error(MINIMAX_M3_NATIVE),
            )
        ]
    if scenario == "misleading_404":
        return [
            MockUpstreamRule(
                model=MINIMAX_M3_NATIVE,
                custom_matcher=lambda r: r.reasoning_effort is None,
                response=_misleading_404_error(MINIMAX_M3_NATIVE),
            )
        ]
    if scenario == "error_then_unrelated_success":
        return [
            MockUpstreamRule(
                min_sequence=1,
                max_sequence=1,
                model=MINIMAX_M3_NATIVE,
                reasoning_effort=EFFORT_UNKNOWN,
                response=_thinking_400_error(MINIMAX_M3_NATIVE),
            ),
            MockUpstreamRule(
                min_sequence=2,
                max_sequence=2,
                model="gpt-4",
                response=_unrelated_success("gpt-4"),
            ),
        ]
    if scenario == "error_then_minimax_success":
        return [
            MockUpstreamRule(
                min_sequence=1,
                max_sequence=1,
                model=MINIMAX_M3_NATIVE,
                reasoning_effort=EFFORT_UNKNOWN,
                response=_thinking_400_error(MINIMAX_M3_NATIVE),
            ),
            MockUpstreamRule(
                min_sequence=2,
                max_sequence=2,
                model=MINIMAX_M3_NATIVE,
                has_thinking=False,
                response=_openai_non_stream_success(MINIMAX_M3_NATIVE),
            ),
        ]
    if scenario == "streaming_rejected":
        return [
            MockUpstreamRule(
                model=MINIMAX_M3_NATIVE,
                reasoning_effort=EFFORT_UNKNOWN,
                response=MockResponseSpec(
                    status_code=400,
                    json_body={
                        "error": {
                            "type": "invalid_request_error",
                            "message": "Unsupported thinking level for streaming.",
                        }
                    },
                ),
            )
        ]
    if scenario == "connection_drop_after_headers":
        return [
            MockUpstreamRule(
                model=MINIMAX_M3_NATIVE,
                reasoning_effort=EFFORT_HIGH,
                response=MockResponseSpec(
                    status_code=200,
                    stream_chunks=[b""],
                    drop_after_headers=True,
                ),
            )
        ]
    msg = f"Unknown scenario: {scenario}"
    raise ValueError(msg)


def minimax_thinking_rules() -> list[MockUpstreamRule]:
    """Return the canonical MiniMax-M3 thinking-control rule set.

    Covers all nine required scenarios from Plan 023 Workstream A:

    1. No thinking field: successful response.
    2. Accepted thinking field/value: successful response.
    3. Unsupported thinking level: HTTP 400 validation error.
    4. Unsupported thinking level rendered as provider-specific HTTP 422.
    5. Misleading model-like HTTP 404 body containing 'unsupported model'
       plus a thinking-field explanation.
    6. Error followed immediately by a successful unrelated model request.
    7. Error followed immediately by a successful MiniMax-M3 request without
       thinking controls.
    8. Streaming request rejected before response bytes.
    9. Connection dropped after response headers but before body read.

    Rules are ordered so that more specific matchers come first.  The
    ``min_sequence`` / ``max_sequence`` bounds on rules 6–7 ensure the
    error-then-success ordering is deterministic.
    """
    rules: list[MockUpstreamRule] = []

    # Scenario 1: No thinking field → success (model matches, no thinking)
    rules.append(
        MockUpstreamRule(
            model=MINIMAX_M3_NATIVE,
            has_thinking=False,
            response=_openai_non_stream_success(MINIMAX_M3_NATIVE),
        )
    )

    # Scenario 2: Accepted thinking field/value → success
    rules.append(
        MockUpstreamRule(
            model=MINIMAX_M3_NATIVE,
            reasoning_effort=EFFORT_LOW,
            response=_openai_non_stream_success(MINIMAX_M3_NATIVE),
        )
    )
    rules.append(
        MockUpstreamRule(
            model=MINIMAX_M3_NATIVE,
            reasoning_effort=EFFORT_MEDIUM,
            response=_openai_non_stream_success(MINIMAX_M3_NATIVE),
        )
    )
    rules.append(
        MockUpstreamRule(
            model=MINIMAX_M3_NATIVE,
            reasoning_effort=EFFORT_HIGH,
            response=_openai_non_stream_success(MINIMAX_M3_NATIVE),
        )
    )

    # Scenario 3: Unsupported thinking level → HTTP 400
    rules.append(
        MockUpstreamRule(
            model=MINIMAX_M3_NATIVE,
            reasoning_effort=EFFORT_UNKNOWN,
            response=_thinking_400_error(MINIMAX_M3_NATIVE),
        )
    )

    # Scenario 4: Unsupported thinking level → HTTP 422
    rules.append(
        MockUpstreamRule(
            model=MINIMAX_M3_NATIVE,
            reasoning_effort=EFFORT_XHIGH,
            response=_thinking_422_error(MINIMAX_M3_NATIVE),
        )
    )

    # Scenario 5: Misleading 404
    rules.append(
        MockUpstreamRule(
            model=MINIMAX_M3_NATIVE,
            reasoning_effort=EFFORT_OMITTED,
            response=_misleading_404_error(MINIMAX_M3_NATIVE),
        )
    )

    # Scenario 6: Error followed by successful unrelated model request
    # (sequence 1 = error, sequence 2 = unrelated success)
    rules.append(
        MockUpstreamRule(
            min_sequence=1,
            max_sequence=1,
            model=MINIMAX_M3_NATIVE,
            reasoning_effort=EFFORT_UNKNOWN,
            response=_thinking_400_error(MINIMAX_M3_NATIVE),
        )
    )
    rules.append(
        MockUpstreamRule(
            min_sequence=2,
            max_sequence=2,
            model="gpt-4",
            response=_unrelated_success("gpt-4"),
        )
    )

    # Scenario 7: Error followed by successful MiniMax-M3 without thinking
    rules.append(
        MockUpstreamRule(
            min_sequence=3,
            max_sequence=3,
            model=MINIMAX_M3_NATIVE,
            reasoning_effort=EFFORT_UNKNOWN,
            response=_thinking_400_error(MINIMAX_M3_NATIVE),
        )
    )
    rules.append(
        MockUpstreamRule(
            min_sequence=4,
            max_sequence=4,
            model=MINIMAX_M3_NATIVE,
            has_thinking=False,
            response=_openai_non_stream_success(MINIMAX_M3_NATIVE),
        )
    )

    # Scenario 8: Streaming request rejected before response bytes
    rules.append(
        MockUpstreamRule(
            model=MINIMAX_M3_NATIVE,
            reasoning_effort=EFFORT_UNKNOWN,
            response=MockResponseSpec(
                status_code=400,
                json_body={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Unsupported thinking level for streaming.",
                    }
                },
            ),
        )
    )

    # Scenario 9: Connection dropped after headers but before body
    rules.append(
        MockUpstreamRule(
            model=MINIMAX_M3_NATIVE,
            reasoning_effort=EFFORT_HIGH,
            response=MockResponseSpec(
                status_code=200,
                stream_chunks=[b""],
                drop_after_headers=True,
            ),
        )
    )

    return rules
