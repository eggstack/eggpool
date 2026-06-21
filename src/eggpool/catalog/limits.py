"""Model limit resolution: per-provider effective context/input/output limits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from eggpool.models.config import AppConfig


# -- Upstream metadata key aliases -------------------------------------------

_CONTEXT_KEYS: tuple[str, ...] = (
    "max_context_tokens",
    "context_window",
    "context_length",
    "max_position_embeddings",
)

_INPUT_KEYS: tuple[str, ...] = (
    "max_input_tokens",
    "input_token_limit",
)

_OUTPUT_KEYS: tuple[str, ...] = (
    "max_output_tokens",
    "output_token_limit",
    "max_completion_tokens",
)

_SOURCE_VALUES: frozenset[str] = frozenset(
    {"provider_override", "global_override", "upstream_metadata", "unknown"}
)


# -- Result type ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectiveModelLimits:
    """Resolved effective limits for a single (model, provider) pair."""

    context_tokens: int | None
    input_tokens: int | None
    output_tokens: int | None
    enforce: bool
    context_source: str | None
    input_source: str | None
    output_source: str | None


_UNKNOWN = EffectiveModelLimits(
    context_tokens=None,
    input_tokens=None,
    output_tokens=None,
    enforce=True,
    context_source="unknown",
    input_source="unknown",
    output_source="unknown",
)


# -- Upstream metadata extraction -------------------------------------------


def _parse_positive_int(value: object) -> int | None:
    """Return a positive integer from *value*, or ``None``.

    Accepts ``int`` and strict decimal-string representations.
    Rejects booleans, negative values, zero, and non-integral floats.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        return int(value) if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = int(text)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _extract_first(
    sources: tuple[Mapping[str, object], ...],
    keys: tuple[str, ...],
) -> tuple[int | None, str | None]:
    """Look through *sources* (in order) for the first valid integer.

    Returns ``(value, source_label)`` where *source_label* is
    ``"upstream_metadata"`` when a value is found, or ``None`` otherwise.
    """
    for source in sources:
        for key in keys:
            raw = source.get(key)
            if raw is None:
                continue
            parsed = _parse_positive_int(raw)
            if parsed is not None:
                return parsed, "upstream_metadata"
    return None, None


def extract_upstream_limits(
    capabilities: Mapping[str, object],
    source_metadata: Mapping[str, object],
) -> tuple[int | None, int | None, int | None]:
    """Extract context, input, and output limits from upstream metadata.

    Prefers normalized ``capabilities`` over opaque ``source_metadata``.
    Returns ``(context_tokens, input_tokens, output_tokens)`` where each
    element is ``None`` when no valid value is found.
    """
    ctx, _ = _extract_first((capabilities, source_metadata), _CONTEXT_KEYS)
    inp, _ = _extract_first((capabilities, source_metadata), _INPUT_KEYS)
    out, _ = _extract_first((capabilities, source_metadata), _OUTPUT_KEYS)
    return ctx, inp, out


def extract_upstream_limits_with_source(
    capabilities: Mapping[str, object],
    source_metadata: Mapping[str, object],
) -> tuple[
    tuple[int | None, str | None],
    tuple[int | None, str | None],
    tuple[int | None, str | None],
]:
    """Like :func:`extract_upstream_limits` but returns per-field provenance."""
    ctx = _extract_first((capabilities, source_metadata), _CONTEXT_KEYS)
    inp = _extract_first((capabilities, source_metadata), _INPUT_KEYS)
    out = _extract_first((capabilities, source_metadata), _OUTPUT_KEYS)
    return ctx, inp, out


# -- Resolver ----------------------------------------------------------------


class ModelLimitResolver:
    """Resolve effective model limits from config overrides and upstream metadata.

    The resolver is pure after construction: it reads ``AppConfig`` but
    does not touch the database, network, or cache.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def resolve(
        self,
        *,
        provider_id: str,
        model_id: str,
        capabilities: Mapping[str, object],
        source_metadata: Mapping[str, object],
    ) -> EffectiveModelLimits:
        """Return effective limits for *model_id* on *provider_id*.

        Precedence (per field):
        1. Provider-specific override
        2. Global override
        3. Upstream metadata
        4. Unknown (None)
        """
        provider_cfg = self._config.providers.get(provider_id)
        provider_override = (
            provider_cfg.model_overrides.get(model_id)
            if provider_cfg is not None
            else None
        )
        global_override = self._config.model_overrides.get(model_id)

        upstream_ctx, upstream_inp, upstream_out = extract_upstream_limits(
            capabilities, source_metadata
        )

        def _resolve(
            provider_val: int | None | bool,
            global_val: int | None | bool,
            upstream_val: int | None,
        ) -> tuple[int | None, str | None]:
            # Provider override
            if provider_val is not None and not isinstance(provider_val, bool):
                return provider_val, "provider_override"
            # Global override
            if global_val is not None and not isinstance(global_val, bool):
                return global_val, "global_override"
            # Upstream metadata
            if upstream_val is not None:
                return upstream_val, "upstream_metadata"
            return None, "unknown"

        # Resolve enforce flag: true if any override says true, default true
        enforce = True
        if provider_override is not None:
            enforce = provider_override.enforce_context_limit
        elif global_override is not None:
            enforce = global_override.enforce_context_limit

        ctx_val, ctx_src = _resolve(
            getattr(provider_override, "max_context_tokens", None)
            if provider_override is not None
            else None,
            getattr(global_override, "max_context_tokens", None)
            if global_override is not None
            else None,
            upstream_ctx,
        )
        inp_val, inp_src = _resolve(
            getattr(provider_override, "max_input_tokens", None)
            if provider_override is not None
            else None,
            getattr(global_override, "max_input_tokens", None)
            if global_override is not None
            else None,
            upstream_inp,
        )
        out_val, out_src = _resolve(
            getattr(provider_override, "max_output_tokens", None)
            if provider_override is not None
            else None,
            getattr(global_override, "max_output_tokens", None)
            if global_override is not None
            else None,
            upstream_out,
        )

        return EffectiveModelLimits(
            context_tokens=ctx_val,
            input_tokens=inp_val,
            output_tokens=out_val,
            enforce=enforce,
            context_source=ctx_src,
            input_source=inp_src,
            output_source=out_src,
        )


# -- Conservative merge for unsuffixed models --------------------------------


def conservative_limits(
    limits: Iterable[EffectiveModelLimits],
) -> EffectiveModelLimits:
    """Merge multiple provider-specific limits into one conservative set.

    Rules:
    - For each numeric field, take the minimum of known positive values.
    - If every provider has ``None``, return ``None``.
    - ``enforce`` is ``True`` if any provider enforces.
    - Provenance is ``"conservative_provider_minimum"``.
    """
    ctx_values: list[int] = []
    inp_values: list[int] = []
    out_values: list[int] = []
    any_enforce = False

    for lim in limits:
        if lim.context_tokens is not None:
            ctx_values.append(lim.context_tokens)
        if lim.input_tokens is not None:
            inp_values.append(lim.input_tokens)
        if lim.output_tokens is not None:
            out_values.append(lim.output_tokens)
        if lim.enforce:
            any_enforce = True

    src = "conservative_provider_minimum"

    return EffectiveModelLimits(
        context_tokens=min(ctx_values) if ctx_values else None,
        input_tokens=min(inp_values) if inp_values else None,
        output_tokens=min(out_values) if out_values else None,
        enforce=any_enforce,
        context_source=src if ctx_values else "unknown",
        input_source=src if inp_values else "unknown",
        output_source=src if out_values else "unknown",
    )
