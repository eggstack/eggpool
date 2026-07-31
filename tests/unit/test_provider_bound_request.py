"""ProviderBoundRequest lifecycle tests."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from eggpool.request.provider_bound_request import (
    PreparedTranscodeValidityKey,
    ProviderBoundRequest,
    SegmentationValidityKey,
    _freeze,
)

# ---------------------------------------------------------------------------
# _freeze helper
# ---------------------------------------------------------------------------


class TestFreeze:
    def test_dict_becomes_mapping_proxy(self) -> None:
        raw: dict[str, object] = {"a": 1, "b": "two"}
        result = _freeze(raw)
        assert isinstance(result, MappingProxyType)
        assert result["a"] == 1
        assert result["b"] == "two"

    def test_list_becomes_tuple(self) -> None:
        result = _freeze([1, "two", 3.0])
        assert isinstance(result, tuple)
        assert result == (1, "two", 3.0)

    def test_nested_structure(self) -> None:
        raw: dict[str, object] = {"messages": [{"role": "user", "content": "hi"}]}
        result = _freeze(raw)
        assert isinstance(result, MappingProxyType)
        msgs = result["messages"]
        assert isinstance(msgs, tuple)
        assert isinstance(msgs[0], MappingProxyType)
        assert msgs[0]["role"] == "user"

    def test_string_passthrough(self) -> None:
        assert _freeze("hello") == "hello"

    def test_int_passthrough(self) -> None:
        assert _freeze(42) == 42

    def test_none_passthrough(self) -> None:
        assert _freeze(None) is None


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

    def test_provider_payload_is_frozen(self) -> None:
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        pbr.set_provider_payload({"messages": [{"role": "user", "content": "hi"}]})
        # The stored payload should be a MappingProxyType
        assert isinstance(pbr._provider_payload, MappingProxyType)

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
        pbr = ProviderBoundRequest(
            client_bytes=b"{}",
            client_payload={"model": "gpt-4"},
            client_protocol="openai",
            model_id="gpt-4",
        )
        first = pbr.serialize_provider_payload()
        second = pbr.serialize_provider_payload()
        assert first == second
        assert pbr.diagnostics.provider_encodes == 1
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
