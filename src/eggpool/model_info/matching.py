"""Tiered identity matching for model-info sources.

Builds a reverse-lookup candidate index from source catalog records
and resolves local model IDs to source records through a 5-tier
resolver: configured aliases, exact source IDs, normalized exact keys,
curated regex rules, and guarded similarity.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from eggpool.model_info.normalization import (
    normalize_model_key,
    normalize_vendor_key,
    split_source_id,
    strip_provider_namespace,
    tokenize_model_key,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from eggpool.model_info.types import SourceModelRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Critical variant tokens that must never be crossed during similarity
# matching.  A candidate whose family_tokens contain a critical token not
# present in the local model's family_tokens is rejected before scoring.
# ---------------------------------------------------------------------------
CRITICAL_VARIANT_TOKENS: frozenset[str] = frozenset(
    {
        "mini",
        "pro",
        "flash",
        "lite",
        "instruct",
        "chat",
        "reasoning",
        "thinking",
        "preview",
    }
)

# ---------------------------------------------------------------------------
# Built-in conservative regex rules (pattern -> vendor).
# ---------------------------------------------------------------------------
_BUILTIN_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)^minimax[-_\s:]*m(\d+)$"), "minimax"),
    (
        re.compile(r"(?i)^claude[-_\s]*(sonnet|opus|haiku)[-_\s]*(\d+(?:\.\d+)?)"),
        "anthropic",
    ),
    (
        re.compile(r"(?i)^gemini[-_\s]*(\d+(?:\.\d+)?)[-_\s]*(pro|flash|flash-lite)?"),
        "google",
    ),
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelInfoMatchCandidate:
    """A single normalized candidate from a source catalog."""

    source: str
    source_model_id: str
    display_name: str | None
    vendor: str | None
    raw_keys: tuple[str, ...]
    normalized_keys: tuple[str, ...]
    family_tokens: tuple[str, ...]
    version_tokens: tuple[str, ...]
    record: SourceModelRecord


@dataclass(frozen=True)
class ModelInfoCandidateIndex:
    """Reverse-lookup index built once per fetch."""

    source: str
    exact_by_source_id: dict[str, SourceModelRecord]
    by_normalized_key: dict[str, list[ModelInfoMatchCandidate]]
    by_vendor_and_key: dict[tuple[str, str], list[ModelInfoMatchCandidate]]


@dataclass(frozen=True)
class MatchDecision:
    """Result of a tiered match attempt."""

    record: SourceModelRecord | None
    matched: bool
    match_method: str
    confidence: float
    diagnostics: dict[str, object]
    alias_to_persist: str | None = None
    alias_to_persist_provider_id: str | None = None


@dataclass(frozen=True)
class ModelInfoMatchingConfig:
    """Feature flags and thresholds for tiered matching."""

    enabled: bool = True
    normalized_exact: bool = True
    regex_rules: bool = True
    similarity: bool = False
    similarity_threshold: float = 0.92
    similarity_min_gap: float = 0.05
    persist_discovered_aliases: bool = True
    max_candidates_per_model: int = 20


# ---------------------------------------------------------------------------
# Candidate index builder
# ---------------------------------------------------------------------------


def build_candidate_index(
    source: str,
    records: Iterable[SourceModelRecord],
) -> ModelInfoCandidateIndex:
    """Build a reverse-lookup index over a fresh source fetch.

    For each record the following raw keys are indexed:
      - ``source_model_id`` (the full slash-delimited ID)
      - the model segment from ``split_source_id``
      - ``display_name``

    Two lookup maps are populated:

    * ``exact_by_source_id`` -- raw source_model_id -> record
    * ``by_normalized_key`` -- normalize_model_key(key) -> list of candidates
    * ``by_vendor_and_key`` -- (vendor_norm, norm_key) -> list of candidates
    """
    exact: dict[str, SourceModelRecord] = {}
    by_norm: dict[str, list[ModelInfoMatchCandidate]] = {}
    by_vk: dict[tuple[str, str], list[ModelInfoMatchCandidate]] = {}

    for record in records:
        src_model_id: str = record.source_model_id
        display: str | None = record.display_name
        vendor, model_segment = split_source_id(src_model_id)

        raw_keys: list[str] = [src_model_id, model_segment]
        if display:
            raw_keys.append(display)

        norm_keys: list[str] = []
        for k in raw_keys:
            if k:
                nk = normalize_model_key(k)
                if nk:
                    norm_keys.append(nk)

        family_tokens = tokenize_model_key(model_segment)
        version_tokens = tuple(t for t in family_tokens if t.isdigit())
        vendor_norm = normalize_vendor_key(vendor)

        candidate = ModelInfoMatchCandidate(
            source=source,
            source_model_id=src_model_id,
            display_name=display,
            vendor=vendor,
            raw_keys=tuple(raw_keys),
            normalized_keys=tuple(norm_keys),
            family_tokens=family_tokens,
            version_tokens=version_tokens,
            record=record,
        )

        exact[src_model_id] = record

        for nk in norm_keys:
            by_norm.setdefault(nk, []).append(candidate)

        if vendor_norm is not None:
            for nk in norm_keys:
                by_vk.setdefault((vendor_norm, nk), []).append(candidate)

    return ModelInfoCandidateIndex(
        source=source,
        exact_by_source_id=exact,
        by_normalized_key=by_norm,
        by_vendor_and_key=by_vk,
    )


# ---------------------------------------------------------------------------
# Local candidate builder (for a single model_id being resolved)
# ---------------------------------------------------------------------------


def _build_local_candidates(
    model_id: str,
    display_name: str | None,
    provider_catalog_aliases: list[str],
    known_provider_namespaces: set[str] | None,
) -> list[str]:
    """Build raw local candidate strings to try against the index."""
    candidates: list[str] = [model_id]
    if display_name:
        candidates.append(display_name)
    candidates.extend(provider_catalog_aliases)
    if known_provider_namespaces:
        stripped = strip_provider_namespace(model_id, known_provider_namespaces)
        if stripped != model_id:
            candidates.append(stripped)
    return candidates


def _vendor_from_model_id(model_id: str) -> str | None:
    """Infer a vendor token from a local model_id (best-effort).

    Returns the first non-numeric token if it looks like a vendor prefix,
    or None.
    """
    tokens = tokenize_model_key(model_id)
    for t in tokens:
        if not t.isdigit():
            return t
    return None


# ---------------------------------------------------------------------------
# Tier 0: configured exact alias
# ---------------------------------------------------------------------------


async def _tier_configured_exact_alias(
    *,
    model_id: str,
    source: str,
    repo: Any,
    candidate_index: ModelInfoCandidateIndex,
) -> MatchDecision | None:
    """Tier 0: look up configured alias rows for this source."""
    alias_rows = await repo.list_alias_rows_for_model(model_id, source=source)
    if not alias_rows:
        return None

    # Deduplicate alias strings, preferring exact-case matches.
    exact_case = [r for r in alias_rows if r.get("model_id") == model_id]
    selected = exact_case if exact_case else alias_rows

    seen: set[str] = set()
    unique_aliases: list[str] = []
    alias_confidence: dict[str, float | None] = {}
    for row in selected:
        alias_str = row.get("alias")
        if not isinstance(alias_str, str) or alias_str in seen:
            continue
        seen.add(alias_str)
        unique_aliases.append(alias_str)
        alias_confidence[alias_str] = row.get("confidence")

    indexed: list[str] = [
        a for a in unique_aliases if a in candidate_index.exact_by_source_id
    ]

    if len(indexed) == 0:
        return None

    if len(indexed) == 1:
        alias_str = indexed[0]
        record = candidate_index.exact_by_source_id[alias_str]
        conf = alias_confidence.get(alias_str)
        capped = min(float(conf) if conf is not None else 0.9, 1.0)
        return MatchDecision(
            record=record,
            matched=True,
            match_method="configured_exact_alias",
            confidence=capped,
            diagnostics={
                "alias": alias_str,
                "source_model_id": record.source_model_id,  # type: ignore[union-attr]
            },
            alias_to_persist=None,
        )

    # Multiple indexed aliases -- ambiguous.
    return MatchDecision(
        record=None,
        matched=False,
        match_method="ambiguous_configured_aliases",
        confidence=0.0,
        diagnostics={"indexed_aliases": indexed, "unique_aliases": unique_aliases},
    )


# ---------------------------------------------------------------------------
# Tier 1: exact source ID
# ---------------------------------------------------------------------------


def _tier_exact_source_id(
    *,
    local_raw_candidates: list[str],
    candidate_index: ModelInfoCandidateIndex,
) -> MatchDecision | None:
    """Tier 1: try exact source_model_id matches."""
    for raw in local_raw_candidates:
        record = candidate_index.exact_by_source_id.get(raw)
        if record is not None:
            return MatchDecision(
                record=record,
                matched=True,
                match_method="exact_source_id",
                confidence=1.0,
                diagnostics={"matched_key": raw},
            )
    return None


# ---------------------------------------------------------------------------
# Tier 2: normalized exact
# ---------------------------------------------------------------------------


def _tier_normalized_exact(
    *,
    local_raw_candidates: list[str],
    local_vendor_token: str | None,
    candidate_index: ModelInfoCandidateIndex,
) -> MatchDecision | None:
    """Tier 2: normalized exact key match."""
    norm_keys_seen: dict[str, list[str]] = {}
    for raw in local_raw_candidates:
        nk = normalize_model_key(raw)
        if nk:
            norm_keys_seen.setdefault(nk, []).append(raw)

    # Collect all candidate matches across all local normalized keys.
    all_matches: list[ModelInfoMatchCandidate] = []
    seen_candidates: set[int] = set()
    matched_via: dict[int, str] = {}

    for nk in norm_keys_seen:
        for cand in candidate_index.by_normalized_key.get(nk, []):
            cand_id = id(cand.record)
            if cand_id not in seen_candidates:
                seen_candidates.add(cand_id)
                all_matches.append(cand)
                matched_via[cand_id] = nk

    if len(all_matches) == 0:
        return None

    if len(all_matches) == 1:
        cand = all_matches[0]
        return MatchDecision(
            record=cand.record,
            matched=True,
            match_method="normalized_exact",
            confidence=0.85,
            diagnostics={
                "matched_source_model_id": cand.source_model_id,
                "matched_normalized_key": matched_via[id(cand.record)],
                "local_vendor_token": local_vendor_token,
                "candidate_vendor": cand.vendor,
            },
            alias_to_persist=cand.source_model_id,
        )

    # Multiple matches -- try vendor/family tie-break.
    if local_vendor_token is not None:
        vendor_norm = normalize_vendor_key(local_vendor_token)
        vendor_filtered = [
            c
            for c in all_matches
            if normalize_vendor_key(c.vendor) == vendor_norm
            or (
                c.family_tokens
                and normalize_model_key(c.family_tokens[0])
                == normalize_model_key(local_vendor_token)
            )
        ]
        if len(vendor_filtered) == 1:
            cand = vendor_filtered[0]
            return MatchDecision(
                record=cand.record,
                matched=True,
                match_method="normalized_exact",
                confidence=0.75,
                diagnostics={
                    "matched_source_model_id": cand.source_model_id,
                    "tie_break": "vendor_match",
                    "local_vendor_token": local_vendor_token,
                    "candidate_vendor": cand.vendor,
                },
                alias_to_persist=cand.source_model_id,
            )

    # Ambiguous.
    return MatchDecision(
        record=None,
        matched=False,
        match_method="ambiguous_candidates",
        confidence=0.0,
        diagnostics={
            "candidate_count": len(all_matches),
            "candidates": [
                {
                    "source_model_id": c.source_model_id,
                    "vendor": c.vendor,
                    "display_name": c.display_name,
                }
                for c in all_matches
            ],
        },
    )


# ---------------------------------------------------------------------------
# Tier 3: regex rule
# ---------------------------------------------------------------------------


def _tier_regex_rule(
    *,
    model_id: str,
    local_vendor_token: str | None,
    candidate_index: ModelInfoCandidateIndex,
) -> MatchDecision | None:
    """Tier 3: built-in conservative regex rules."""
    local_family = tokenize_model_key(model_id)

    for pattern, vendor in _BUILTIN_RULES:
        if not pattern.search(model_id):
            continue

        # Look up candidates by vendor + normalized key.
        vendor_norm = normalize_vendor_key(vendor)
        if vendor_norm is None:
            continue

        matching_cands: list[ModelInfoMatchCandidate] = []
        seen_ids: set[int] = set()
        for cands in candidate_index.by_vendor_and_key.values():
            for c in cands:
                if normalize_vendor_key(c.vendor) != vendor_norm:
                    continue
                cid = id(c.record)
                if cid in seen_ids:
                    continue
                # Require family token overlap for safety.
                if (
                    c.family_tokens
                    and local_family
                    and not (set(c.family_tokens) & set(local_family))
                ):
                    continue
                # Reject candidates whose version tokens differ from
                # the local model when both sides carry version info.
                local_versions = tuple(t for t in local_family if t.isdigit())
                if (
                    local_versions
                    and c.version_tokens
                    and local_versions != c.version_tokens
                ):
                    continue
                # Reject candidates whose family tokens are not a
                # subset of the local model's family tokens (e.g.
                # flash vs pro, mini vs full).
                if (
                    c.family_tokens
                    and local_family
                    and set(c.family_tokens) != set(local_family)
                ):
                    continue
                seen_ids.add(cid)
                matching_cands.append(c)

        if len(matching_cands) == 1:
            cand = matching_cands[0]
            return MatchDecision(
                record=cand.record,
                matched=True,
                match_method="regex_rule",
                confidence=0.80,
                diagnostics={
                    "rule_pattern": pattern.pattern,
                    "rule_vendor": vendor,
                    "matched_source_model_id": cand.source_model_id,
                },
                alias_to_persist=cand.source_model_id,
            )

    return None


# ---------------------------------------------------------------------------
# Tier 4: similarity guarded
# ---------------------------------------------------------------------------


def _tier_similarity_guarded(
    *,
    model_id: str,
    local_vendor_token: str | None,
    candidate_index: ModelInfoCandidateIndex,
    config: ModelInfoMatchingConfig,
) -> MatchDecision | None:
    """Tier 4: guarded edit-distance similarity matching."""
    local_norm = normalize_model_key(model_id)
    if not local_norm:
        return None

    local_family = set(tokenize_model_key(model_id))

    # Build the candidate pool.
    pool: list[ModelInfoMatchCandidate] = []
    seen_ids: set[int] = set()
    for cands in candidate_index.by_normalized_key.values():
        for c in cands:
            cid = id(c.record)
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            pool.append(c)

    # Filter: same vendor if known.
    if local_vendor_token is not None:
        vn = normalize_vendor_key(local_vendor_token)
        pool = [c for c in pool if normalize_vendor_key(c.vendor) == vn]
        if not pool:
            return None

    # Filter: must not cross critical variant tokens.
    filtered: list[ModelInfoMatchCandidate] = []
    for c in pool:
        cand_variants: set[str] = set(c.family_tokens) if c.family_tokens else set()
        if cand_variants and local_family:
            cand_only_critical = cand_variants & CRITICAL_VARIANT_TOKENS
            local_only_critical = local_family & CRITICAL_VARIANT_TOKENS
            if cand_only_critical != local_only_critical:
                continue
        filtered.append(c)

    if not filtered:
        return None

    # Score candidates: try all normalized keys, keep best per candidate.
    scored: list[tuple[float, ModelInfoMatchCandidate]] = []
    for c in filtered:
        best_cand_score = 0.0
        for nk in c.normalized_keys:
            score = SequenceMatcher(None, local_norm, nk).ratio()
            if score > best_cand_score:
                best_cand_score = score
        scored.append((best_cand_score, c))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_cand = scored[0]
    second_best = scored[1][0] if len(scored) > 1 else 0.0

    if best_score < config.similarity_threshold:
        return None
    if (best_score - second_best) < config.similarity_min_gap:
        return None

    confidence = min(best_score * 0.70, 0.70)
    return MatchDecision(
        record=best_cand.record,
        matched=True,
        match_method="similarity_guarded",
        confidence=confidence,
        diagnostics={
            "best_score": best_score,
            "second_best_score": second_best,
            "gap": best_score - second_best,
            "matched_source_model_id": best_cand.source_model_id,
        },
        alias_to_persist=best_cand.source_model_id,
    )


# ---------------------------------------------------------------------------
# Public tiered resolver
# ---------------------------------------------------------------------------


async def resolve_source_record_tiered(
    *,
    source: str,
    model_id: str,
    provider_id: str | None,
    display_name: str | None,
    repo: Any,
    candidate_index: ModelInfoCandidateIndex,
    config: ModelInfoMatchingConfig | None = None,
    known_provider_namespaces: set[str] | None = None,
) -> MatchDecision:
    """Resolve a local model_id to a source record via tiered matching.

    Tier order:

      0. configured_exact_alias
      1. exact_source_id
      2. normalized_exact
      3. regex_rule
      4. similarity_guarded

    Returns a :class:`MatchDecision` with diagnostics explaining the
    outcome.
    """
    if config is None:
        config = ModelInfoMatchingConfig()
    if not config.enabled:
        return MatchDecision(
            record=None,
            matched=False,
            match_method="no_match",
            confidence=0.0,
            diagnostics={"reason": "matching_disabled"},
        )

    # Build local raw candidates.
    provider_aliases = await repo.list_alias_rows_for_model(
        model_id, source="provider_catalog"
    )
    alias_strings = [
        r["alias"] for r in provider_aliases if isinstance(r.get("alias"), str)
    ]
    local_raw = _build_local_candidates(
        model_id, display_name, alias_strings, known_provider_namespaces
    )
    local_vendor = _vendor_from_model_id(model_id)

    # Tier 0: configured exact alias.
    t0 = await _tier_configured_exact_alias(
        model_id=model_id,
        source=source,
        repo=repo,
        candidate_index=candidate_index,
    )
    if t0 is not None:
        return t0

    # Tier 1: exact source ID.
    t1 = _tier_exact_source_id(
        local_raw_candidates=local_raw,
        candidate_index=candidate_index,
    )
    if t1 is not None:
        return t1

    # Tier 2: normalized exact.
    if config.normalized_exact:
        t2 = _tier_normalized_exact(
            local_raw_candidates=local_raw,
            local_vendor_token=local_vendor,
            candidate_index=candidate_index,
        )
        if t2 is not None:
            return t2

    # Tier 3: regex rules.
    if config.regex_rules:
        t3 = _tier_regex_rule(
            model_id=model_id,
            local_vendor_token=local_vendor,
            candidate_index=candidate_index,
        )
        if t3 is not None:
            return t3

    # Tier 4: similarity guarded.
    if config.similarity:
        t4 = _tier_similarity_guarded(
            model_id=model_id,
            local_vendor_token=local_vendor,
            candidate_index=candidate_index,
            config=config,
        )
        if t4 is not None:
            return t4

    return MatchDecision(
        record=None,
        matched=False,
        match_method="no_match",
        confidence=0.0,
        diagnostics={
            "source": source,
            "model_id": model_id,
            "local_raw_candidates": local_raw,
            "local_vendor_token": local_vendor,
            "candidate_index_size": len(candidate_index.exact_by_source_id),
        },
    )
