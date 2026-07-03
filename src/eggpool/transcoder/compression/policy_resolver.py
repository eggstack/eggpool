"""Phase 6 deterministic compression-policy resolver.

Operators extend the global ``[compression]`` config with
``[[compression.policies]]`` rows.  Each row overlays a subset of
the global compression knobs when a request matches specific
client, protocol, provider, or model fields.  This module owns the
match + merge algorithm:

1. Start with the global :class:`CompressionConfig`.
2. Walk the override list in file order.
3. For each override that matches the request context, overlay
   non-``None`` fields onto the current config.  Scalar fields
   are last-match-wins; ``transforms`` are merged field-by-field.
4. Re-validate the merged config against the same safety rules
   as the global config (static-prefix compression only in
   ``safe`` mode and only with ``allow_static_prefix_override``).
5. If validation fails, fall back to the global config and emit a
   warning.  Resolution never raises; malformed overrides are
   logged and the request is served with the safe default.

The resolver is **content-private**: it never inspects the raw
request body, the prompt content, the model output, or any
header value other than the well-known client-identity fields
exposed by :class:`CompressionPolicyContext`.  All matching is
simple string equality or ``*`` prefix/suffix globbing.

The resolved policy carries:

- ``name``   : name of the matched override (``"<global>"`` when none matched).
- ``source`` : a stable audit string (``"global"`` or ``"policy:<name>"``).
- ``config`` : the merged :class:`CompressionConfig` for analyzer / applier.
- ``matched_policy_names`` : the tuple of override names that fired, in file order.
- ``warnings`` : bounded, structured warnings produced during resolution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import ValidationError

from eggpool.transcoder.compression.policy import (
    CompressionConfig,
    CompressionPolicyOverride,
    CompressionTransforms,
)


@dataclass(frozen=True, slots=True)
class CompressionPolicyContext:
    """Inputs the resolver needs to pick a policy.

    All fields except ``source_protocol`` may be ``None``.  A
    pre-route resolver sees only client identity, source protocol,
    requested model, and the transcoded flag; a post-route resolver
    (not yet wired in Phase 6) would also see provider id/kind and
    the resolved model id.  Provider-specific overrides therefore
    require post-route resolution and are silently skipped pre-route.
    """

    client_id: str | None = None
    client_name: str | None = None
    source_protocol: str = "openai"
    target_protocol: str | None = None
    requested_model: str | None = None
    resolved_model: str | None = None
    provider_id: str | None = None
    provider_kind: str | None = None
    transcoded: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedCompressionPolicy:
    """Output of the resolver.

    The ``config`` field is the merged :class:`CompressionConfig` to
    feed to the analyzer and applier.  ``name`` and ``source`` are
    audit strings persisted to the ``compression_policy_name`` /
    ``compression_policy_source`` columns.  ``matched_policy_names``
    records every override that fired (file order).  ``warnings``
    are bounded structured strings suitable for structured logs.

    The ``synthetic_cache_overrides`` field carries the merged
    Phase 9 synthetic cache-control knobs (``enabled``, ``dry_run``,
    ``min_stable_tokens``, ``max_breakpoints``) when any policy
    override provided them; ``None`` when the global
    ``[cache] synthetic_cache_controls`` config should be honoured
    unchanged.  This field is informational only — the cache-
    synthesis module merges it with the global ``CacheConfig``
    before running the candidate selector.
    """

    name: str
    source: str
    config: CompressionConfig
    matched_policy_names: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    synthetic_cache_overrides: dict[str, Any] | None = None
    runtime_override_metadata: dict[str, Any] = field(default_factory=dict[str, Any])

    def as_dict(self) -> dict[str, Any]:
        """Compact dict for the persistence layer.

        ``name`` and ``source`` go to dedicated columns; ``warnings``
        is serialised to a JSON array; the resolved config summary
        (mode, enabled, key thresholds) is flattened so the
        finalizer does not need to import :mod:`policy_resolver`.
        """
        return {
            "name": self.name,
            "source": self.source,
            "matched_policy_names": list(self.matched_policy_names),
            "warnings": list(self.warnings),
            "config_enabled": bool(self.config.enabled),
            "config_mode": str(self.config.mode),
            "config_placement": str(self.config.placement),
            "config_respect_cache_boundaries": bool(
                self.config.respect_cache_boundaries,
            ),
            "config_compress_static_prefix": bool(
                self.config.compress_static_prefix,
            ),
            "config_min_candidate_tokens": int(self.config.min_candidate_tokens),
            "config_min_savings_tokens": int(self.config.min_savings_tokens),
            "config_max_compression_latency_ms": float(
                self.config.max_compression_latency_ms,
            ),
            "runtime_override_active": bool(
                self.runtime_override_metadata.get("active", False),
            ),
            "runtime_override_fields": dict(
                self.runtime_override_metadata.get("applied_fields", {}),
            ),
        }


GLOBAL_POLICY_NAME = "<global>"
GLOBAL_POLICY_SOURCE = "global"


def _glob_match(value: str | None, pattern: str) -> bool:
    """Glob match with simple ``*`` prefix/suffix support.

    ``*foo`` matches any string ending in ``foo``; ``foo*`` matches
    any string starting with ``foo``; ``*foo*`` matches any string
    containing ``foo``.  Exact strings match exactly.  No regex,
    no character classes, no escapes — operators get a small,
    predictable surface.
    """
    if value is None:
        return False
    if pattern == value:
        return True
    if "*" not in pattern:
        return False
    starts_star = pattern.startswith("*")
    ends_star = pattern.endswith("*")
    body = pattern.strip("*")
    if starts_star and ends_star:
        return body in value
    if starts_star:
        return value.endswith(body)
    if ends_star:
        return value.startswith(body)
    return False


def _any_match(
    value: str | None,
    patterns: list[str] | None,
) -> bool:
    """True when ``value`` matches at least one pattern.

    A ``None`` patterns list or an empty list never matches.
    Comparison is case-sensitive (operator-friendly; downstream
    config keys are also case-sensitive).
    """
    if not patterns or value is None:
        return False
    return any(_glob_match(value, pattern) for pattern in patterns)


def _override_matches(
    override: CompressionPolicyOverride,
    ctx: CompressionPolicyContext,
) -> bool:
    """Match a single override against a request context.

    Match fields are unioned: the override fires when **any** match
    field fires.  A request with no provider/model information (the
    pre-route case) simply cannot fire provider/model fields;
    client / source protocol / requested model / transcoded are
    the always-on discriminators.

    When no match fields are configured, the override is treated as
    a catch-all and always fires.  The config validator only accepts
    catch-all overrides under the reserved name ``"default"``;
    pre-route resolution still applies it to every request.
    """
    if (
        _any_match(ctx.client_id, override.match_clients)
        or _any_match(ctx.client_name, override.match_clients)
        or _any_match(ctx.provider_id, override.match_provider_ids)
        or _any_match(ctx.provider_kind, override.match_provider_kinds)
        or _any_match(ctx.resolved_model, override.match_models)
        or _any_match(ctx.requested_model, override.match_requested_models)
    ):
        return True
    if override.match_protocols and ctx.source_protocol in override.match_protocols:
        return True
    if override.match_transcoded is not None and bool(
        override.match_transcoded
    ) == bool(ctx.transcoded):
        return True
    return not override.has_any_match_field()


_OVERRIDE_ONLY_FIELDS = frozenset(
    {
        "match_clients",
        "match_provider_ids",
        "match_provider_kinds",
        "match_models",
        "match_requested_models",
        "match_protocols",
        "match_transcoded",
    },
)

# Phase 9: synthetic cache-control override fields. They ride on
# CompressionPolicyOverride so we reuse the same match-and-merge
# machinery, but they are overlay-only fields that live on a
# different Pydantic model (SyntheticCacheControlsConfig).  The
# resolver surfaces them via ``ResolvedCompressionPolicy.cache`` so
# the cache-synthesis module can read the merged values.
_SYNTHETIC_CACHE_OVERLAY_FIELDS = frozenset(
    {
        "synthetic_cache_controls",
        "synthetic_cache_dry_run",
        "synthetic_cache_min_stable_tokens",
        "synthetic_cache_max_breakpoints",
    },
)


def _overlay_config(
    base: CompressionConfig,
    override: CompressionPolicyOverride,
) -> CompressionConfig:
    """Apply one override on top of a base config.

    Builds a fresh :class:`CompressionConfig` via ``model_validate``
    so the merged model honours all validators (static-prefix
    safety guard, transform defaults, etc.).  ``None`` override
    fields keep the base value; non-``None`` overrides win.
    ``transforms`` is merged field-by-field: a non-``None``
    override transforms replaces each base field that is also
    non-``None`` inside the override.

    Match fields are ``CompressionPolicyOverride``-only and are
    dropped before the dict is re-validated against
    :class:`CompressionConfig` (which uses ``extra='forbid'``).
    Overlay knobs like ``compress_static_prefix`` are present on
    both classes and are merged field-by-field.
    """
    base_dict_raw: Any = base.model_dump()
    override_dict_raw: Any = override.model_dump(exclude={"name"})
    base_dict: dict[str, Any] = cast(
        "dict[str, Any]",
        base_dict_raw if isinstance(base_dict_raw, dict) else {},
    )
    override_dict: dict[str, Any] = cast(
        "dict[str, Any]",
        override_dict_raw if isinstance(override_dict_raw, dict) else {},
    )
    for key, value in override_dict.items():
        if key in _OVERRIDE_ONLY_FIELDS:
            continue
        if value is None:
            continue
        if key == "transforms" and isinstance(value, dict):
            base_transforms_obj: Any = base_dict.get("transforms")
            base_transforms: dict[str, Any] = (
                dict(base_transforms_obj)  # type: ignore[arg-type]
                if isinstance(base_transforms_obj, dict)
                else {}
            )
            transforms_dict: dict[str, Any] = value  # type: ignore[assignment]
            for transform_key, transform_value in transforms_dict.items():
                if transform_value is not None:
                    base_transforms[transform_key] = transform_value
            base_dict["transforms"] = base_transforms
        else:
            base_dict[key] = value
    try:
        return CompressionConfig.model_validate(base_dict)
    except ValidationError:
        raise


def resolve_compression_policy(
    base: CompressionConfig,
    ctx: CompressionPolicyContext,
    *,
    overrides: list[CompressionPolicyOverride] | None = None,
    runtime_override_registry: Any | None = None,
) -> ResolvedCompressionPolicy:
    """Pick and merge the compression policy for one request.

    Algorithm:

    1. Iterate overrides in file order; collect every match.
    2. Overlay each match on top of the previous config (last
       match wins on scalars; transforms merge field-by-field).
    3. Re-validate the final config; on validation error fall back
       to the global config and append a warning.
    4. Return a frozen :class:`ResolvedCompressionPolicy`.

    ``overrides`` defaults to ``base.policies`` so the typical call
    site is ``resolve_compression_policy(base, ctx)``.  Tests can
    pass a curated list to exercise the merge order.

    The Phase 9 synthetic cache overrides are extracted as a side
    effect of the same pass and returned in the
    ``synthetic_cache_overrides`` field so callers (the cache-
    synthesis module) can read the merged values without
    re-walking the override list.
    """
    candidates = overrides if overrides is not None else list(base.policies)
    warnings: list[str] = []
    matched_names: list[str] = []
    merged = base
    synthetic_cache_overrides: dict[str, Any] = {}
    for override in candidates:
        if not _override_matches(override, ctx):
            continue
        matched_names.append(override.name)
        try:
            merged = _overlay_config(merged, override)
        except ValidationError as exc:
            warnings.append(
                f"policy:{override.name}: overlay validation failed: {exc}; "
                "ignored override and continued with the previous config",
            )
        override_dict: Any = override.model_dump()
        if not isinstance(override_dict, dict):
            continue
        override_dict_cast: dict[str, Any] = cast("dict[str, Any]", override_dict)
        for key in _SYNTHETIC_CACHE_OVERLAY_FIELDS:
            value = override_dict_cast.get(key)
            if value is None:
                continue
            synthetic_cache_overrides[key] = value
    synthetic_cache_overrides_out: dict[str, Any] | None = (
        synthetic_cache_overrides or None
    )
    if not matched_names:
        runtime_metadata = {"active": False, "applied_fields": {}}
        if runtime_override_registry is not None:
            try:
                from eggpool.transcoder.compression.tuning import (
                    apply_runtime_override,
                )

                override = runtime_override_registry.lookup(GLOBAL_POLICY_NAME)
                if override is not None:
                    merged, runtime_metadata = apply_runtime_override(merged, override)
                    if not runtime_metadata.get("active", False):
                        warnings.append(
                            f"runtime_override: registry entry for "
                            f"policy:{GLOBAL_POLICY_NAME} could not be applied; "
                            "falling back to the previous config",
                        )
            except Exception as exc:  # pragma: no cover - safety net
                warnings.append(
                    f"runtime_override: registry lookup failed: {exc}; "
                    "falling back to the previous config",
                )
        return ResolvedCompressionPolicy(
            name=GLOBAL_POLICY_NAME,
            source=GLOBAL_POLICY_SOURCE,
            config=merged,
            matched_policy_names=(),
            warnings=tuple(warnings),
            synthetic_cache_overrides=synthetic_cache_overrides_out,
            runtime_override_metadata=runtime_metadata,
        )
    last = matched_names[-1]
    source = f"policy:{last}"
    runtime_metadata: dict[str, Any] = {"active": False, "applied_fields": {}}
    if runtime_override_registry is not None:
        try:
            # Lazy import to keep the resolver import graph tiny for
            # code paths that never use runtime overrides (Phase 6).
            from eggpool.transcoder.compression.tuning import (
                apply_runtime_override,
            )

            policy_name = last if matched_names else GLOBAL_POLICY_NAME
            override = runtime_override_registry.lookup(policy_name)
            if override is not None:
                merged, runtime_metadata = apply_runtime_override(merged, override)
                if not runtime_metadata.get("active", False):
                    warnings.append(
                        f"runtime_override: registry entry for "
                        f"policy:{policy_name} could not be applied; "
                        "falling back to the previous config",
                    )
        except Exception as exc:  # pragma: no cover - safety net
            warnings.append(
                f"runtime_override: registry lookup failed: {exc}; "
                "falling back to the previous config",
            )
    return ResolvedCompressionPolicy(
        name=last,
        source=source,
        config=merged,
        matched_policy_names=tuple(matched_names),
        warnings=tuple(warnings),
        synthetic_cache_overrides=synthetic_cache_overrides_out,
        runtime_override_metadata=runtime_metadata,
    )


def merge_transforms(
    base: CompressionTransforms,
    override: CompressionTransforms | None,
) -> CompressionTransforms:
    """Field-by-field merge helper exposed for tests and audit code.

    A ``None`` ``override`` returns a defensive copy of ``base``.
    Per-field, ``None`` inside the override keeps the base value;
    ``True`` / ``False`` wins.
    """
    if override is None:
        return base.model_copy()
    base_dict = base.model_dump()
    override_dict = override.model_dump()
    for key, value in override_dict.items():
        if value is not None:
            base_dict[key] = value
    return CompressionTransforms.model_validate(base_dict)


__all__ = [
    "CompressionPolicyContext",
    "GLOBAL_POLICY_NAME",
    "GLOBAL_POLICY_SOURCE",
    "ResolvedCompressionPolicy",
    "merge_transforms",
    "resolve_compression_policy",
]
