"""Structured price resolution pipeline.

This module extracts pricing resolution out of
``CatalogService._maybe_insert_price_snapshot`` so that callers receive
a ``ResolvedPricing`` object with explicit source/detail/confidence
metadata instead of a flat tuple of values.

The resolver layer sits between upstream catalog metadata + operator
TOML overrides and the price-snapshot persistence path. Future catalog
fallbacks (Phase 3) will plug into the same pipeline by wrapping
``ResolvedPricing`` with the catalog-derived ``source_detail`` /
``source_confidence`` and returning it from the higher-level resolver.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from eggpool.catalog.pricing import (
    extract_price_decimal,
    parse_microdollars_per_million,
    parse_price_per_1k,
    price_unit,
)

logger = logging.getLogger(__name__)


# Broad ``source`` labels persisted on the snapshot row. Mirrors the
# values already supported by migration 0005.
SOURCE_CONFIG = "config"
SOURCE_UPSTREAM = "upstream"
SOURCE_MIXED = "mixed"


# Granular ``source_detail`` labels so the dashboard and audit trail
# can pinpoint exactly which path produced a value.
SOURCE_DETAIL_PROVIDER_METADATA = "provider_metadata"
SOURCE_DETAIL_OPERATOR_OVERRIDE = "operator_override"
SOURCE_DETAIL_PROVIDER_PRICING_ENDPOINT = "provider_pricing_endpoint"
SOURCE_DETAIL_OPENCODE_ZEN = "opencode_zen"
SOURCE_DETAIL_OPENROUTER = "openrouter"
SOURCE_DETAIL_STATIC_CATALOG = "static_catalog"
SOURCE_DETAIL_HEURISTIC = "heuristic"


# ``source_confidence`` labels — how much trust to put in the value.
CONFIDENCE_AUTHORITATIVE = "authoritative"
CONFIDENCE_CURATED_ALIAS = "curated_alias"
CONFIDENCE_EXACT_EXTERNAL_ID = "exact_external_id"
CONFIDENCE_OPERATOR = "operator"
CONFIDENCE_HEURISTIC = "heuristic"


@dataclass(frozen=True)
class ResolvedPricing:
    """Structured pricing resolution result.

    Each per-category field carries its own provenance via the
    aggregated ``source``, ``source_detail``, and ``source_confidence``
    fields. ``None`` values mean "not resolved from any source"; the
    caller can still persist partial snapshots when at least one
    category is resolved.
    """

    input_price_per_1k: float | None
    output_price_per_1k: float | None
    cache_read_per_million_microdollars: int | None
    cache_write_per_million_microdollars: int | None
    source: str  # one of SOURCE_*
    source_detail: str  # one of SOURCE_DETAIL_*
    source_confidence: str  # one of CONFIDENCE_*
    source_model_id: str | None = None  # external catalog model ID
    source_provider_id: str | None = None  # external catalog provider ID

    @property
    def has_any(self) -> bool:
        return any(
            value is not None
            for value in (
                self.input_price_per_1k,
                self.output_price_per_1k,
                self.cache_read_per_million_microdollars,
                self.cache_write_per_million_microdollars,
            )
        )


def _safe_parse_price_per_1k(
    category: str, value: object, *, default_unit: str = "1k"
) -> float | None:
    try:
        return parse_price_per_1k(value, default_unit=default_unit)
    except ValueError as exc:
        logger.warning(
            "Ignoring invalid %s price: %s",
            category,
            exc,
        )
        return None


# Pricing dicts that carry no unit suffix on individual fields (e.g.
# ``pricing: {prompt: "0.000003"}``) are ambiguous: the same bare number
# could mean dollars/token (OpenRouter) or dollars/million (Anthropic-style
# vendors, MiniMax, and many other Anthropic-compatible endpoints). The
# mistake is catastrophic — misreading a per-million value as per-token
# inflates downstream cost by 1,000,000x.
#
# Generalizable resolution rules, in order of priority:
#
#   1. Sibling agreement — if any OTHER field in the same ``pricing`` dict
#      carries an explicit per-token / per-1k / per-million suffix, the
#      siblings share units. OpenRouter uses ``$0.000003`` (per-token)
#      while Anthropic-style catalogs use human-scale values
#      (``0.2`` = $0.20/M). Reading siblings is provider-agnostic and
#      catches MiniMax, OpenAI-Compat-vendors, and any future catalog
#      that mixes both styles.
#
#   2. Numeric scale — when no sibling carries an explicit unit, look at
#      the magnitude of all numeric siblings. OpenRouter per-token values
#      cluster below ``1e-3``; per-million values cluster above. A
#      majority-of-siblings rule avoids the single-value ambiguity.
#
#   3. Safe default — if both signals are inconclusive (one value, or
#      mixed magnitudes), default to ``per-million``. Per-million is
#      conservative for under-reporting and matches the Anthropic API
#      convention; per-token interpretation is what produced the bug.
_PRICING_PER_TOKEN_CEILING = Decimal("0.001")


def _explicit_unit_for_pricing_dict_value(value: object) -> str | None:
    """Return the unit if ``value`` carries an explicit suffix, else None."""
    if not isinstance(value, str):
        return None
    return price_unit(value)


def _pricing_dict_default_unit(value: object, siblings: dict[str, Any]) -> str:
    """Resolve the default unit for an ambiguous bare-numeric pricing field.

    ``siblings`` are the other fields in the same ``pricing`` dict. When
    even one sibling carries an explicit unit suffix, every sibling
    inherits it (openrouter and Anthropic-style catalogs are uniform
    within a payload). When no sibling carries a suffix, the unit is
    inferred from the magnitudes of the parseable numeric siblings,
    defaulting to per-million when the signal is inconclusive.
    """
    # Rule 1: explicit sibling unit. Cheap and unambiguous when present.
    for sibling_value in siblings.values():
        unit = _explicit_unit_for_pricing_dict_value(sibling_value)
        if unit is not None:
            return unit

    # Rule 2: numeric-scale consensus across siblings. Collect every
    # parseable numeric magnitude; if the majority are below the
    # per-token ceiling, treat the dict as per-token, otherwise
    # per-million. A single-value dict falls through to rule 3.
    magnitudes: list[Decimal] = []
    for sibling_value in (value, *siblings.values()):
        if _explicit_unit_for_pricing_dict_value(sibling_value) is not None:
            continue
        try:
            number = extract_price_decimal(sibling_value)
        except ValueError:
            continue
        if number is None:
            continue
        magnitudes.append(number)

    per_token_count = sum(1 for m in magnitudes if m < _PRICING_PER_TOKEN_CEILING)
    per_million_count = len(magnitudes) - per_token_count

    if per_token_count > per_million_count and per_token_count > 0:
        return "token"
    if per_million_count > per_token_count and per_million_count > 0:
        return "million"

    # Rule 3: inconclusive. Default to per-million — the Anthropic
    # convention and the direction that does not produce runaway
    # inflation when an upstream is wrong about its units.
    return "million"


def _safe_parse_pricing_dict_price(
    category: str, value: object, siblings: dict[str, Any] | None = None
) -> float | None:
    if siblings is None:
        siblings = {}
    return _safe_parse_price_per_1k(
        category,
        value,
        default_unit=_pricing_dict_default_unit(value, siblings),
    )


def _safe_parse_microdollars(
    category: str, value: object, *, default_unit: str | None = None
) -> int | None:
    """Parse a cache rate into integer microdollars per million tokens.

    ``default_unit`` (``"token"``/``"1k"``/``"million"``/``None``) is
    applied only when the string carries no unit suffix. The default
    ``None`` matches the pre-existing contract: a bare numeric string is
    treated as already being in microdollars per million tokens.
    """
    try:
        return parse_microdollars_per_million(value, default_unit=default_unit)
    except ValueError as exc:
        logger.warning(
            "Ignoring invalid %s price: %s",
            category,
            exc,
        )
        return None


def resolve_pricing_from_metadata(
    *,
    model_id: str,
    provider_id: str,
    model_info: dict[str, Any],
    override_values: dict[str, Any],
) -> ResolvedPricing | None:
    """Resolve pricing from operator overrides + upstream metadata.

    Returns ``None`` when no category can be resolved — the caller
    should skip snapshot insertion in that case.

    The returned ``source`` field is the broad persisted label
    (``"config"``, ``"upstream"``, or ``"mixed"``). ``source_detail``
    is the granular path; for this metadata-only resolver it is always
    either ``SOURCE_DETAIL_PROVIDER_METADATA`` (every category came
    from upstream metadata) or ``SOURCE_DETAIL_OPERATOR_OVERRIDE``
    (every category came from a TOML override), or one of those with
    the other mixed in (which keeps the broad ``source`` as ``"mixed"``).
    """
    meta: dict[str, Any] = model_info.get("source_metadata", {})

    def _has_override(key: str) -> bool:
        return override_values.get(key) is not None

    def _pricing_siblings(exclude: str) -> dict[str, Any]:
        """Return sibling fields of ``pricing`` excluding ``exclude``.

        Used as the cross-key context for unit disambiguation. Including
        the value's own key is unnecessary because each call site asks
        for one specific category and the disambiguator inspects every
        other numeric sibling for scale consensus.
        """
        pricing = meta.get("pricing")
        if not isinstance(pricing, dict):
            return {}
        pricing_dict = cast("dict[str, Any]", pricing)
        siblings: dict[str, Any] = {}
        for k, v in pricing_dict.items():
            if k != exclude:
                siblings[k] = v
        return siblings

    def _input() -> float | None:
        if _has_override("input"):
            return override_values["input"]
        pricing: dict[str, Any] | None = meta.get("pricing")
        if isinstance(pricing, dict) and "prompt" in pricing:
            return _safe_parse_pricing_dict_price(
                "input", pricing["prompt"], _pricing_siblings("prompt")
            )
        if isinstance(pricing, dict) and "input" in pricing:
            return _safe_parse_pricing_dict_price(
                "input", pricing["input"], _pricing_siblings("input")
            )
        for upstream_key in (
            "input_price_per_1k",
            "prompt_price_per_1k",
            "prompt",
        ):
            upstream = meta.get(upstream_key)
            parsed = _safe_parse_price_per_1k("input", upstream)
            if parsed is not None:
                return parsed
        return None

    def _output() -> float | None:
        if _has_override("output"):
            return override_values["output"]
        pricing: dict[str, Any] | None = meta.get("pricing")
        if isinstance(pricing, dict) and "completion" in pricing:
            return _safe_parse_pricing_dict_price(
                "output",
                pricing["completion"],
                _pricing_siblings("completion"),
            )
        if isinstance(pricing, dict) and "output" in pricing:
            return _safe_parse_pricing_dict_price(
                "output", pricing["output"], _pricing_siblings("output")
            )
        for upstream_key in (
            "output_price_per_1k",
            "completion_price_per_1k",
            "completion",
        ):
            upstream = meta.get(upstream_key)
            parsed = _safe_parse_price_per_1k("output", upstream)
            if parsed is not None:
                return parsed
        return None

    def _cache_read() -> int | None:
        if _has_override("cache_read"):
            return int(override_values["cache_read"])
        pricing: dict[str, Any] | None = meta.get("pricing")
        if isinstance(pricing, dict):
            for nested_key in (
                "input_cache_read",
                "cache_read",
                "prompt_cache_read",
            ):
                nested = pricing.get(nested_key)
                # OpenRouter-style cache fields are dollars per token;
                # bare numeric strings need that default.
                parsed = _safe_parse_microdollars(
                    "cache_read", nested, default_unit="token"
                )
                if parsed is not None:
                    return parsed
        for upstream_key in (
            "cache_read_per_million_microdollars",
            "input_cache_read_per_million_microdollars",
        ):
            upstream = meta.get(upstream_key)
            # These legacy fields are already in microdollars per
            # million tokens.
            parsed = _safe_parse_microdollars("cache_read", upstream)
            if parsed is not None:
                return parsed
        anthropic_cost = meta.get("cache_read_input_token_cost")
        if anthropic_cost is not None:
            parsed = _safe_parse_microdollars(
                "cache_read", anthropic_cost, default_unit="token"
            )
            if parsed is not None:
                return parsed
        return None

    def _cache_write() -> int | None:
        if _has_override("cache_write"):
            return int(override_values["cache_write"])
        pricing: dict[str, Any] | None = meta.get("pricing")
        if isinstance(pricing, dict):
            for nested_key in (
                "input_cache_write",
                "cache_write",
                "prompt_cache_write",
            ):
                nested = pricing.get(nested_key)
                parsed = _safe_parse_microdollars(
                    "cache_write", nested, default_unit="token"
                )
                if parsed is not None:
                    return parsed
        for upstream_key in (
            "cache_write_per_million_microdollars",
            "input_cache_write_per_million_microdollars",
        ):
            upstream = meta.get(upstream_key)
            parsed = _safe_parse_microdollars("cache_write", upstream)
            if parsed is not None:
                return parsed
        anthropic_cost = meta.get("cache_creation_input_token_cost")
        if anthropic_cost is not None:
            parsed = _safe_parse_microdollars(
                "cache_write", anthropic_cost, default_unit="token"
            )
            if parsed is not None:
                return parsed
        return None

    input_price = _input()
    output_price = _output()
    cache_read_price = _cache_read()
    cache_write_price = _cache_write()

    if all(
        value is None
        for value in (
            input_price,
            output_price,
            cache_read_price,
            cache_write_price,
        )
    ):
        return None

    present_provenance: set[str] = set()
    for category_key, present in (
        ("input", input_price is not None),
        ("output", output_price is not None),
        ("cache_read", cache_read_price is not None),
        ("cache_write", cache_write_price is not None),
    ):
        if not present:
            continue
        present_provenance.add(
            SOURCE_CONFIG if _has_override(category_key) else SOURCE_UPSTREAM
        )

    if present_provenance == {SOURCE_CONFIG}:
        source = SOURCE_CONFIG
    elif present_provenance == {SOURCE_UPSTREAM}:
        source = SOURCE_UPSTREAM
    else:
        source = SOURCE_MIXED

    if source == SOURCE_CONFIG:
        source_detail = SOURCE_DETAIL_OPERATOR_OVERRIDE
        confidence = CONFIDENCE_OPERATOR
    elif source == SOURCE_UPSTREAM:
        source_detail = SOURCE_DETAIL_PROVIDER_METADATA
        confidence = CONFIDENCE_AUTHORITATIVE
    else:
        # Mixed: prefer the upstream metadata detail and mark
        # confidence as authoritative for the upstream-sourced
        # categories. Operator overrides are explicit by definition,
        # so mixed confidence stays at authoritative rather than
        # dropping to operator-only.
        source_detail = SOURCE_DETAIL_PROVIDER_METADATA
        confidence = CONFIDENCE_AUTHORITATIVE

    return ResolvedPricing(
        input_price_per_1k=input_price,
        output_price_per_1k=output_price,
        cache_read_per_million_microdollars=cache_read_price,
        cache_write_per_million_microdollars=cache_write_price,
        source=source,
        source_detail=source_detail,
        source_confidence=confidence,
        source_model_id=model_id,
        source_provider_id=provider_id,
    )
