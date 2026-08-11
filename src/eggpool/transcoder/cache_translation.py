"""Small helpers for provider-native prompt-cache boundary translation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from eggpool.transcoder.cache_stability import CacheBoundaryAnnotation

if TYPE_CHECKING:
    from eggpool.catalog.capabilities import TranscodingCapabilities
    from eggpool.transcoder.context import TranscodeContext

MAX_NATIVE_BREAKPOINTS = 4


def _supports(capability: TranscodingCapabilities | None, protocol: str) -> bool:
    return capability is not None and capability.supports(
        "prompt_cache_breakpoints", protocol
    )


def openai_breakpoint_to_anthropic(
    part: dict[str, Any],
    *,
    source_path: str,
    capability: TranscodingCapabilities | None,
    count: list[int],
    context: TranscodeContext,
    warnings: list[dict[str, Any]],
) -> bool:
    """Translate one OpenAI explicit content breakpoint in place."""
    marker_raw = part.get("prompt_cache_breakpoint")
    if not isinstance(marker_raw, dict):
        return False
    marker = cast("dict[str, Any]", marker_raw)
    if marker.get("mode") != "explicit":
        return False
    if not _supports(capability, "anthropic"):
        kind = "cache_breakpoint_unsupported_target"
        warnings.append({"kind": kind, "field": source_path})
        context.cache_boundary_tracker.record(
            CacheBoundaryAnnotation(
                kind="dropped_unsupported_target",
                source_protocol="openai",
                target_protocol="anthropic",
                source_path=source_path,
                target_path=None,
            )
        )
        return True
    if count[0] >= MAX_NATIVE_BREAKPOINTS:
        warnings.append(
            {
                "kind": "cache_breakpoint_limit_exceeded",
                "field": source_path,
                "source_count": count[0] + 1,
                "target_limit": MAX_NATIVE_BREAKPOINTS,
            }
        )
        context.cache_boundary_tracker.record(
            CacheBoundaryAnnotation(
                kind="dropped_unsupported_target",
                source_protocol="openai",
                target_protocol="anthropic",
                source_path=source_path,
                target_path=None,
            )
        )
        return True
    part["cache_control"] = {"type": "ephemeral"}
    count[0] += 1
    context.cache_boundary_tracker.record(
        CacheBoundaryAnnotation(
            kind="preserved_relocated",
            source_protocol="openai",
            target_protocol="anthropic",
            source_path=source_path,
            target_path=source_path,
            cache_control_type="ephemeral",
        )
    )
    return True


def anthropic_boundary_to_openai(
    part: dict[str, Any],
    *,
    source_path: str,
    capability: TranscodingCapabilities | None,
    count: list[int],
    context: TranscodeContext,
    warnings: list[dict[str, Any]],
) -> bool:
    """Translate one Anthropic cacheable block boundary in place."""
    if "cache_control" not in part:
        return False
    cache_control_raw = part.get("cache_control")
    if not isinstance(cache_control_raw, dict):
        warnings.append({"kind": "cache_control_invalid_shape", "field": source_path})
        return True
    cache_control = cast("dict[str, Any]", cache_control_raw)
    if not isinstance(cache_control.get("type"), str):
        warnings.append({"kind": "cache_control_invalid_shape", "field": source_path})
        return True
    if cache_control.get("ttl") is not None:
        warnings.append(
            {
                "kind": "cache_ttl_mismatch",
                "field": source_path,
                "source_ttl": str(cache_control["ttl"]),
                "target_ttl": "30m",
            }
        )
    if not _supports(capability, "openai"):
        warnings.append(
            {"kind": "cache_breakpoint_unsupported_target", "field": source_path}
        )
        context.cache_boundary_tracker.record(
            CacheBoundaryAnnotation(
                kind="dropped_unsupported_target",
                source_protocol="anthropic",
                target_protocol="openai",
                source_path=source_path,
                target_path=None,
                cache_control_type=str(cache_control.get("type")),
            )
        )
        return True
    if count[0] >= MAX_NATIVE_BREAKPOINTS:
        warnings.append(
            {
                "kind": "cache_breakpoint_limit_exceeded",
                "field": source_path,
                "source_count": count[0] + 1,
                "target_limit": MAX_NATIVE_BREAKPOINTS,
            }
        )
        context.cache_boundary_tracker.record(
            CacheBoundaryAnnotation(
                kind="dropped_unsupported_target",
                source_protocol="anthropic",
                target_protocol="openai",
                source_path=source_path,
                target_path=None,
                cache_control_type=str(cache_control.get("type")),
            )
        )
        return True
    part["prompt_cache_breakpoint"] = {"mode": "explicit"}
    count[0] += 1
    context.cache_boundary_tracker.record(
        CacheBoundaryAnnotation(
            kind="preserved_relocated",
            source_protocol="anthropic",
            target_protocol="openai",
            source_path=source_path,
            target_path=source_path,
            cache_control_type=str(cache_control.get("type")),
        )
    )
    return True
