"""Tests for dashboard CLI configuration persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eggpool.cli import _read_dashboard_public, _write_dashboard_public
from eggpool.models.config import AppConfig

if TYPE_CHECKING:
    from pathlib import Path


def test_write_dashboard_public_creates_missing_section(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[server]\nport = 11300\n", encoding="utf-8")

    _write_dashboard_public(str(config_path), False)

    config = AppConfig.from_toml(str(config_path))
    assert config.dashboard.public is False
    assert _read_dashboard_public(str(config_path)) is False


def test_write_dashboard_public_inserts_missing_key(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[dashboard]\ntheme = "Nord"\n[server]\nport = 11300\n',
        encoding="utf-8",
    )

    _write_dashboard_public(str(config_path), False)

    config = AppConfig.from_toml(str(config_path))
    assert config.dashboard.public is False
    assert config.dashboard.theme == "Nord"
