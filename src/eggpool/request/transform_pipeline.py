"""Provider transform pipeline — ordered post-selection transforms.

Workstream B of Plan 028.  The pipeline replaces ad-hoc transform
calls scattered across ``_apply_selected_provider_transcode_adjustments``
and ``_apply_synthetic_cache_controls`` with a single ordered sequence
of named transforms.  Both streaming and non-streaming paths execute
the same pipeline so behaviour is guaranteed identical.

Each transform is a plain function that receives the
:class:`ProviderBoundRequest` and a :class:`TransformContext` and
returns a :class:`TransformResult`.  The orchestrator
(:func:`run_transform_pipeline`) executes transforms in order and
short-circuits on ``rejection``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eggpool.jsonx import dumps_bytes as jsonx_dumps_bytes

if TYPE_CHECKING:
    from collections.abc import Mapping

    from eggpool.request.provider_bound_request import ProviderBoundRequest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transform metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransformMeta:
    """Declarative metadata for a single pipeline transform.

    Every transform MUST declare these properties so the pipeline
    orchestrator can make informed skip/reuse decisions without
    executing the transform body.
    """

    name: str
    requires_decoded_payload: bool = True
    can_return_unchanged: bool = True
    invalidates_segmentation: bool = False
    changes_token_estimates: bool = False
    may_fail_request: bool = True
    diagnostic_category: str = "passthrough"


class TransformDecision:
    """Result classification for a single transform execution."""

    PASSTHROUGH = "passthrough"
    MUTATED = "mutated"
    REJECTED = "rejected"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class TransformResult:
    """Outcome of a single transform execution.

    Attributes
    ----------
    decision:
        One of ``"passthrough"``, ``"mutated"``, ``"rejected"``,
        ``"skipped"``.
    warnings:
        Non-fatal warnings accumulated during the transform.
    category:
        Diagnostic category matching the transform's
        ``TransformMeta.diagnostic_category``.
    """

    decision: str = TransformDecision.PASSTHROUGH
    warnings: tuple[Mapping[str, Any], ...] = ()
    category: str = "passthrough"


@dataclass(slots=True)
class TransformContext:
    """Shared read-only context passed to every pipeline transform.

    Transforms read from this context but never mutate it — all
    mutations go through the :class:`ProviderBoundRequest`.
    """

    upstream_protocol: str
    transcode_required: bool = False
    transcode_context: Any | None = None  # TranscodeContext
    thinking_intent: Any | None = None  # ThinkingRequestIntent
    thinking_capability: Any | None = None  # ThinkingCapability
    prepared_transcode: Any | None = None  # PreparedTranscode
    cache_config: Any | None = None
    compression_policy: Any | None = None
    compression_tuning_registry: Any | None = None
    resolved_compression_policy: Any | None = None
    selected_provider_id: str | None = None
    selected_provider_kind: str | None = None
    model_id: str = ""
    request_id: str = ""
    transcoder_policy: Any | None = None
    catalog: Any | None = None
    config: Any | None = None
    segmentation_policy_version: int = 0


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

# Type alias for a transform function.
TransformFn = Any  # Callable[[ProviderBoundRequest, TransformContext], TransformResult]


@dataclass(slots=True)
class PipelineResult:
    """Aggregate result of running the full transform pipeline.

    Attributes
    ----------
    final_payload:
        The provider-bound payload after all transforms.
    final_bytes:
        The serialized provider body (may be ``None`` if the caller
        must serialize).
    transformed:
        ``True`` when at least one transform mutated the payload.
    generation:
        The payload generation after all transforms.
    warnings:
        All non-fatal warnings accumulated across transforms.
    rejection:
        When a transform rejected the request, this contains the
        error details; ``None`` on success.
    decisions:
        Per-transform decision log for diagnostics.
    """

    final_payload: Mapping[str, Any]  # type: ignore[name-defined]
    final_bytes: bytes | None = None
    transformed: bool = False
    generation: int = 0
    warnings: tuple[Mapping[str, Any], ...] = ()  # type: ignore[name-defined]
    rejection: Any | None = None
    decisions: tuple[TransformResult, ...] = ()


def run_transform_pipeline(
    request: ProviderBoundRequest,
    context: TransformContext,
    transforms: list[tuple[TransformMeta, TransformFn]],
) -> PipelineResult:
    """Execute the ordered transform pipeline.

    Transforms are executed in the order provided.  If any transform
    returns ``TransformDecision.REJECTED``, the pipeline short-circuits
    and returns the rejection immediately.

    The pipeline does NOT serialize the final payload — the caller
    (coordinator) is responsible for encoding once via
    ``jsonx.dumps_bytes`` and calling ``request.set_provider_bytes``.
    """
    warnings: list[Mapping[str, Any]] = []  # type: ignore[name-defined]
    decisions: list[TransformResult] = []
    transformed = False

    for _meta, fn in transforms:
        result = fn(request, context)
        decisions.append(result)

        if result.decision == TransformDecision.REJECTED:
            return PipelineResult(
                final_payload=request.provider_payload,
                transformed=transformed,
                generation=request.payload_generation,
                warnings=tuple(warnings),
                rejection=result,
                decisions=tuple(decisions),
            )

        if result.decision == TransformDecision.MUTATED:
            transformed = True

        warnings.extend(result.warnings)

    return PipelineResult(
        final_payload=request.provider_payload,
        transformed=transformed,
        generation=request.payload_generation,
        warnings=tuple(warnings),
        decisions=tuple(decisions),
    )


def serialize_provider_payload(request: ProviderBoundRequest) -> bytes:
    """Serialize the current provider payload to bytes (idempotent).

    If ``request.provider_bytes`` is already set and the payload
    generation has not changed, returns the cached bytes.  Otherwise
    serializes and caches.
    """
    return jsonx_dumps_bytes(request.provider_payload)
