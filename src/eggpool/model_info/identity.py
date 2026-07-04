"""Identity resolution for model-info sources.

Maps local Eggpool model IDs to source-specific model IDs using exact
alias matching only.  No fuzzy, substring, or edit-distance matching.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.model_info.repository import ModelInfoRepository
    from eggpool.model_info.types import SourceModelRecord

logger = logging.getLogger(__name__)


def choose_alias_candidates(
    requested_model_id: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pick deterministic alias candidate rows for the requested model.

    Rules (Phase 1 of the OpenRouter polish closeout plan):

    1. Prefer rows whose stored ``model_id`` equals
       ``requested_model_id`` exactly (exact-case match).
    2. Fall back to case-insensitive rows when no exact-case row exists.
    3. Preserve insertion order from the underlying SQL query.
    4. Annotate each surviving row with ``match_kind``:

       * ``"exact_case"`` — stored model_id equals requested.
       * ``"case_folded"`` — case-insensitive lookup only.

    The caller (``resolve_openrouter_record``) decides what to do with
    the candidate set: a single unique alias resolves, multiple
    distinct aliases may be filtered against the indexed catalog,
    conflicting aliases are reported as ambiguous.
    """
    exact = [r for r in rows if r.get("model_id") == requested_model_id]
    selected = exact if exact else list(rows)
    out: list[dict[str, Any]] = []
    for row in selected:
        enriched = dict(row)
        enriched["match_kind"] = "exact_case" if exact else "case_folded"
        out.append(enriched)
    return out


def dedupe_alias_strings(rows: list[dict[str, Any]]) -> list[str]:
    """Return unique alias strings from rows, preserving order.

    Multiple alias rows for the same stored model_id pointing to the
    same source-id (e.g. ``MiniMax-M3 -> minimax/minimax-m3`` and
    ``minimax-m3 -> minimax/minimax-m3``) must collapse to a single
    candidate so the resolver sees one unique alias instead of false
    ambiguity.
    """
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        alias = row.get("alias")
        if not isinstance(alias, str):
            continue
        if alias in seen:
            continue
        seen.add(alias)
        out.append(alias)
    return out


async def resolve_openrouter_record(
    model_id: str,
    repo: ModelInfoRepository,
    openrouter_indexed: dict[str, SourceModelRecord],
) -> SourceModelRecord | None:
    """Resolve a local model_id to an OpenRouter source record.

    Identity resolution rules (exact / curated only, no fuzzy matching):

    1. ``model_info_aliases`` rows with ``source=openrouter``.  Multiple
       rows for the same alias string are deduplicated; when multiple
       distinct aliases remain and only one of them appears in the
       OpenRouter catalog index, that one wins (Phase 1 polish).
       Exact-case rows always take precedence over case-folded rows.
    2. ``model_info_aliases`` rows with ``source=provider_catalog``
       (or any other source) whose value matches an indexed OpenRouter
       record. This handles the common case where the operator has not
       hand-curated an OpenRouter alias but the provider-catalog
       observation has emitted a ``<provider_id>/<model_id>`` alias
       that happens to match OpenRouter's vendor-prefix naming.
    3. Exact ``source_model_id == model_id`` match (no contradictory
       provider/source context).
    4. Existing pricing aliases may be reused only if they are exact and
       the alias source matches ``openrouter``.
    5. Ambiguous matches (multiple alias candidates after dedupe and
       indexed-alias narrowing) return no match.
    6. No substring or edit-distance matching.
    """
    if not openrouter_indexed:
        return None

    # Rule 1: Check model_info_aliases for an exact openrouter alias.
    # Phase 1 polish: use ``list_alias_rows_for_model`` so we can
    # apply exact-case preference and dedupe identical alias strings
    # before declaring ambiguity.
    or_rows = await repo.list_alias_rows_for_model(model_id, source="openrouter")
    if or_rows:
        candidates = choose_alias_candidates(model_id, or_rows)
        unique_aliases = dedupe_alias_strings(candidates)
        if len(unique_aliases) == 1:
            record = openrouter_indexed.get(unique_aliases[0])
            if record is not None:
                return record
            # The single remaining alias isn't in the indexed catalog;
            # fall through to other rules so direct match / pricing
            # aliases still get a chance.
        else:
            # Multiple distinct aliases remain after exact-case
            # preference + dedupe.  If exactly one of them is in the
            # indexed catalog, prefer that one.  Multiple indexed
            # candidates remain ambiguous.  Zero indexed candidates
            # mean we have aliases pointing nowhere — fall through so
            # direct match / pricing aliases can still resolve.
            indexed_candidates = [a for a in unique_aliases if a in openrouter_indexed]
            if len(indexed_candidates) == 1:
                return openrouter_indexed[indexed_candidates[0]]
            if len(indexed_candidates) > 1:
                logger.debug(
                    "Ambiguous OpenRouter aliases for %s: %s — skipping",
                    model_id,
                    indexed_candidates,
                )
                return None

    # Rule 2: Try aliases from any other source (provider_catalog,
    # huggingface, artificial_analysis).  The provider-catalog source
    # emits a ``<provider_id>/<model_id>`` alias whenever the local
    # provider_id matches OpenRouter's vendor naming (openai, anthropic,
    # google, ...), and the operator's 33-model test fixtures all rely
    # on this path because they do not ship a hand-curated
    # ``[model_info.aliases]`` block.  We still require an exact match
    # against the OpenRouter catalog — no fuzzy matching.
    fallback_rows = await repo.list_alias_rows_for_model(
        model_id, source="provider_catalog"
    )
    if fallback_rows:
        fallback_candidates = choose_alias_candidates(model_id, fallback_rows)
        unique_fallback = dedupe_alias_strings(fallback_candidates)
        if len(unique_fallback) == 1:
            record = openrouter_indexed.get(unique_fallback[0])
            if record is not None:
                return record
        elif len(unique_fallback) > 1:
            # Multiple provider-catalog aliases exist (e.g. when the same
            # base model_id appears under two distinct provider_ids).
            # Only resolve when exactly one of them matches an indexed
            # OpenRouter record; otherwise the match is ambiguous and we
            # skip.
            candidate_records = [
                openrouter_indexed[a]
                for a in unique_fallback
                if a in openrouter_indexed
            ]
            if len(candidate_records) == 1:
                return candidate_records[0]
            if len(candidate_records) > 1:
                logger.debug(
                    "Ambiguous provider_catalog aliases for %s: %s — skipping",
                    model_id,
                    unique_fallback,
                )
                return None

    # Rule 3: Exact source_model_id == model_id
    direct = openrouter_indexed.get(model_id)
    if direct is not None:
        return direct

    # Rule 4: Check pricing aliases (exact match only, source must be openrouter)
    pricing_aliases = await repo.get_aliases_for_model(model_id, source="pricing")
    for alias_str in pricing_aliases:
        record = openrouter_indexed.get(alias_str)
        if record is not None:
            return record

    return None
