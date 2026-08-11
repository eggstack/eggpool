"""ProviderBoundRequest — typed lifecycle object for the request payload.

Workstream A of Plan 028.  A ``ProviderBoundRequest`` owns the decoded
payload lifecycle for one proxy request from the moment account selection
completes through to upstream dispatch.  It replaces ad-hoc re-decoding
of duplicated provider-body state across the coordinator, compression,
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
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from eggpool.request.parsed_payload import ParsedRequestPayload


def _owned_json_value(value: Any) -> Any:
    """Materialize one ordinary mutable graph from JSON-like input.

    Prepared transcode results intentionally use mapping proxies and tuples
    while cached.  ``copy.deepcopy`` cannot copy those proxy objects, so the
    provider-bound ownership boundary performs the one required conversion
    directly and normalizes JSON arrays back to lists.
    """
    if isinstance(value, Mapping):
        source: dict[str, Any] = dict(cast("Mapping[str, Any]", value))
        return {key: _owned_json_value(item) for key, item in source.items()}
    if isinstance(value, (list, tuple)):
        items = cast("list[Any] | tuple[Any, ...]", value)
        return [_owned_json_value(item) for item in items]
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


@dataclass(frozen=True, slots=True)
class PayloadMutation:
    """Bounded diagnostic record for one provider-payload generation."""

    generation: int
    reason: str


@dataclass(slots=True)
class ProviderPayloadDiagnostics:
    """Low-cardinality lifecycle counters for tests and request diagnostics."""

    provider_decodes: int = 0
    provider_encodes: int = 0
    generation_changes: int = 0
    transforms_run: int = 0
    transforms_skipped: int = 0
    transforms_mutated: int = 0
    transforms_rejected: int = 0


@dataclass(slots=True)
class ProviderBoundRequest:
    """Lifecycle object owned by one proxy request.

    Created after account selection, the ``ProviderBoundRequest`` carries
    both the original client payload and the (potentially mutated)
    provider-bound payload through the rest of the request lifecycle.
    Callers read from ``provider_payload`` or ``provider_bytes`` instead of
    maintaining a second request-body mirror.

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

    # Decoded provider-bound payload — initially aliases client_payload. Once
    # a transform needs mutation it becomes one detached ordinary dict.
    _provider_payload: dict[str, Any] | None = field(
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
    mutation_log_limit: int = 16
    mutation_log: list[PayloadMutation] = field(
        default_factory=lambda: list[PayloadMutation](), repr=False
    )
    diagnostics: ProviderPayloadDiagnostics = field(
        default_factory=ProviderPayloadDiagnostics, repr=False
    )
    _serialized_generation: int | None = field(default=None, repr=False)
    _frozen: bool = field(default=False, repr=False)

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
        """Store bytes for the current generation.

        This compatibility setter is intentionally not the preferred API;
        callers should use :meth:`serialize_provider_payload` so the
        generation and encode counter cannot drift apart.
        """
        if self._frozen and self._serialized_generation != self.payload_generation:
            raise RuntimeError("provider payload is frozen")
        object.__setattr__(self, "_provider_bytes", body)
        object.__setattr__(self, "_serialized_generation", self.payload_generation)

    def replace_provider_payload(
        self, payload: Mapping[str, Any], *, reason: str
    ) -> bool:
        """Replace the provider payload when its structural content changed."""
        if dict(self.provider_payload) == dict(payload):
            return False
        if self._frozen:
            raise RuntimeError("provider payload is frozen")
        object.__setattr__(self, "_provider_payload", _owned_json_value(payload))
        object.__setattr__(self, "mutated", True)
        object.__setattr__(self, "payload_generation", self.payload_generation + 1)
        object.__setattr__(self, "_provider_bytes", None)
        object.__setattr__(self, "_serialized_generation", None)
        self.diagnostics.generation_changes += 1
        self.mutation_log.append(PayloadMutation(self.payload_generation, reason))
        del self.mutation_log[: -self.mutation_log_limit]
        return True

    def mutate_provider_payload(
        self, mutator: Callable[[dict[str, Any]], None], *, reason: str
    ) -> bool:
        """Copy the current payload, apply ``mutator``, and replace it safely."""
        if self._frozen:
            raise RuntimeError("provider payload is frozen")
        candidate = (
            self._provider_payload
            if self._provider_payload is not None
            else deepcopy(dict(self.client_payload))
        )
        mutator(candidate)
        object.__setattr__(self, "_provider_payload", candidate)
        object.__setattr__(self, "mutated", True)
        object.__setattr__(self, "payload_generation", self.payload_generation + 1)
        object.__setattr__(self, "_provider_bytes", None)
        object.__setattr__(self, "_serialized_generation", None)
        self.diagnostics.generation_changes += 1
        self.mutation_log.append(PayloadMutation(self.payload_generation, reason))
        del self.mutation_log[: -self.mutation_log_limit]
        return True

    def provider_payload_copy(self) -> dict[str, Any]:
        """Return a mutable, detached copy for legacy read/transform callers."""
        return deepcopy(dict(self.provider_payload))

    def release_dispatch_buffers(self) -> None:
        """Drop request graphs and bytes after dispatch can no longer retry."""
        if self._frozen:
            object.__setattr__(self, "_provider_bytes", None)
        object.__setattr__(self, "_provider_payload", None)
        object.__setattr__(self, "client_payload", {})
        object.__setattr__(self, "parsed_payload", None)
        object.__setattr__(self, "client_bytes", b"")

    def set_provider_payload(
        self, payload: Mapping[str, Any], *, increment_generation: bool = True
    ) -> None:
        """Replace the provider-bound payload and bump the generation.

        The new payload is stored as an owned ordinary graph. Callers only
        receive it through the provider-bound API. ``payload_generation`` is
        incremented so
        downstream caches (segmentation, prepared-transcode) can detect
        staleness.
        """
        if not increment_generation and self._frozen:
            raise RuntimeError("provider payload is frozen")
        object.__setattr__(self, "_provider_payload", _owned_json_value(payload))
        object.__setattr__(self, "mutated", True)
        if increment_generation:
            object.__setattr__(self, "payload_generation", self.payload_generation + 1)
            self.diagnostics.generation_changes += 1
            self.mutation_log.append(
                PayloadMutation(self.payload_generation, "set_provider_payload")
            )
            del self.mutation_log[: -self.mutation_log_limit]
        object.__setattr__(self, "_provider_bytes", None)
        object.__setattr__(self, "_serialized_generation", None)

    def serialize_provider_payload(self) -> bytes:
        """Serialize and cache the current generation, then freeze dispatch.

        A body with no provider-bound mutation is already the accepted client
        body, so dispatch reuses those bytes without decoding/re-encoding.
        """
        if (
            self._provider_bytes is not None
            and self._serialized_generation == self.payload_generation
        ):
            return self._provider_bytes
        if not self.mutated:
            body = self.client_bytes
        else:
            from eggpool.jsonx import dumps_bytes

            body = dumps_bytes(self.provider_payload)
            self.diagnostics.provider_encodes += 1
        object.__setattr__(self, "_provider_bytes", body)
        object.__setattr__(self, "_serialized_generation", self.payload_generation)
        object.__setattr__(self, "_frozen", True)
        return body

    @property
    def frozen(self) -> bool:
        """Whether dispatch serialization has frozen this request."""
        return self._frozen

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
