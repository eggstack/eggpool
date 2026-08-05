"""Smoke: configuration model parsing and validation."""

from __future__ import annotations

import os

from eggpool.models.config import AppConfig


def test_minimal_config_parses() -> None:
    os.environ.setdefault("SMOKE_TEST_KEY", "smoke-key")
    cfg = AppConfig.from_dict(
        {
            "server": {"api_key_env": "SMOKE_TEST_KEY"},
            "database": {"path": ":memory:"},
            "upstream": {"base_url": "https://smoke.example.com"},
            "accounts": [{"name": "smoke", "api_key_env": "SMOKE_TEST_KEY"}],
        }
    )
    assert cfg.server.host == "127.0.0.1"
    assert cfg.database.path == ":memory:"


def test_config_defaults() -> None:
    os.environ.setdefault("SMOKE_TEST_KEY", "smoke-key")
    cfg = AppConfig.from_dict(
        {
            "server": {"api_key_env": "SMOKE_TEST_KEY"},
            "database": {"path": ":memory:"},
            "upstream": {"base_url": "https://smoke.example.com"},
            "accounts": [{"name": "smoke", "api_key_env": "SMOKE_TEST_KEY"}],
        }
    )
    assert cfg.upstream.connect_timeout_s > 0
    assert cfg.upstream.read_timeout_s > 0
