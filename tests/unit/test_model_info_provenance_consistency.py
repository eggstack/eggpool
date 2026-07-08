"""Tests for provenance/source consistency when external IDs are
preserved from prior cycles.

Pins the contract: ``detail.external_ids`` and ``provenance.sources``
must not disagree silently.  When ``existing_detail`` carries an
external_id for a source that did NOT contribute this cycle, that
source remains in ``provenance.sources`` with ``source_states[X] =
"preserved_external_id"``.
"""

from __future__ import annotations

from eggpool.model_info.service import build_canonical_detail


def _provider_detail() -> dict[str, object]:
    return {
        "display_name": "MiniMax-M3",
        "protocol": "openai",
        "capabilities": {"supports_tools": True},
        "limits": {
            "effective_context": 128000,
            "effective_input": 128000,
            "effective_output": 16384,
        },
    }


def _existing_with_openrouter_id() -> dict[str, object]:
    return {
        "external_ids": {
            "openrouter": "minimax/minimax-m3",
        },
        "pricing": {"openrouter": {"prompt": 0.0000002}},
    }


def test_preserved_external_id_is_credited_in_sources() -> None:
    detail, provenance, _conflicts = build_canonical_detail(
        model_id="minimax-m3",
        provider_detail=_provider_detail(),
        observation_payloads=[],  # no fresh observations this cycle
        existing_detail=_existing_with_openrouter_id(),
    )

    external_ids = detail.get("external_ids", {})
    assert external_ids.get("openrouter") == "minimax/minimax-m3"

    sources = provenance.get("sources", [])
    assert "provider_catalog" in sources
    assert "openrouter" in sources, (
        f"provenance.sources must credit openrouter because "
        f"existing_detail.external_ids.openrouter is preserved; "
        f"got sources={sources!r}"
    )
    assert provenance.get("source_states", {}).get("openrouter") == (
        "preserved_external_id"
    )


def test_no_existing_external_id_does_not_credit() -> None:
    detail, provenance, _conflicts = build_canonical_detail(
        model_id="minimax-m3",
        provider_detail=_provider_detail(),
        observation_payloads=[],
        existing_detail=None,
    )
    sources = provenance.get("sources", [])
    assert sources == ["provider_catalog"]
    assert provenance.get("source_states", {}) == {}
    assert detail.get("external_ids") in ({}, None)


def test_fresh_observation_credits_source_in_provenance() -> None:
    """Sanity: when a source DOES contribute this cycle, it appears in
    provenance with no special source_state (because used_sources
    already credits it via the standard path)."""

    detail, provenance, _conflicts = build_canonical_detail(
        model_id="claude-sonnet-4.5",
        provider_detail={
            "display_name": "Claude Sonnet 4.5",
            "protocol": "anthropic",
            "capabilities": {"supports_tools": True},
            "limits": {
                "effective_context": 200000,
                "effective_input": 200000,
                "effective_output": 8192,
            },
        },
        observation_payloads=[
            {
                "source": "openrouter",
                "source_model_id": "anthropic/claude-sonnet-4.5",
                "normalized": {"context_window": 200000, "modalities": ["text"]},
            }
        ],
        existing_detail=None,
    )
    assert "openrouter" in provenance["sources"]
    # source_states is only set when something is preserved; here nothing
    # is preserved.  Either no key, or empty.
    source_states = provenance.get("source_states", {})
    assert source_states == {} or "openrouter" not in source_states


def test_already_credited_source_is_not_duplicated() -> None:
    """When a source contributes this cycle AND is also preserved, no
    duplicate source entry appears."""

    detail, provenance, _conflicts = build_canonical_detail(
        model_id="claude-sonnet-4.5",
        provider_detail={
            "display_name": "Claude Sonnet 4.5",
            "protocol": "anthropic",
            "limits": {"effective_context": 200000},
            "capabilities": {},
        },
        observation_payloads=[
            {
                "source": "openrouter",
                "source_model_id": "anthropic/claude-sonnet-4.5",
                "normalized": {"context_window": 200000},
            }
        ],
        existing_detail={"external_ids": {"openrouter": "anthropic/claude-sonnet-4.5"}},
    )
    sources = provenance["sources"]
    assert sources.count("openrouter") == 1
    assert sources.count("provider_catalog") == 1
