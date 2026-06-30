"""Identity resolution for model-info sources.

Maps local Eggpool model IDs to source-specific model IDs using exact
alias matching only.  No fuzzy, substring, or edit-distance matching.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.model_info.repository import ModelInfoRepository
    from eggpool.model_info.types import SourceModelRecord

logger = logging.getLogger(__name__)


async def resolve_openrouter_record(
    model_id: str,
    repo: ModelInfoRepository,
    openrouter_indexed: dict[str, SourceModelRecord],
) -> SourceModelRecord | None:
    """Resolve a local model_id to an OpenRouter source record.

    Identity resolution rules (exact / curated only, no fuzzy matching):

    1. Exact ``model_info_aliases`` row with ``source=openrouter`` wins.
    2. Exact ``source_model_id == model_id`` match (no contradictory
       provider/source context).
    3. Existing pricing aliases may be reused only if they are exact and
       the alias source matches ``openrouter``.
    4. Ambiguous matches (multiple alias candidates) return no match.
    5. No substring or edit-distance matching.
    """
    if not openrouter_indexed:
        return None

    # Rule 1: Check model_info_aliases for an exact openrouter alias
    alias_strings = await repo.get_aliases_for_model(model_id, source="openrouter")
    if len(alias_strings) == 1:
        record = openrouter_indexed.get(alias_strings[0])
        if record is not None:
            return record
    elif len(alias_strings) > 1:
        # Ambiguous — multiple aliases point to different OpenRouter entries.
        # Record diagnostic and return no match.
        logger.debug(
            "Ambiguous OpenRouter aliases for %s: %s — skipping",
            model_id,
            alias_strings,
        )
        return None

    # Rule 2: Exact source_model_id == model_id
    direct = openrouter_indexed.get(model_id)
    if direct is not None:
        return direct

    # Rule 3: Check pricing aliases (exact match only, source must be openrouter)
    pricing_aliases = await repo.get_aliases_for_model(model_id, source="pricing")
    for alias_str in pricing_aliases:
        record = openrouter_indexed.get(alias_str)
        if record is not None:
            return record

    return None
