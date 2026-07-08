"""Safety tests for tiered identity matching.

Verifies that close model variants do NOT incorrectly bind to each
other when similarity matching is disabled (the default).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eggpool.model_info.matching import (
    build_candidate_index,
    resolve_source_record_tiered,
)
from eggpool.model_info.types import SourceModelRecord

_NOW = datetime.now(UTC)


def _record(
    source_model_id: str,
    *,
    display_name: str | None = None,
    source: str = "openrouter",
) -> SourceModelRecord:
    return SourceModelRecord(
        source=source,
        source_model_id=source_model_id,
        observed_at=_NOW,
        raw_hash=source_model_id,
        raw_payload={},
        normalized={},
        display_name=display_name or source_model_id,
        confidence=0.9,
    )


class FakeRepo:
    """Minimal async repo stub."""

    async def list_alias_rows_for_model(
        self, model_id: str, *, source: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    async def get_aliases_for_model(
        self, model_id: str, *, source: str | None = None
    ) -> list[str]:
        return []


class TestGPT55DoesNotBindToMini:
    @pytest.mark.asyncio()
    async def test_no_match(self) -> None:
        record = _record("openai/gpt-5.5-mini")
        index = build_candidate_index("openrouter", [record])
        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="gpt-5.5",
            provider_id=None,
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is False


class TestDeepSeekV4DoesNotBindToPro:
    @pytest.mark.asyncio()
    async def test_no_match(self) -> None:
        record = _record("deepseek/deepseek-v4-pro")
        index = build_candidate_index("openrouter", [record])
        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="deepseek-v4",
            provider_id=None,
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is False


class TestClaudeSonnet4DoesNotBindTo45:
    @pytest.mark.asyncio()
    async def test_no_match(self) -> None:
        record = _record("anthropic/claude-sonnet-4.5")
        index = build_candidate_index("openrouter", [record])
        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="claude-sonnet-4",
            provider_id=None,
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is False


class TestGemini25FlashDoesNotBindToPro:
    @pytest.mark.asyncio()
    async def test_no_match(self) -> None:
        record = _record("google/gemini-2.5-pro")
        index = build_candidate_index("openrouter", [record])
        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="gemini-2.5-flash",
            provider_id=None,
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is False


# ---------------------------------------------------------------------------
# Deployment-suffix safety: semantic variants must not be auto-stripped
# ---------------------------------------------------------------------------


class TestHighspeedDoesNotBindToPro:
    @pytest.mark.asyncio()
    async def test_no_match(self) -> None:
        """Even with deployment-suffix tier enabled, `*-pro` is a
        semantic variant and must not collapse to the base."""
        record = _record("minimax/minimax-m2.7")
        index = build_candidate_index("openrouter", [record])
        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="MiniMax-M2.7-pro",
            provider_id=None,
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is False


class TestHighspeedDoesNotBindToMini:
    @pytest.mark.asyncio()
    async def test_no_match(self) -> None:
        record = _record("openai/gpt-5-mini")
        index = build_candidate_index("openrouter", [record])
        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id="gpt-5-mini-turbo",
            provider_id=None,
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is False


class TestSemanticTokenNotTreatedAsDeploymentSuffix:
    """Ensure the deployment-suffix tier does not silently strip
    semantic variants like ``preview`` or ``code``."""

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        "variant_id",
        [
            "kimi-k2.7-code",
            "hy3-preview",
            "MiniMax-M2.7-thinking",
            "mimo-v2.5-pro",
        ],
    )
    async def test_no_strip(self, variant_id: str) -> None:
        record = _record("minimax/minimax-m2.7")
        index = build_candidate_index("openrouter", [record])
        repo = FakeRepo()

        decision = await resolve_source_record_tiered(
            source="openrouter",
            model_id=variant_id,
            provider_id=None,
            display_name=None,
            repo=repo,
            candidate_index=index,
        )
        assert decision.matched is False
