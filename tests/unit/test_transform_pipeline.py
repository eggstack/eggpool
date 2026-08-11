"""Transform pipeline tests."""

from __future__ import annotations

import json
from typing import Any

import pytest

from eggpool.request.provider_bound_request import ProviderBoundRequest
from eggpool.request.transform_pipeline import (
    TransformContext,
    TransformDecision,
    TransformMeta,
    TransformResult,
    _make_stream_options_adapter,
    run_transform_pipeline,
    serialize_provider_payload,
)


def _make_request(
    payload: dict[str, Any] | None = None,
) -> ProviderBoundRequest:
    payload = payload or {"model": "gpt-4"}
    return ProviderBoundRequest(
        client_bytes=json.dumps(payload, separators=(",", ":")).encode(),
        client_payload=payload,
        client_protocol="openai",
        model_id="gpt-4",
    )


def _make_context(**kwargs: Any) -> TransformContext:
    defaults: dict[str, Any] = {
        "upstream_protocol": "openai",
        "model_id": "gpt-4",
        "request_id": "test-req",
    }
    defaults.update(kwargs)
    return TransformContext(**defaults)


# ---------------------------------------------------------------------------
# TransformMeta
# ---------------------------------------------------------------------------


class TestTransformMeta:
    def test_defaults(self) -> None:
        meta = TransformMeta(name="test_transform")
        assert meta.name == "test_transform"
        assert meta.requires_decoded_payload is True
        assert meta.can_return_unchanged is True
        assert meta.invalidates_segmentation is False
        assert meta.changes_token_estimates is False
        assert meta.may_fail_request is True
        assert meta.diagnostic_category == "passthrough"

    def test_custom_values(self) -> None:
        meta = TransformMeta(
            name="budget_recompute",
            requires_decoded_payload=True,
            can_return_unchanged=False,
            invalidates_segmentation=False,
            changes_token_estimates=True,
            may_fail_request=True,
            diagnostic_category="thinking_budget",
        )
        assert meta.changes_token_estimates is True
        assert meta.diagnostic_category == "thinking_budget"


# ---------------------------------------------------------------------------
# TransformResult
# ---------------------------------------------------------------------------


class TestTransformResult:
    def test_passthrough_default(self) -> None:
        result = TransformResult()
        assert result.decision == TransformDecision.PASSTHROUGH
        assert result.warnings == ()
        assert result.category == "passthrough"

    def test_mutated(self) -> None:
        result = TransformResult(
            decision=TransformDecision.MUTATED,
            warnings=({"kind": "budget_clamped"},),
            category="thinking_budget",
        )
        assert result.decision == TransformDecision.MUTATED
        assert len(result.warnings) == 1


# ---------------------------------------------------------------------------
# run_transform_pipeline
# ---------------------------------------------------------------------------


