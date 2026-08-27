"""Small helpers for provider-native prompt-cache boundary translation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from eggpool.transcoder.cache_stability import CacheBoundaryAnnotation

if TYPE_CHECKING:
    from eggpool.catalog.capabilities import (
        PromptCacheCapability,
        TranscodingCapabilities,
    )
    from eggpool.transcoder.context import TranscodeContext


def _cache_capability(
    capability: TranscodingCapabilities | None,
    protocol: str,
) -> PromptCacheCapability | None:
    if capability is None:
        return None
    return capability.prompt_cache_capability(protocol)


def _bounded_ttl_label(value: object) -> str:
    """Return a non-sensitive, bounded source TTL label."""
    if not isinstance(value, str) or len(value) > 16:
        return "invalid"
    if value in {"in_memory", "ephemeral"}:
        return value
    # Cap the magnitude (5 digits ≈ 27 hours in seconds) so absurd
    # values like ``999999h`` cannot reach diagnostic surfaces.
    if len(value) <= 6 and value[:-1].isdigit() and value[-1] in "smhd":
        return value
    return "unrecognized"


def prompt_cache_ttl_label(
    capability: TranscodingCapabilities | None,
    protocol: str,
) -> str:
    """Return target-contract TTL metadata without exposing raw config."""
    target_capability = _cache_capability(capability, protocol)
    if target_capability is None:
        return "unverified target contract"
    return target_capability.ttl_label()


def prompt_cache_source_ttl_label(value: object) -> str:
    """Return a bounded, non-sensitive source retention label."""
    return _bounded_ttl_label(value)


def openai_breakpoint_to_anthropic(
    part: dict[str, Any],
    *,
    source_path: str,
    capability: TranscodingCapabilities | None,
    count: list[int],
    context: TranscodeContext,
    warnings: list[dict[str, Any]],
) -> bool:
    """Translate one OpenAI explicit content breakpoint in place.

    Returns ``True`` only when the breakpoint maps natively onto the
    target part (``cache_control`` set); drop paths return ``False``.
    """
    if "prompt_cache_breakpoint" not in part:
        return False
    marker_raw = part.pop("prompt_cache_breakpoint")
    if not isinstance(marker_raw, dict):
        warnings.append(
            {"kind": "cache_breakpoint_invalid_shape", "field": source_path}
        )
        context.cache_boundary_tracker.record(
            CacheBoundaryAnnotation(
                kind="dropped_invalid_shape",
                source_protocol="openai",
                target_protocol="anthropic",
                source_path=source_path,
                target_path=None,
            )
        )
        return False
    marker = cast("dict[str, Any]", marker_raw)
    if marker.get("mode") != "explicit":
        warnings.append(
            {"kind": "cache_breakpoint_invalid_shape", "field": source_path}
        )
        context.cache_boundary_tracker.record(
            CacheBoundaryAnnotation(
                kind="dropped_invalid_shape",
                source_protocol="openai",
                target_protocol="anthropic",
                source_path=source_path,
                target_path=None,
            )
        )
        return False
    target_capability = _cache_capability(capability, "anthropic")
    if target_capability is None:
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
        return False
    if count[0] >= target_capability.max_breakpoints:
        warnings.append(
            {
                "kind": "cache_breakpoint_limit_exceeded",
                "field": source_path,
                "source_count": count[0] + 1,
                "target_limit": target_capability.max_breakpoints,
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
        return False
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
    """Translate one boundary, returning true only when it maps natively."""
    if "cache_control" not in part:
        return False
    cache_control_raw = part.get("cache_control")
    if not isinstance(cache_control_raw, dict):
        warnings.append({"kind": "cache_control_invalid_shape", "field": source_path})
        context.cache_boundary_tracker.record(
            CacheBoundaryAnnotation(
                kind="dropped_invalid_shape",
                source_protocol="anthropic",
                target_protocol="openai",
                source_path=source_path,
                target_path=None,
            )
        )
        return False
    cache_control = cast("dict[str, Any]", cache_control_raw)
    if cache_control.get("type") != "ephemeral":
        warnings.append({"kind": "cache_control_invalid_shape", "field": source_path})
        context.cache_boundary_tracker.record(
            CacheBoundaryAnnotation(
                kind="dropped_invalid_shape",
                source_protocol="anthropic",
                target_protocol="openai",
                source_path=source_path,
                target_path=None,
            )
        )
        return False
    target_capability = _cache_capability(capability, "openai")
    if cache_control.get("ttl") is not None:
        warnings.append(
            {
                "kind": "cache_ttl_mismatch",
                "field": source_path,
                "source_ttl": _bounded_ttl_label(cache_control["ttl"]),
                "target_ttl": (
                    target_capability.ttl_label()
                    if target_capability is not None
                    else "unverified target contract"
                ),
            }
        )
    if target_capability is None:
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
        return False
    if count[0] >= target_capability.max_breakpoints:
        warnings.append(
            {
                "kind": "cache_breakpoint_limit_exceeded",
                "field": source_path,
                "source_count": count[0] + 1,
                "target_limit": target_capability.max_breakpoints,
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
        return False
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
