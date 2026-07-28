"""ProviderBoundRequest — typed lifecycle object for the request payload.

Workstream A of Plan 028.  A ``ProviderBoundRequest`` owns the decoded
payload lifecycle for one proxy request from the moment account selection
completes through to upstream dispatch.  It replaces ad-hoc re-decoding
of ``context.upstream_body`` across the coordinator, compression,
synthetic-cache, and transcoder subsystems with a single authoritative
decoded representation.

Design rules
~~~~~~~~~~~~
- ``client_payload`` is **immutable** — transforms never mutate it.
- ``provider_payload`` is produced by the transform pipeline; when no
  transform mutates the payload it **aliases** ``client_payload``
  (zero-copy).
- ``provider_bytes`` is serialized **once** after the last transform.
- A monotonically increasing ``payload_generation`` counter lets
  downstream consumers (segmentation, cache synthesis) invalidate
  stale derived state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.request.parsed_payload import ParsedRequestPayload


def _freeze(value: Any) -> Any:  # noqa: ANN401
    """Return a deeply frozen copy suitable for use as a mapping value."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {k: _freeze(v) for k, v in value.items()}  # type: ignore[misc]
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in cast("list[Any]", value))
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in cast("tuple[Any, ...]", value))
    return value


@dataclass(frozen=True, slots=True)
class SegmentationValidityKey:
    """Determines whether a cached segmentation result can be reused.

    A segmentation is only reused when **all** fields match, meaning the
    payload structure, protocol interpretation, and segmentation policy
    have not changed since the segmentation was computed.
    """

    payload_generation: int
    protocol: str
    segmentation_policy_version: int


@dataclass(frozen=True, slots=True)
class PreparedTranscodeValidityKey:
    """Determines whether a cached prepared-transcode result is valid.

    Replaces the loose ``features_fingerprint`` check with an explicit
    compound key that includes the policy generation, capability
    generation, and feature flags so invalidation is deterministic.
    """

    client_protocol: str
    upstream_protocol: str
    features_fingerprint: str
    policy_generation: int = 0
    capability_generation: int = 0


@dataclass(slots=True)
class ProviderBoundRequest:
    """Lifecycle object owned by one proxy request.

    Created after account selection, the ``ProviderBoundRequest`` carries
    both the original client payload and the (potentially mutated)
    provider-bound payload through the rest of the request lifecycle.
    Callers that previously called ``jsonx_loads(context.upstream_body)``
    now read from ``provider_payload`` or ``provider_bytes`` instead.

    ``client_payload`` is treated as **immutable**.  ``provider_payload``
    initially aliases ``client_payload`` when no mutation has occurred;
    the ``mutated`` flag and ``payload_generation`` counter let consumers
    detect when derived state (segmentation, cache boundaries) must be
    recomputed.
    """

    client_bytes: bytes
    client_payload: Mapping[str, Any]
    client_protocol: str
    model_id: str

    provider_id: str | None = None
    upstream_protocol: str | None = None

    # Decoded provider-bound payload — initially aliases client_payload.
    _provider_payload: Mapping[str, Any] | None = field(
        default=None, repr=False, compare=False, hash=False
    )
    # Serialized provider-bound body — produced once after the last
    # transform.  ``None`` means "not yet serialized".
    _provider_bytes: bytes | None = field(
        default=None, repr=False, compare=False, hash=False
    )

    mutated: bool = False
    payload_generation: int = 0

    # Derived / cached state
    segmentation: Any | None = field(default=None, repr=False)
    segmentation_key: SegmentationValidityKey | None = field(default=None, repr=False)

    # Optional pre-computed parse cache — avoids re-parsing the same
    # body bytes for consumers that need a decoded dict.
    parsed_payload: ParsedRequestPayload | None = field(
        default=None, repr=False, compare=False, hash=False
    )

    @property
    def provider_payload(self) -> Mapping[str, Any]:
        """Return the decoded provider-bound payload.

        When no transform has mutated the original, this **aliases**
        ``client_payload`` (zero-copy).  After mutation it returns the
        separately stored ``_provider_payload``.
        """
        if self._provider_payload is not None:
            return self._provider_payload
        return self.client_payload

    @property
    def provider_bytes(self) -> bytes | None:
        """Return the serialized provider-bound body.

        ``None`` when serialization has not yet occurred — callers must
        serialize via ``jsonx.dumps_bytes(self.provider_payload)`` in
        that case and then call ``set_provider_bytes`` so subsequent
        consumers avoid a duplicate encode.
        """
        return self._provider_bytes

    def set_provider_bytes(self, body: bytes) -> None:
        """Store the serialized provider-bound body (write-once)."""
        object.__setattr__(self, "_provider_bytes", body)

    def set_provider_payload(
        self, payload: Mapping[str, Any], *, increment_generation: bool = True
    ) -> None:
        """Replace the provider-bound payload and bump the generation.

        The new payload is stored as a frozen mapping to prevent
        accidental mutation.  ``payload_generation`` is incremented so
        downstream caches (segmentation, prepared-transcode) can detect
        staleness.
        """
        frozen = cast("Mapping[str, Any]", _freeze(payload))
        object.__setattr__(self, "_provider_payload", frozen)
        object.__setattr__(self, "mutated", True)
        if increment_generation:
            object.__setattr__(self, "payload_generation", self.payload_generation + 1)
        # Invalidate stale serialized bytes — they no longer match the
        # decoded payload.
        object.__setattr__(self, "_provider_bytes", None)

    def segmentation_is_valid(
        self,
        protocol: str,
        segmentation_policy_version: int,
    ) -> bool:
        """Return ``True`` when the cached segmentation can be reused."""
        if self.segmentation is None or self.segmentation_key is None:
            return False
        expected = SegmentationValidityKey(
            payload_generation=self.payload_generation,
            protocol=protocol,
            segmentation_policy_version=segmentation_policy_version,
        )
        return self.segmentation_key == expected

    def mark_segmentation_valid(
        self,
        protocol: str,
        segmentation_policy_version: int,
    ) -> None:
        """Stamp the current validity key after computing segmentation."""
        object.__setattr__(
            self,
            "segmentation_key",
            SegmentationValidityKey(
                payload_generation=self.payload_generation,
                protocol=protocol,
                segmentation_policy_version=segmentation_policy_version,
            ),
        )


# Cast is needed for the frozen MappingProxyType wrapper but is imported
# at the bottom to keep the module's public API clean.
from typing import cast  # noqa: E402
