"""Shared fixture loaders for model-info tests."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "model_info"


def load_openrouter_fixture() -> dict:
    """Return the OpenRouter fixture as a dict matching the API response shape."""
    with (FIXTURES_DIR / "openrouter_models_sample.json").open() as f:
        return json.load(f)


def load_provider_catalog_fixture() -> dict:
    """Return the provider-catalog fixture."""
    with (FIXTURES_DIR / "provider_catalog_sample_opencode_go.json").open() as f:
        return json.load(f)
