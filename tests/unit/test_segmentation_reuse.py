"""Tests for Plan 028 — Segmentation reuse and invalidation.

Workstream D: segmentation is only reused when payload generation,
protocol, and segmentation policy version are unchanged. A payload
mutation or protocol change must invalidate the cached segmentation.
"""

from __future__ import annotations

from eggpool.request.provider_bound_request import (
    ProviderBoundRequest,
    SegmentationValidityKey,
)


class TestSegmentationValidityKey:
    """Deterministic invalidation via compound key."""

    def test_equal_when_all_fields_match(self) -> None:
        k1 = SegmentationValidityKey(
            payload_generation=1,
            protocol="openai",
            segmentation_policy_version=3,
        )
        k2 = SegmentationValidityKey(
            payload_generation=1,
            protocol="openai",
            segmentation_policy_version=3,
        )
        assert k1 == k2
        assert hash(k1) == hash(k2)

    def test_not_equal_when_generation_differs(self) -> None:
        k1 = SegmentationValidityKey(1, "openai", 3)
        k2 = SegmentationValidityKey(2, "openai", 3)
        assert k1 != k2

    def test_not_equal_when_protocol_differs(self) -> None:
        k1 = SegmentationValidityKey(1, "openai", 3)
        k2 = SegmentationValidityKey(1, "anthropic", 3)
        assert k1 != k2

    def test_not_equal_when_policy_version_differs(self) -> None:
        k1 = SegmentationValidityKey(1, "openai", 3)
        k2 = SegmentationValidityKey(1, "openai", 4)
        assert k1 != k2

    def test_frozen(self) -> None:
        k = SegmentationValidityKey(1, "openai", 3)
        try:
            k.payload_generation = 2  # type: ignore[misc]
        except AttributeError:
            pass
        else:
            raise AssertionError("SegmentationValidityKey should be frozen")


class TestSegmentationReuse:
    """ProviderBoundRequest segmentation validity tracking."""

    def _make_request(self) -> ProviderBoundRequest:
        return ProviderBoundRequest(
            client_bytes=b'{"model":"gpt-4","messages":[]}',
            client_payload={"model": "gpt-4", "messages": []},
            client_protocol="openai",
            model_id="gpt-4",
        )

    def test_no_segmentation_initially(self) -> None:
        pbr = self._make_request()
        assert pbr.segmentation is None
        assert pbr.segmentation_key is None
        assert pbr.segmentation_is_valid("openai", 1) is False

    def test_segmentation_valid_after_marking(self) -> None:
        pbr = self._make_request()
        pbr.segmentation = {"status": "ok"}  # type: ignore[assignment]
        pbr.mark_segmentation_valid("openai", 1)
        assert pbr.segmentation_is_valid("openai", 1) is True

    def test_generation_bump_invalidates(self) -> None:
        pbr = self._make_request()
        pbr.segmentation = {"status": "ok"}  # type: ignore[assignment]
        pbr.mark_segmentation_valid("openai", 1)
        assert pbr.segmentation_is_valid("openai", 1) is True
        # Mutate payload — bumps generation
        pbr.set_provider_payload({"model": "claude"})
        assert pbr.segmentation_is_valid("openai", 1) is False

    def test_protocol_change_invalidates(self) -> None:
        pbr = self._make_request()
        pbr.segmentation = {"status": "ok"}  # type: ignore[assignment]
        pbr.mark_segmentation_valid("openai", 1)
        # Same generation, different protocol
        assert pbr.segmentation_is_valid("anthropic", 1) is False

    def test_policy_version_change_invalidates(self) -> None:
        pbr = self._make_request()
        pbr.segmentation = {"status": "ok"}  # type: ignore[assignment]
        pbr.mark_segmentation_valid("openai", 1)
        # Same generation and protocol, different policy version
        assert pbr.segmentation_is_valid("openai", 2) is False

    def test_remarking_after_mutation_restores_validity(self) -> None:
        pbr = self._make_request()
        pbr.segmentation = {"status": "ok"}  # type: ignore[assignment]
        pbr.mark_segmentation_valid("openai", 1)
        # Mutate and recompute segmentation
        pbr.set_provider_payload({"model": "claude"})
        pbr.segmentation = {"status": "ok_v2"}  # type: ignore[assignment]
        pbr.mark_segmentation_valid("openai", 1)
        assert pbr.segmentation_is_valid("openai", 1) is True

    def test_no_mutation_preserves_validity(self) -> None:
        """When no mutation occurs, segmentation remains valid."""
        pbr = self._make_request()
        pbr.segmentation = {"status": "ok"}  # type: ignore[assignment]
        pbr.mark_segmentation_valid("openai", 1)
        # No mutation — still valid
        assert pbr.segmentation_is_valid("openai", 1) is True
        assert pbr.mutated is False
