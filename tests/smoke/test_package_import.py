"""Smoke: package import and basic identity."""

from __future__ import annotations

import importlib


def test_import_eggpool() -> None:
    mod = importlib.import_module("eggpool")
    assert hasattr(mod, "__version__") or mod.__name__ == "eggpool"


def test_import_core_submodules() -> None:
    for name in (
        "eggpool.models.config",
        "eggpool.db.connection",
        "eggpool.db.migrations",
        "eggpool.request.coordinator",
        "eggpool.routing.router",
        "eggpool.cli",
    ):
        importlib.import_module(name)