class TestRunTransformPipeline:
    def test_empty_pipeline(self) -> None:
        request = _make_request()
        context = _make_context()
        result = run_transform_pipeline(request, context, [])
        assert result.transformed is False
        assert result.decisions == ()
        assert result.rejection is None

    def test_passthrough_transform(self) -> None:
        def passthrough(
            req: ProviderBoundRequest, ctx: TransformContext
        ) -> TransformResult:
            return TransformResult()

        meta = TransformMeta(name="noop")
        request = _make_request()
        context = _make_context()
        result = run_transform_pipeline(request, context, [(meta, passthrough)])
        assert result.transformed is False
        assert len(result.decisions) == 1
        assert result.decisions[0].decision == TransformDecision.PASSTHROUGH

    def test_mutated_transform(self) -> None:
        def mutate(req: ProviderBoundRequest, ctx: TransformContext) -> TransformResult:
            req.set_provider_payload({"model": "gpt-4", "stream": True})
            return TransformResult(decision=TransformDecision.MUTATED)

        meta = TransformMeta(name="add_stream")
        request = _make_request()
        context = _make_context()
        result = run_transform_pipeline(request, context, [(meta, mutate)])
        assert result.transformed is True
        assert request.mutated is True
        assert request.provider_payload["stream"] is True

    def test_rejection_short_circuits(self) -> None:
        def reject(req: ProviderBoundRequest, ctx: TransformContext) -> TransformResult:
            return TransformResult(decision=TransformDecision.REJECTED)

        def should_not_run(
            req: ProviderBoundRequest, ctx: TransformContext
        ) -> TransformResult:
            raise AssertionError("Should not be called after rejection")

        meta_reject = TransformMeta(name="rejector")
        meta_late = TransformMeta(name="late")
        request = _make_request()
        context = _make_context()
        result = run_transform_pipeline(
            request, context, [(meta_reject, reject), (meta_late, should_not_run)]
        )
        assert result.rejection is not None
        assert result.rejection.decision == TransformDecision.REJECTED
        assert len(result.decisions) == 1

    def test_multiple_transforms_ordering(self) -> None:
        order: list[str] = []

        def first(req: ProviderBoundRequest, ctx: TransformContext) -> TransformResult:
            order.append("first")
            return TransformResult()

        def second(req: ProviderBoundRequest, ctx: TransformContext) -> TransformResult:
            order.append("second")
            return TransformResult()

        meta1 = TransformMeta(name="first")
        meta2 = TransformMeta(name="second")
        request = _make_request()
        context = _make_context()
        run_transform_pipeline(request, context, [(meta1, first), (meta2, second)])
        assert order == ["first", "second"]

    def test_warnings_accumulated(self) -> None:
        def warn1(req: ProviderBoundRequest, ctx: TransformContext) -> TransformResult:
            req.set_provider_payload({"model": "gpt-4", "warning": True})
            return TransformResult(
                decision=TransformDecision.MUTATED,
                warnings=({"kind": "warning1"},),
            )

        def warn2(req: ProviderBoundRequest, ctx: TransformContext) -> TransformResult:
            return TransformResult(
                warnings=({"kind": "warning2"},),
            )

        meta1 = TransformMeta(name="w1")
        meta2 = TransformMeta(name="w2")
        request = _make_request()
        context = _make_context()
        result = run_transform_pipeline(
            request, context, [(meta1, warn1), (meta2, warn2)]
        )
        assert len(result.warnings) == 2

    def test_mutated_decision_requires_generation_change(self) -> None:
        def dishonest(
            req: ProviderBoundRequest, ctx: TransformContext
        ) -> TransformResult:
            return TransformResult(decision=TransformDecision.MUTATED)

        with pytest.raises(RuntimeError, match="reported mutation"):
            run_transform_pipeline(
                _make_request(),
                _make_context(),
                [(TransformMeta(name="dishonest"), dishonest)],
            )

    def test_stream_options_noop_preserves_original_bytes(self) -> None:
        request = _make_request(
            {
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "large"}],
                "stream_options": {"include_usage": True},
            }
        )
        context = _make_context(
            proxy_context=type(
                "ProxyContext", (), {"streaming": True, "client_metadata": {}}
            )()
        )
        meta, transform = _make_stream_options_adapter()

        result = run_transform_pipeline(request, context, [(meta, transform)])

        assert result.transformed is False
        assert result.decisions[0].decision == TransformDecision.PASSTHROUGH
        assert request.payload_generation == 0
        assert request.serialize_provider_payload() == request.client_bytes

    def test_stream_options_insertion_copies_only_changed_path(self) -> None:
        messages = [{"role": "user", "content": "large"}]
        payload = {"model": "gpt-4", "messages": messages}
        request = _make_request(payload)
        context = _make_context(
            proxy_context=type(
                "ProxyContext", (), {"streaming": True, "client_metadata": {}}
            )()
        )
        meta, transform = _make_stream_options_adapter()

        result = run_transform_pipeline(request, context, [(meta, transform)])

        assert result.transformed is True
        assert request.payload_generation == 1
        assert request.provider_payload["messages"] is messages
        assert payload.get("stream_options") is None
        assert request.provider_payload["stream_options"] == {"include_usage": True}


# ---------------------------------------------------------------------------
# serialize_provider_payload
# ---------------------------------------------------------------------------


class TestSerializeProviderPayload:
    def test_serializes_current_payload(self) -> None:
        request = _make_request({"model": "gpt-4", "messages": []})
        body = serialize_provider_payload(request)
        assert b"gpt-4" in body
        assert b"messages" in body

    def test_returns_cached_bytes_when_available(self) -> None:
        request = _make_request({"model": "gpt-4"})
        request.set_provider_bytes(b'{"model":"gpt-4"}')
        # Even though set_provider_bytes was called, serialize_provider_payload
        # re-serializes from the current payload (since the cache is on the
        # request object, not the function)
        body = serialize_provider_payload(request)
        assert b"gpt-4" in body
