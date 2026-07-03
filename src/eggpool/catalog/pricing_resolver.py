"""Structured price resolution pipeline.

This module extracts pricing resolution out of
``CatalogService._maybe_insert_price_snapshot`` so that callers receive
structured per-category pricing plus provenance metadata instead of a
flat tuple of values.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, cast

from eggpool.catalog.pricing import (
    extract_price_decimal,
    price_unit,
    rate_is_implausible,
)
from eggpool.constants import clamp_sqlite_integer

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

PriceCategory = Literal["input", "output", "cache_read", "cache_write"]
PriceUnit = Literal[
    "dollars_per_token",
    "dollars_per_1k",
    "dollars_per_million",
    "microdollars_per_million",
]
UnitEvidence = Literal[
    "operator_override",
    "explicit_suffix",
    "field_name",
    "sibling_consensus",
    "numeric_scale",
    "safe_default",
]
CategoryConfidence = Literal["high", "medium", "low"]

_CATEGORY_ORDER: tuple[PriceCategory, ...] = (
    "input",
    "output",
    "cache_read",
    "cache_write",
)
_HEURISTIC_EVIDENCE = frozenset({"sibling_consensus", "numeric_scale", "safe_default"})
_PRICING_PER_TOKEN_CEILING = Decimal("0.001")

_UNIT_FROM_SUFFIX: dict[str, PriceUnit] = {
    "token": "dollars_per_token",
    "1k": "dollars_per_1k",
    "million": "dollars_per_million",
}


@dataclass(frozen=True)
class RawPriceCandidate:
    """One raw price field discovered in metadata or operator overrides."""

    category: PriceCategory
    raw_value: object
    path: tuple[str, ...]
    field_name: str
    explicit_unit: PriceUnit | None
    field_unit_hint: PriceUnit | None
    numeric_value: Decimal | None
    cluster: str
    priority: int


@dataclass(frozen=True)
class ResolvedPriceCategory:
    """Resolved per-category pricing plus unit provenance."""

    category: PriceCategory
    microdollars_per_million: int | None
    legacy_price_per_1k: float | None
    unit: PriceUnit | None
    evidence: UnitEvidence | None
    confidence: CategoryConfidence
    path: tuple[str, ...]
    raw_value: object | None = None


@dataclass(frozen=True)
class ResolvedPricing:
    """Structured pricing resolution result."""

    input_price_per_1k: float | None
    output_price_per_1k: float | None
    cache_read_per_million_microdollars: int | None
    cache_write_per_million_microdollars: int | None
    source: str  # one of SOURCE_*
    source_detail: str  # one of SOURCE_DETAIL_*
    source_confidence: str  # one of CONFIDENCE_*
    source_model_id: str | None = None  # external catalog model ID
    source_provider_id: str | None = None  # external catalog provider ID
    category_details: tuple[ResolvedPriceCategory, ...] = ()

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

    def detail_for(self, category: PriceCategory) -> ResolvedPriceCategory | None:
        for detail in self.category_details:
            if detail.category == category:
                return detail
        return None


@dataclass(frozen=True)
class _ClusterInference:
    unit: PriceUnit
    evidence: UnitEvidence
    mixed_hints: bool


@dataclass(frozen=True)
class _AliasSpec:
    field_name: str
    path: tuple[str, ...]
    bare_top_level: bool = False


_INPUT_ALIASES: tuple[_AliasSpec, ...] = (
    _AliasSpec("prompt", ("pricing", "prompt")),
    _AliasSpec("input", ("pricing", "input")),
    _AliasSpec("input_price_per_1k", ("input_price_per_1k",)),
    _AliasSpec("prompt_price_per_1k", ("prompt_price_per_1k",)),
    _AliasSpec("input_usd_per_million", ("input_usd_per_million",)),
    _AliasSpec("prompt_usd_per_million", ("prompt_usd_per_million",)),
    _AliasSpec(
        "input_per_million_microdollars",
        ("input_per_million_microdollars",),
    ),
    _AliasSpec(
        "prompt_per_million_microdollars",
        ("prompt_per_million_microdollars",),
    ),
    _AliasSpec("prompt", ("prompt",), bare_top_level=True),
)

_OUTPUT_ALIASES: tuple[_AliasSpec, ...] = (
    _AliasSpec("completion", ("pricing", "completion")),
    _AliasSpec("output", ("pricing", "output")),
    _AliasSpec("output_price_per_1k", ("output_price_per_1k",)),
    _AliasSpec("completion_price_per_1k", ("completion_price_per_1k",)),
    _AliasSpec("output_usd_per_million", ("output_usd_per_million",)),
    _AliasSpec("completion_usd_per_million", ("completion_usd_per_million",)),
    _AliasSpec(
        "output_per_million_microdollars",
        ("output_per_million_microdollars",),
    ),
    _AliasSpec(
        "completion_per_million_microdollars",
        ("completion_per_million_microdollars",),
    ),
    _AliasSpec("completion", ("completion",), bare_top_level=True),
)

_CACHE_READ_ALIASES: tuple[_AliasSpec, ...] = (
    _AliasSpec("input_cache_read", ("pricing", "input_cache_read")),
    _AliasSpec("cache_read", ("pricing", "cache_read")),
    _AliasSpec("prompt_cache_read", ("pricing", "prompt_cache_read")),
    _AliasSpec(
        "cache_read_per_million_microdollars",
        ("cache_read_per_million_microdollars",),
    ),
    _AliasSpec(
        "input_cache_read_per_million_microdollars",
        ("input_cache_read_per_million_microdollars",),
    ),
    _AliasSpec("cache_read_usd_per_million", ("cache_read_usd_per_million",)),
    _AliasSpec(
        "input_cache_read_usd_per_million",
        ("input_cache_read_usd_per_million",),
    ),
    _AliasSpec("cache_read_input_token_cost", ("cache_read_input_token_cost",)),
)

_CACHE_WRITE_ALIASES: tuple[_AliasSpec, ...] = (
    _AliasSpec("input_cache_write", ("pricing", "input_cache_write")),
    _AliasSpec("cache_write", ("pricing", "cache_write")),
    _AliasSpec("prompt_cache_write", ("pricing", "prompt_cache_write")),
    _AliasSpec(
        "cache_write_per_million_microdollars",
        ("cache_write_per_million_microdollars",),
    ),
    _AliasSpec(
        "input_cache_write_per_million_microdollars",
        ("input_cache_write_per_million_microdollars",),
    ),
    _AliasSpec("cache_write_usd_per_million", ("cache_write_usd_per_million",)),
    _AliasSpec(
        "input_cache_write_usd_per_million",
        ("input_cache_write_usd_per_million",),
    ),
    _AliasSpec(
        "cache_creation_input_token_cost",
        ("cache_creation_input_token_cost",),
    ),
)

_ALIASES_BY_CATEGORY: dict[PriceCategory, tuple[_AliasSpec, ...]] = {
    "input": _INPUT_ALIASES,
    "output": _OUTPUT_ALIASES,
    "cache_read": _CACHE_READ_ALIASES,
    "cache_write": _CACHE_WRITE_ALIASES,
}


def _field_unit_hint(field_name: str) -> PriceUnit | None:
    if field_name.endswith("_price_per_1k"):
        return "dollars_per_1k"
    if field_name.endswith("_per_million_microdollars"):
        return "microdollars_per_million"
    if field_name.endswith("_usd_per_million") or field_name.endswith(
        "_dollars_per_million"
    ):
        return "dollars_per_million"
    if (
        field_name.endswith("_input_token_cost")
        or field_name.endswith("_token_cost")
        or field_name.endswith("_per_token")
    ):
        return "dollars_per_token"
    return None


def _category_confidence(evidence: UnitEvidence) -> CategoryConfidence:
    if evidence in {"operator_override", "explicit_suffix", "field_name"}:
        return "high"
    if evidence in {"sibling_consensus", "numeric_scale"}:
        return "medium"
    return "low"


def _path_value(meta: dict[str, Any], path: tuple[str, ...]) -> tuple[bool, object]:
    if len(path) == 1:
        key = path[0]
        if key in meta:
            return True, meta[key]
        return False, None

    if path[0] != "pricing":
        return False, None
    pricing = meta.get("pricing")
    if not isinstance(pricing, dict):
        return False, None
    pricing_dict = cast("dict[str, Any]", pricing)
    key = path[1]
    if key in pricing_dict:
        return True, pricing_dict[key]
    return False, None


def _candidate_cluster(path: tuple[str, ...]) -> str:
    if path and path[0] == "pricing":
        return "pricing"
    return "top_level"


def _build_candidate(
    *,
    category: PriceCategory,
    raw_value: object,
    path: tuple[str, ...],
    priority: int,
) -> RawPriceCandidate | None:
    field_name = path[-1]
    explicit_suffix = price_unit(raw_value)
    explicit_unit = (
        _UNIT_FROM_SUFFIX[explicit_suffix] if explicit_suffix is not None else None
    )
    numeric_value: Decimal | None
    try:
        numeric_value = extract_price_decimal(raw_value)
    except ValueError as exc:
        logger.warning(
            "Ignoring invalid %s price at %s: %s",
            category,
            ".".join(path),
            exc,
        )
        return None
    if numeric_value is None:
        if raw_value is not None:
            logger.warning(
                "Ignoring empty or negative %s price at %s: %r",
                category,
                ".".join(path),
                raw_value,
            )
        return None
    return RawPriceCandidate(
        category=category,
        raw_value=raw_value,
        path=path,
        field_name=field_name,
        explicit_unit=explicit_unit,
        field_unit_hint=_field_unit_hint(field_name),
        numeric_value=numeric_value,
        cluster=_candidate_cluster(path),
        priority=priority,
    )


def _collect_category_candidates(
    *,
    meta: dict[str, Any],
    category: PriceCategory,
    specs: tuple[_AliasSpec, ...],
) -> list[RawPriceCandidate]:
    candidates: list[RawPriceCandidate] = []
    explicit_top_level_present = any(
        not spec.bare_top_level
        and len(spec.path) == 1
        and _path_value(meta, spec.path)[0]
        for spec in specs
    )
    for priority, spec in enumerate(specs):
        if spec.bare_top_level and explicit_top_level_present:
            continue
        present, raw_value = _path_value(meta, spec.path)
        if not present:
            continue
        candidate = _build_candidate(
            category=category,
            raw_value=raw_value,
            path=spec.path,
            priority=priority,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _unit_regime(unit: PriceUnit) -> str:
    if unit == "dollars_per_token":
        return "token"
    if unit == "dollars_per_1k":
        return "1k"
    return "million"


def _regime_to_unit(regime: str) -> PriceUnit:
    if regime == "token":
        return "dollars_per_token"
    if regime == "1k":
        return "dollars_per_1k"
    return "dollars_per_million"


def _infer_cluster(candidates: list[RawPriceCandidate]) -> _ClusterInference:
    hinted_regimes = [
        _unit_regime(unit)
        for candidate in candidates
        for unit in (
            candidate.explicit_unit,
            None if candidate.explicit_unit is not None else candidate.field_unit_hint,
        )
        if unit is not None
    ]
    if hinted_regimes:
        counts = Counter(hinted_regimes)
        regime, count = counts.most_common(1)[0]
        leaders = sum(1 for value in counts.values() if value == count)
        mixed_hints = len(counts) > 1
        if leaders == 1:
            return _ClusterInference(
                unit=_regime_to_unit(regime),
                evidence="sibling_consensus",
                mixed_hints=mixed_hints,
            )

    magnitudes = [
        candidate.numeric_value
        for candidate in candidates
        if candidate.explicit_unit is None
        and candidate.field_unit_hint is None
        and candidate.numeric_value is not None
    ]
    if len(magnitudes) > 1:
        per_token_count = sum(
            1 for value in magnitudes if value < _PRICING_PER_TOKEN_CEILING
        )
        per_million_count = len(magnitudes) - per_token_count
        if per_token_count > per_million_count and per_token_count > 0:
            return _ClusterInference(
                unit="dollars_per_token",
                evidence="numeric_scale",
                mixed_hints=len(set(hinted_regimes)) > 1,
            )
        if per_million_count > per_token_count and per_million_count > 0:
            return _ClusterInference(
                unit="dollars_per_million",
                evidence="numeric_scale",
                mixed_hints=len(set(hinted_regimes)) > 1,
            )

    return _ClusterInference(
        unit="dollars_per_million",
        evidence="safe_default",
        mixed_hints=len(set(hinted_regimes)) > 1,
    )


def _normalize_microdollars_per_million(
    value: Decimal,
    unit: PriceUnit,
) -> int:
    if unit == "dollars_per_token":
        normalized = value * Decimal(1_000_000_000_000)
    elif unit == "dollars_per_1k":
        normalized = value * Decimal(1_000_000_000)
    elif unit == "dollars_per_million":
        normalized = value * Decimal(1_000_000)
    else:
        normalized = value
    return clamp_sqlite_integer(int(normalized.to_integral_value()))


def _resolve_candidate(
    candidate: RawPriceCandidate,
    *,
    cluster_inference: _ClusterInference,
) -> ResolvedPriceCategory:
    if candidate.explicit_unit is not None:
        unit = candidate.explicit_unit
        evidence: UnitEvidence = "explicit_suffix"
    elif candidate.field_unit_hint is not None:
        unit = candidate.field_unit_hint
        evidence = "field_name"
    else:
        unit = cluster_inference.unit
        evidence = cluster_inference.evidence

    numeric_value = candidate.numeric_value
    microdollars = (
        _normalize_microdollars_per_million(numeric_value, unit)
        if numeric_value is not None
        else None
    )
    legacy_price = (
        float(microdollars / 1_000_000_000)
        if microdollars is not None and candidate.category in {"input", "output"}
        else None
    )
    return ResolvedPriceCategory(
        category=candidate.category,
        microdollars_per_million=microdollars,
        legacy_price_per_1k=legacy_price,
        unit=unit,
        evidence=evidence,
        confidence=_category_confidence(evidence),
        path=candidate.path,
        raw_value=candidate.raw_value,
    )


def _override_category(
    category: PriceCategory,
    value: object,
) -> ResolvedPriceCategory:
    if category in {"input", "output"}:
        price_per_1k = cast("float", value)
        microdollars = clamp_sqlite_integer(int(round(price_per_1k * 1_000_000_000)))
        unit: PriceUnit = "dollars_per_1k"
        legacy_price = price_per_1k
    else:
        microdollars = clamp_sqlite_integer(int(cast("int", value)))
        unit = "microdollars_per_million"
        legacy_price = None
    return ResolvedPriceCategory(
        category=category,
        microdollars_per_million=microdollars,
        legacy_price_per_1k=legacy_price,
        unit=unit,
        evidence="operator_override",
        confidence="high",
        path=("override_values", category),
        raw_value=value,
    )


def _resolved_pricing_from_details(
    *,
    details: list[ResolvedPriceCategory],
    source: str,
    source_detail: str,
    source_confidence: str,
    source_model_id: str | None,
    source_provider_id: str | None,
) -> ResolvedPricing:
    detail_by_category = {detail.category: detail for detail in details}
    return ResolvedPricing(
        input_price_per_1k=(
            detail_by_category["input"].legacy_price_per_1k
            if "input" in detail_by_category
            else None
        ),
        output_price_per_1k=(
            detail_by_category["output"].legacy_price_per_1k
            if "output" in detail_by_category
            else None
        ),
        cache_read_per_million_microdollars=(
            detail_by_category["cache_read"].microdollars_per_million
            if "cache_read" in detail_by_category
            else None
        ),
        cache_write_per_million_microdollars=(
            detail_by_category["cache_write"].microdollars_per_million
            if "cache_write" in detail_by_category
            else None
        ),
        source=source,
        source_detail=source_detail,
        source_confidence=source_confidence,
        source_model_id=source_model_id,
        source_provider_id=source_provider_id,
        category_details=tuple(
            sorted(details, key=lambda detail: _CATEGORY_ORDER.index(detail.category))
        ),
    )


def resolve_pricing_from_metadata(
    *,
    model_id: str,
    provider_id: str,
    model_info: dict[str, Any],
    override_values: dict[str, Any],
) -> ResolvedPricing | None:
    """Resolve pricing from operator overrides + upstream metadata."""
    raw_meta = model_info.get("source_metadata", {})
    meta = cast("dict[str, Any]", raw_meta) if isinstance(raw_meta, dict) else {}

    details: list[ResolvedPriceCategory] = []
    present_provenance: set[str] = set()

    candidate_map: dict[PriceCategory, list[RawPriceCandidate]] = {}
    all_candidates: list[RawPriceCandidate] = []
    for category in _CATEGORY_ORDER:
        override_value = override_values.get(category)
        if override_value is not None:
            details.append(_override_category(category, override_value))
            present_provenance.add(SOURCE_CONFIG)
            continue
        candidates = _collect_category_candidates(
            meta=meta,
            category=category,
            specs=_ALIASES_BY_CATEGORY[category],
        )
        if candidates:
            candidate_map[category] = candidates
            all_candidates.extend(candidates)

    cluster_candidates: dict[str, list[RawPriceCandidate]] = defaultdict(list)
    for candidate in all_candidates:
        cluster_candidates[candidate.cluster].append(candidate)
    cluster_inferences = {
        cluster: _infer_cluster(cluster_group)
        for cluster, cluster_group in cluster_candidates.items()
    }

    for cluster, inference in cluster_inferences.items():
        if inference.mixed_hints and any(
            candidate.explicit_unit is None and candidate.field_unit_hint is None
            for candidate in cluster_candidates[cluster]
        ):
            logger.warning(
                "Pricing cluster has mixed unit evidence for %s/%s "
                "(cluster=%s); falling back to %s via %s",
                provider_id,
                model_id,
                cluster,
                inference.unit,
                inference.evidence,
            )

    for category in _CATEGORY_ORDER:
        if category not in candidate_map:
            continue
        selected = sorted(
            candidate_map[category],
            key=lambda candidate: candidate.priority,
        )[0]
        resolved = _resolve_candidate(
            selected,
            cluster_inference=cluster_inferences[selected.cluster],
        )
        if resolved.evidence == "safe_default":
            logger.warning(
                "Ambiguous bare pricing defaulted to dollars-per-million for "
                "%s/%s category=%s path=%s raw=%r",
                provider_id,
                model_id,
                category,
                ".".join(resolved.path),
                resolved.raw_value,
            )
        details.append(resolved)
        present_provenance.add(SOURCE_UPSTREAM)

    if not details:
        return None

    if present_provenance == {SOURCE_CONFIG}:
        source = SOURCE_CONFIG
        source_detail = SOURCE_DETAIL_OPERATOR_OVERRIDE
        confidence = CONFIDENCE_OPERATOR
    elif present_provenance == {SOURCE_UPSTREAM}:
        source = SOURCE_UPSTREAM
        source_detail = SOURCE_DETAIL_PROVIDER_METADATA
        confidence = (
            CONFIDENCE_HEURISTIC
            if any(detail.evidence in _HEURISTIC_EVIDENCE for detail in details)
            else CONFIDENCE_AUTHORITATIVE
        )
    else:
        source = SOURCE_MIXED
        source_detail = SOURCE_DETAIL_PROVIDER_METADATA
        confidence = (
            CONFIDENCE_HEURISTIC
            if any(detail.evidence in _HEURISTIC_EVIDENCE for detail in details)
            else CONFIDENCE_AUTHORITATIVE
        )

    return _resolved_pricing_from_details(
        details=details,
        source=source,
        source_detail=source_detail,
        source_confidence=confidence,
        source_model_id=model_id,
        source_provider_id=provider_id,
    )


def apply_snapshot_trust_gates(
    resolved: ResolvedPricing,
    *,
    model_id: str,
    provider_id: str,
) -> ResolvedPricing | None:
    """Discard implausible per-category rates before snapshot persistence."""
    details = list(resolved.category_details)
    if not details:
        if resolved.input_price_per_1k is not None:
            details.append(_override_category("input", resolved.input_price_per_1k))
        if resolved.output_price_per_1k is not None:
            details.append(_override_category("output", resolved.output_price_per_1k))
        if resolved.cache_read_per_million_microdollars is not None:
            details.append(
                _override_category(
                    "cache_read",
                    resolved.cache_read_per_million_microdollars,
                )
            )
        if resolved.cache_write_per_million_microdollars is not None:
            details.append(
                _override_category(
                    "cache_write",
                    resolved.cache_write_per_million_microdollars,
                )
            )

    kept: list[ResolvedPriceCategory] = []
    rejected_count = 0
    for detail in details:
        rate = detail.microdollars_per_million
        if rate is None:
            continue
        if rate < 0 or rate_is_implausible(rate):
            rejected_count += 1
            logger.warning(
                "Rejecting implausible pricing category for %s/%s "
                "category=%s path=%s raw=%r unit=%s evidence=%s "
                "normalized_microdollars_per_million=%s",
                provider_id,
                model_id,
                detail.category,
                ".".join(detail.path),
                detail.raw_value,
                detail.unit,
                detail.evidence,
                rate,
            )
            continue
        kept.append(detail)

    if not kept:
        if rejected_count > 0:
            logger.warning(
                "Skipping price snapshot insertion for %s/%s; every resolved "
                "category was rejected by trust gates.",
                provider_id,
                model_id,
            )
        return None

    if rejected_count == 0:
        return resolved

    source_confidence = (
        CONFIDENCE_HEURISTIC
        if resolved.source_confidence != CONFIDENCE_OPERATOR
        else resolved.source_confidence
    )
    return _resolved_pricing_from_details(
        details=kept,
        source=resolved.source,
        source_detail=resolved.source_detail,
        source_confidence=source_confidence,
        source_model_id=resolved.source_model_id or model_id,
        source_provider_id=resolved.source_provider_id or provider_id,
    )
