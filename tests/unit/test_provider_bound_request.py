"""ProviderBoundRequest lifecycle tests."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from eggpool.request.provider_bound_request import (
    PreparedTranscodeValidityKey,
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
# PreparedTranscodeValidityKey
# ---------------------------------------------------------------------------


class TestPreparedTranscodeValidityKey:
    def test_equal_when_all_fields_match(self) -> None:
        k1 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="abc123",
            policy_generation=1,
            capability_generation=2,
        )
        k2 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="abc123",
            policy_generation=1,
            capability_generation=2,
        )
        assert k1 == k2

    def test_not_equal_when_fingerprint_differs(self) -> None:
        k1 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="abc",
        )
        k2 = PreparedTranscodeValidityKey(
            client_protocol="openai",
            upstream_protocol="anthropic",
            features_fingerprint="def",
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

    def test_set_provider_payload_accepts_frozen_json_graph(self) -> None:
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"messages": []},
            client_protocol="openai",
            model_id="gpt-4",
        )

        pbr.set_provider_payload(
            MappingProxyType({"messages": (MappingProxyType({"role": "user"}),)})
        )

        assert pbr.provider_payload == {
            "messages": [{"role": "user"}],
        }

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
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
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
        pbr.mutate_provider_payload(
            lambda payload: payload["messages"][0].__setitem__("content", "changed"),
            reason="nested_test",
        )
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
