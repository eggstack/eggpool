"""Conditional request segmentation guard.

Phase 2 of the performance optimization plan introduces a predicate that
determines whether ``segment_request()`` needs to run for the current
request.  Segmentation is skipped when no active consumer — compression
observe/safe, synthetic cache controls, or cache observability — needs
the output, saving CPU on the hot path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.models.config import AppConfig


def should_segment_request(
    app_config: AppConfig | None,
    *,
    compression_enabled: bool = False,
    compression_mode: str = "off",
    synthetic_cache_enabled: bool = False,
    cache_observability_enabled: bool = False,
    force_segmentation: bool = False,
) -> bool:
    """Return ``True`` when any active consumer needs segmentation.

    Segmentation is required when:

    * ``force_segmentation`` is ``True`` (debug / compatibility mode)
    * Compression observe mode is active (``enabled`` AND ``mode`` in
      ``("observe", "safe")``)
    * Compression safe mode is active
    * Synthetic cache controls are enabled
    * Request-shaping / cache observability mode promises segmentation
      metrics

    Returns ``False`` when none of the above apply, allowing the caller
    to skip the relatively expensive ``segment_request()`` call.
    """
    if force_segmentation:
        return True

    if (
        not compression_enabled
        and not synthetic_cache_enabled
        and not cache_observability_enabled
    ):
        return False

    return (
        (compression_enabled and compression_mode in ("observe", "safe"))
        or synthetic_cache_enabled
        or cache_observability_enabled
    )
