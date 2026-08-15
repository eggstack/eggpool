"""ProviderBoundRequest — typed lifecycle object for the request payload.

Workstream A of Plan 028.  A ``ProviderBoundRequest`` owns the decoded
payload lifecycle for one proxy request from the moment account selection
completes through to upstream dispatch.  It replaces ad-hoc re-decoding
of duplicated provider-body state across the coordinator, compression, and
transcoder subsystems with a single authoritative
decoded representation.

Design rules
~~~~~~~~~~~~
- ``client_payload`` is **immutable** — transforms never mutate it.
- ``provider_payload`` is produced by the transform pipeline; when no
  transform mutates the payload it **aliases** ``client_payload``
  (zero-copy). Narrow changes use path-level copy-on-write, while unknown
  graphs use the conservative deep-owning path.
- ``provider_bytes`` is serialized **once** after the last transform.
- A monotonically increasing ``payload_generation`` counter lets
  downstream consumers (segmentation and compression) invalidate
  stale derived state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from eggpool.request.parsed_payload import ParsedRequestPayload


def _owned_json_value(value: Any) -> Any:
    """Materialize one ordinary mutable graph from JSON-like input.

    This is the conservative ownership boundary for unknown or externally
    supplied graphs. Trusted EggPool-owned graphs, including prepared
    transcode generations, use :meth:`ProviderBoundRequest.adopt_provider_payload`
    instead and retain their path-level sharing contract.

    The tuple handling remains for compatibility with callers that provide
    immutable JSON-like values outside the prepared-transcode path.
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
    # a transform needs mutation it becomes one detached ordinary dict or an
    # explicitly adopted EggPool-owned graph. Prepared transcode reuse adopts
    # its request-local logical generation and supplies its already-encoded
    # bytes separately; later mutations use the normal COW/owning APIs.
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
        """Conservatively replace a provider payload with owned content."""
        if payload is self.provider_payload or self.provider_payload == payload:
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

    def adopt_provider_payload(
        self,
        payload: Mapping[str, Any],
        *,
        reason: str,
        increment_generation: bool = True,
    ) -> None:
        """Adopt an EggPool-owned provider graph without rematerializing it.

        The caller must supply a graph whose changed ancestors are already
        copied and whose unchanged children are treated as read-only.  This
        is the ownership boundary for path-level transformations such as
        safe compression.  Unknown or externally-owned graphs must use
        :meth:`set_provider_payload` instead.
        """
        if self._frozen:
            raise RuntimeError("provider payload is frozen")
        object.__setattr__(self, "_provider_payload", dict(payload))
        object.__setattr__(self, "mutated", True)
        if increment_generation:
            object.__setattr__(self, "payload_generation", self.payload_generation + 1)
            self.diagnostics.generation_changes += 1
            self.mutation_log.append(PayloadMutation(self.payload_generation, reason))
            del self.mutation_log[: -self.mutation_log_limit]
        object.__setattr__(self, "_provider_bytes", None)
        object.__setattr__(self, "_serialized_generation", None)

    def mutate_top_level_mapping(
        self,
        key: str,
        field: str,
        value: Any,
        *,
        reason: str,
    ) -> bool:
        """Set one field in a top-level mapping with path-local copy-on-write."""
        current = self.provider_payload.get(key)
        if isinstance(current, Mapping):
            if field in current and current[field] == value:
                return False
            candidate = dict(self.provider_payload)
            nested: dict[str, Any] = dict(cast("Mapping[str, Any]", current))
            nested[field] = value
            candidate[key] = nested
        elif current is None:
            candidate = dict(self.provider_payload)
            candidate[key] = {field: value}
        else:
            return False

        self.adopt_provider_payload(candidate, reason=reason)
        return True

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
        """Conservatively replace the provider-bound payload and bump generation.

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
            # Prepared-transcode reuse installs the already-encoded body
            # before the common serialization boundary. Treat a cache hit as
            # the same dispatch freeze as a freshly serialized generation.
            object.__setattr__(self, "_frozen", True)
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
