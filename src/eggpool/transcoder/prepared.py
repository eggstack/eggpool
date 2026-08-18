"""PreparedTranscode — cached preflight translation result for reuse."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from eggpool.jsonx import dumps_str as jsonx_dumps_str

if TYPE_CHECKING:
    from collections.abc import Mapping

    from eggpool.api.proxy_request import TranscodePreflightResult
    from eggpool.transcoder.policy import TranscoderFeatures

RECOMPUTE_REASONS: set[str] = {
    "no_prepared_result",
    "protocol_or_features_mismatch",
    "thinking_controls_present",
    "transcoder_missing",
    "provider_multimodal_capability_required",
}


@dataclass(slots=True)
class PreparedTranscodeDiagnostics:
    """Mutable observability state for a prepared transcode."""

    available: bool = True
    reused: bool = False
    recompute_reason: str | None = None


@dataclass(slots=True, frozen=True)
class PreparedTranscode:
    """Cached result of transcode preflight, reusable in coordinator dispatch.

    When the preflight translation in :func:`_prepare_transcode_preflight`
    produces a valid result, it is attached to the :class:`ProxyRequestContext`
    so the coordinator can skip the duplicate :meth:`encode_request` call.
    The prepared result is only reused when the upstream protocol and
    transcoder features match what the coordinator would use; provider-
    specific thinking budget overrides still require a recompute. The
    translated payload is a request-local logical generation: callers do not
    mutate it through this object. Provider-bound changes must cross the
    provider payload's copy-on-write or conservative ownership boundary.
    Mutable bookkeeping lives on :attr:`diagnostics`.
    """

    client_protocol: str
    upstream_protocol: str
    translated_payload: Mapping[str, Any]
    translated_body: bytes
    warnings: tuple[Mapping[str, Any], ...]
    tool_token_padding: int
    loss_policy_used: str
    features_fingerprint: str = ""
    diagnostics: PreparedTranscodeDiagnostics = field(
        default_factory=PreparedTranscodeDiagnostics,
        compare=False,
        hash=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        # Warning records are bounded metadata. Copy only each warning root so
        # later diagnostic annotation cannot replace the preflight records;
        # recursively rebuilding the translated request is deliberately not
        # part of prepared-transcode construction.
        object.__setattr__(
            self,
            "warnings",
            tuple(dict(warning) for warning in self.warnings),
        )

    @classmethod
    def from_preflight_result(
        cls,
        result: TranscodePreflightResult,
        client_protocol: str,
        loss_policy: str,
        encoded_body: bytes,
        features: TranscoderFeatures | None = None,
    ) -> PreparedTranscode:
        """Create a PreparedTranscode from a preflight result.

        The *features_fingerprint* is a deterministic hash of the
        :class:`TranscoderFeatures` state so the coordinator can verify
        the cached result was produced with the same feature flags.
        """
        return cls(
            client_protocol=client_protocol,
            upstream_protocol=str(result.upstream_protocol),
            translated_payload=result.translated_payload,
            translated_body=encoded_body,
            warnings=tuple(result.warnings),
            tool_token_padding=result.tool_token_padding,
            loss_policy_used=loss_policy,
            features_fingerprint=_features_fingerprint(features),
        )

    def is_valid_for(
        self,
        *,
        upstream_protocol: str,
        features: TranscoderFeatures | None = None,
    ) -> bool:
        """Return True when this prepared result can be reused.

        The prepared transcode is valid when the upstream protocol
        matches and the transcoder features are identical.  Provider-
        specific thinking budget overrides are not checked here — the
        coordinator handles those separately.
        """
        return (
            self.upstream_protocol == upstream_protocol
            and self.features_fingerprint == _features_fingerprint(features)
        )


def _features_fingerprint(features: TranscoderFeatures | None) -> str:
    """Compute a deterministic fingerprint for TranscoderFeatures.

    Returns a short hex digest suitable for comparison.  None/empty
    features produce a sentinel fingerprint so two identical None
    inputs compare equal.
    """
    if features is None:
        return "none"
    raw = jsonx_dumps_str(
        {
            "tools": features.tools,
            "vision": features.vision,
            "thinking": features.thinking,
            "structured_outputs": features.structured_outputs,
            "anthropic_primitives": features.anthropic_primitives,
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
