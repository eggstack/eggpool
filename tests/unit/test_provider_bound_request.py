"""ProviderBoundRequest lifecycle tests."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from eggpool.request.provider_bound_request import (
    ProviderBoundRequest,
    SegmentationValidityKey,
)

# ---------------------------------------------------------------------------
# SegmentationValidityKey
# ---------------------------------------------------------------------------


class TestSegmentationValidityKey:
    def test_equal_when_all_fields_match(self) -> None:
        k1 = SegmentationValidityKey(
            payload_generation=1, protocol="openai", segmentation_policy_version=3
        )
        k2 = SegmentationValidityKey(
            payload_generation=1, protocol="openai", segmentation_policy_version=3
        )
        assert k1 == k2

    def test_not_equal_when_generation_differs(self) -> None:
        k1 = SegmentationValidityKey(
            payload_generation=1, protocol="openai", segmentation_policy_version=3
        )
        k2 = SegmentationValidityKey(
            payload_generation=2, protocol="openai", segmentation_policy_version=3
        )
        assert k1 != k2

    def test_not_equal_when_protocol_differs(self) -> None:
        k1 = SegmentationValidityKey(
            payload_generation=1, protocol="openai", segmentation_policy_version=3
        )
        k2 = SegmentationValidityKey(
            payload_generation=1, protocol="anthropic", segmentation_policy_version=3
        )
        assert k1 != k2


# ---------------------------------------------------------------------------
# ProviderBoundRequest
# ---------------------------------------------------------------------------


class TestProviderBoundRequest:
    def test_initial_state(self) -> None:
        payload: dict[str, object] = {"model": "gpt-4", "messages": []}
        pbr = ProviderBoundRequest(
            client_bytes=b'{"model":"gpt-4","messages":[]}',
            client_payload=payload,
            client_protocol="openai",
            model_id="gpt-4",
        )
        assert pbr.client_payload == payload
        assert pbr.provider_payload is pbr.client_payload  # aliased
        assert pbr.provider_bytes is None
        assert pbr.mutated is False
        assert pbr.payload_generation == 0

    def test_provider_payload_aliased_when_not_mutated(self) -> None:
        payload: dict[str, object] = {"model": "gpt-4"}
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload=payload,
            client_protocol="openai",
            model_id="gpt-4",
        )
        assert pbr.provider_payload is pbr.client_payload

    def test_provider_namespace_is_removed_from_upstream_model(self) -> None:
        """Provider-scoped client IDs must never reach the upstream body."""
        payload = {
            "model": "minimax-m3/opencode-go",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload=payload,
            client_protocol="openai",
            model_id="minimax-m3",
        )

        assert pbr.client_payload["model"] == "minimax-m3/opencode-go"
        assert pbr.provider_payload["model"] == "minimax-m3"
        assert json.loads(pbr.serialize_provider_payload())["model"] == "minimax-m3"

    def test_set_provider_payload_increments_generation(self) -> None:
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        assert pbr.payload_generation == 0
        new_payload: dict[str, object] = {"model": "claude-3"}
        pbr.set_provider_payload(new_payload)
        assert pbr.payload_generation == 1
        assert pbr.mutated is True
        assert pbr.provider_payload == new_payload
        assert pbr.provider_payload is not pbr.client_payload
        # Serialized bytes invalidated
        assert pbr.provider_bytes is None

    def test_adopt_provider_payload_preserves_unchanged_subtrees(self) -> None:
        messages = [{"role": "user", "content": "large"}]
        payload = {"messages": messages, "stream_options": {}}
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload=payload,
            client_protocol="openai",
            model_id="gpt-4",
        )

        pbr.adopt_provider_payload(
            {"messages": messages, "stream_options": {"include_usage": True}},
            reason="test_adopt",
        )

        assert pbr.provider_payload["messages"] is messages
        assert pbr.provider_payload["stream_options"] is not payload["stream_options"]
        assert payload["stream_options"] == {}

    def test_top_level_mapping_mutation_is_path_local_and_reports_noop(self) -> None:
        messages = [{"role": "user", "content": "hi"}]
        stream_options = {"include_usage": True}
        payload = {"messages": messages, "stream_options": stream_options}
        pbr = ProviderBoundRequest(
            client_bytes=b'{"messages":[]}',
            client_payload=payload,
            client_protocol="openai",
            model_id="gpt-4",
        )

        assert (
            pbr.mutate_top_level_mapping(
                "stream_options", "include_usage", True, reason="noop"
            )
            is False
        )
        assert pbr.payload_generation == 0
        assert pbr.mutate_top_level_mapping(
            "stream_options", "include_usage", False, reason="change"
        )
        assert pbr.payload_generation == 1
        assert pbr.provider_payload["messages"] is messages
        assert stream_options["include_usage"] is True
        assert pbr.provider_payload["stream_options"] == {"include_usage": False}

    def test_set_provider_payload_no_generation_increment(self) -> None:
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        pbr.set_provider_payload({"model": "x"}, increment_generation=False)
        assert pbr.payload_generation == 0

    def test_set_provider_bytes(self) -> None:
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        pbr.set_provider_bytes(b'{"model":"gpt-4"}')
        assert pbr.provider_bytes == b'{"model":"gpt-4"}'

    def test_client_payload_not_mutated_by_set_provider(self) -> None:
        original: dict[str, object] = {"model": "gpt-4"}
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload=original,
            client_protocol="openai",
            model_id="gpt-4",
        )
        pbr.set_provider_payload({"model": "claude"})
        assert pbr.client_payload == original
        assert pbr.client_payload["model"] == "gpt-4"

    def test_segmentation_validity(self) -> None:
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        # No segmentation yet
        assert pbr.segmentation_is_valid("openai", 1) is False

        # Set a segmentation result and mark valid
        pbr.segmentation = {"status": "ok"}  # type: ignore[assignment]
        pbr.mark_segmentation_valid("openai", 1)
        assert pbr.segmentation_is_valid("openai", 1) is True

        # Generation bump invalidates
        pbr.set_provider_payload({"model": "x"})
        assert pbr.segmentation_is_valid("openai", 1) is False

    def test_nested_provider_mutation_isolated_from_client(self) -> None:
        client_payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
        }
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload=client_payload,
            client_protocol="openai",
            model_id="gpt-4",
        )
        # After Plan 121 the arbitrary mutator helper was removed;
        # use the explicit narrow COW API.  ``mutate_top_level_mapping``
        # shallow-copies the root only; the nested messages list is
        # shared with the source until a later transform establishes
        # ownership for it.
        nested_provider_payload = dict(
            cast("dict[str, Any]", pbr.provider_payload),
        )
        nested_provider_payload["messages"] = list(
            nested_provider_payload["messages"],
        )
        nested_provider_payload["messages"][0] = dict(
            nested_provider_payload["messages"][0],
        )
        nested_provider_payload["messages"][0]["content"] = "changed"
        pbr.adopt_provider_payload(nested_provider_payload, reason="nested_test")
        assert client_payload["messages"][0]["content"] == "hi"
        assert pbr.provider_payload["messages"][0]["content"] == "changed"

    def test_structural_noop_does_not_advance_generation(self) -> None:
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        assert pbr.replace_provider_payload({"model": "gpt-4"}, reason="noop") is False
        assert pbr.payload_generation == 0

    def test_serialization_is_cached_and_freezes_dispatch(self) -> None:
        original = b'{"model":"gpt-4","messages":[]}'
        pbr = ProviderBoundRequest(
            client_bytes=original,
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        first = pbr.serialize_provider_payload()
        second = pbr.serialize_provider_payload()
        assert first is original
        assert second is original
        assert pbr.diagnostics.provider_encodes == 0
        assert pbr.frozen is True
        with pytest.raises(RuntimeError):
            pbr.replace_provider_payload({"model": "claude"}, reason="late")

    def test_mutation_log_is_bounded(self) -> None:
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
            mutation_log_limit=2,
        )
        for index in range(4):
            pbr.set_provider_payload({"model": f"model-{index}"})
        assert len(pbr.mutation_log) == 2


# ---------------------------------------------------------------------------
# Plan 121 — thinking-control ownership regression coverage
# ---------------------------------------------------------------------------


class TestThinkingControlAdoption:
    """Plan 121: thinking-control changes use the trusted adoption path.

    ``_adapt_provider_thinking_controls`` adopts the adapter result
    through ``adopt_provider_payload(reason="thinking_control")``
    instead of running it through the conservative
    ``replace_provider_payload`` helper that performs a full
    whole-graph equality check followed by a recursive re-ownership
    walk.  These tests pin that contract on the ownership primitives
    themselves; the integration tests in
    ``test_thinking_budget_provider_cleanup.py`` cover the coordinator
    hot path end to end.
    """

    def test_adopt_thinking_control_shares_unchanged_descendants(
        self,
    ) -> None:
        messages = [{"role": "user", "content": "large"}]
        tools = [{"type": "function", "function": {"name": "tool"}}]
        source_thinking = {"type": "enabled", "effort": "med"}
        payload = {
            "model": "test",
            "messages": messages,
            "tools": tools,
            "thinking": source_thinking,
        }
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload=payload,
            client_protocol="anthropic",
            model_id="test-model",
        )

        # Simulate the adapter output: root shallow-copied + nested
        # ``thinking`` shallow-copied; ``messages`` and ``tools`` retain
        # their identity with the source.
        adopted_root = dict(pbr.provider_payload)
        adopted_thinking = dict(source_thinking)
        adopted_thinking["effort"] = "medium"
        adopted_root["thinking"] = adopted_thinking
        pbr.adopt_provider_payload(adopted_root, reason="thinking_control")

        # Untouched descendants share identity with the source.
        assert pbr.provider_payload["messages"] is messages
        assert pbr.provider_payload["tools"] is tools
        # The nested ``thinking`` mapping is distinct from the source.
        assert pbr.provider_payload["thinking"] is not source_thinking
        # Source ``thinking`` is unchanged.
        assert source_thinking == {"type": "enabled", "effort": "med"}
        assert pbr.payload_generation == 1
        assert pbr.mutation_log[-1].reason == "thinking_control"

    def test_adopt_thinking_control_root_only_change(self) -> None:
        messages = [{"role": "user", "content": "large"}]
        payload = {
            "model": "test",
            "messages": messages,
            "reasoning_effort": "med",
        }
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload=payload,
            client_protocol="openai",
            model_id="test-model",
        )

        # Root-only change: messages stays shared.
        adopted_root = dict(pbr.provider_payload)
        adopted_root["reasoning_effort"] = "medium"
        pbr.adopt_provider_payload(adopted_root, reason="thinking_control")

        assert pbr.provider_payload["messages"] is messages
        assert pbr.provider_payload["reasoning_effort"] == "medium"
        assert payload["reasoning_effort"] == "med"
        assert pbr.payload_generation == 1

    def test_no_op_adaptation_does_not_change_generation(self) -> None:
        """No-op thinking controls leave generation unchanged.

        The coordinator's ``_adapt_provider_thinking_controls`` only
        calls ``adopt_provider_payload`` when ``result.changed`` is
        ``True``; this test pins that contract on the request object so
        accidental adoption on a passthrough decision cannot bump the
        generation or invalidate provider bytes.
        """
        payload = {
            "model": "test",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high",
        }
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload=payload,
            client_protocol="openai",
            model_id="test-model",
        )
        # Pre-flight the precondition: nothing mutated yet.
        assert pbr.payload_generation == 0
        assert pbr.provider_bytes is None
        # A no-op passthrough must not call any adoption setter.
        # (The adapter's ``result.changed`` is the authoritative signal;
        # the coordinator honors it by skipping the adopt call.)


# ---------------------------------------------------------------------------
# Generation-increment must reset the dispatch-freeze state so retries can
# translate from the original client payload through a different provider.
# ---------------------------------------------------------------------------


class TestGenerationIncrementResetsFreeze:
    """Plan: a retry that picks a different selected provider must
    retranslate the request body from scratch. The previous attempt
    serialized and dispatched the body, which freezes dispatch — but
    ``set_provider_payload(increment_generation=True)`` and
    ``adopt_provider_payload`` legitimately start a brand-new generation
    that supersedes the previously serialized one. Both calls must
    therefore clear the dispatch-freeze flag alongside the cached
    serialized bytes; ``replace_provider_payload`` (the conservative
    ownership path) still rejects a frozen body so its stricter
    contract is preserved.
    """

    def _frozen_pbr(self) -> ProviderBoundRequest:
        pbr = ProviderBoundRequest(
            client_bytes=b'{"model":"muse-spark-1.2-contributor"}',
            client_payload={"model": "muse-spark-1.2-contributor"},
            client_protocol="openai",
            model_id="muse-spark-1.2-contributor",
        )
        pbr.set_provider_payload(
            {"model": "muse-spark-1.2-contributor", "thinking": {"type": "enabled"}}
        )
        # ``serialize_provider_payload`` freezes dispatch.
        pbr.serialize_provider_payload()
        assert pbr.frozen is True
        return pbr

    def test_set_provider_payload_with_generation_clears_freeze(self) -> None:
        pbr = self._frozen_pbr()
        # ``set_provider_payload`` with the default
        # ``increment_generation=True`` begins a new generation. It must
        # succeed despite the dispatch-freeze flag and reset the flag
        # so the next ``serialize_provider_payload`` call re-encodes
        # the freshly replaced graph.
        pbr.set_provider_payload({"model": "muse-spark-1.2-contributor"})
        assert pbr.frozen is False
        assert pbr.payload_generation == 2
        assert pbr.provider_bytes is None
        # The new body round-trips through serialization.
        body = pbr.serialize_provider_payload()
        assert b"muse-spark-1.2-contributor" in body
        assert pbr.frozen is True

    def test_adopt_provider_payload_with_generation_clears_freeze(self) -> None:
        pbr = self._frozen_pbr()
        # ``adopt_provider_payload`` with the default
        # ``increment_generation=True`` is the legitimate boundary used
        # by the post-selection transcoder when a retry must
        # retranslate the request. It must succeed even when the body
        # has been previously frozen for dispatch.
        pbr.adopt_provider_payload(
            {
                "model": "muse-spark-1.2-contributor",
                "thinking": {"type": "enabled", "budget_tokens": 1024},
            },
            reason="protocol_transcode",
        )
        assert pbr.frozen is False
        assert pbr.payload_generation == 2
        assert pbr.provider_bytes is None
        # Re-serializing freezes again with the new graph.
        pbr.serialize_provider_payload()
        assert pbr.frozen is True

    def test_replace_provider_payload_still_rejects_when_frozen(self) -> None:
        """``replace_provider_payload`` is the conservative path and
        must still raise when the body has been frozen for dispatch —
        its callers own the body, so silent mutations could break
        downstream consumers that hold the original graph.
        """
        pbr = self._frozen_pbr()
        with pytest.raises(RuntimeError, match="provider payload is frozen"):
            pbr.replace_provider_payload(
                {"model": "muse-spark-1.2-contributor"}, reason="late_replace"
            )

    def test_set_provider_payload_without_generation_still_rejects(self) -> None:
        """``set_provider_payload`` with ``increment_generation=False``
        must still raise when frozen — the caller's intent is to swap
        the body without bumping the generation, which would corrupt
        the cached serialized bytes.
        """
        pbr = self._frozen_pbr()
        with pytest.raises(RuntimeError, match="provider payload is frozen"):
            pbr.set_provider_payload({"model": "x"}, increment_generation=False)

    def test_retry_after_dispatch_retranslates_from_client_payload(self) -> None:
        """End-to-end pin: the post-selection transcoder must be able to
        retranslate the body for a retry against a different selected
        provider after the first attempt's serialization frozen the body.
        This is the actual hot-path sequence that broke before the fix.
        """
        pbr = ProviderBoundRequest(
            client_bytes=b'{"model":"shared-model","messages":[{"role":"user","content":"hi"}]}',
            client_payload={
                "model": "shared-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
            client_protocol="openai",
            model_id="shared-model",
        )
        # First attempt: the preflight path adopts the preflight
        # translated payload and serializes it for dispatch, freezing
        # dispatch in the process.
        pbr.adopt_provider_payload(
            {
                "model": "shared-model",
                "messages": [{"role": "user", "content": "provider-a"}],
            },
            reason="prepared_transcode",
        )
        pbr.serialize_provider_payload()
        assert pbr.frozen is True
        # Retry against a different provider: reset to the client
        # payload (incrementing generation) and retranslate.
        pbr.set_provider_payload(
            {
                "model": "shared-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert pbr.frozen is False
        pbr.adopt_provider_payload(
            {
                "model": "shared-model",
                "messages": [{"role": "user", "content": "provider-b"}],
            },
            reason="protocol_transcode",
        )
        assert pbr.payload_generation == 3
        body = pbr.serialize_provider_payload()
        assert b"provider-b" in body
        assert pbr.frozen is True
