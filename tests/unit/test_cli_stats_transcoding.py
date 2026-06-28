"""Tests for the eggpool stats transcoding CLI subcommand."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _mock_stats_data(
    *,
    total: int = 100,
    native_count: int = 80,
    transcoded_count: int = 20,
    per_direction: dict[tuple[str, str], int] | None = None,
) -> dict[str, Any]:
    default_direction = {("openai", "anthropic"): 15, ("anthropic", "openai"): 5}
    return {
        "total": total,
        "native_count": native_count,
        "transcoded_count": transcoded_count,
        "per_direction": (
            per_direction if per_direction is not None else default_direction
        ),
    }


def _invoke_stats_transcoding(
    runner: CliRunner,
    args: list[str],
    stats_data: dict[str, Any],
) -> Any:
    """Invoke ``eggpool stats transcoding`` through the full CLI with mocked DB."""
    from eggpool.cli import cli

    mock_svc = AsyncMock()
    mock_svc.get_transcoding_stats.return_value = stats_data

    with (
        patch(
            "eggpool.deploy_user.resolve_config_path",
            return_value="/tmp/fake-config.toml",
        ),
        patch("eggpool.config.ensure_config"),
        patch("eggpool.cli_full.AppConfig.from_toml") as mock_config,
        patch("eggpool.db.connection.Database") as mock_db_cls,
        patch("eggpool.stats.StatsService", return_value=mock_svc),
    ):
        mock_config.return_value = MagicMock()
        mock_db_cls.return_value = AsyncMock()
        result = runner.invoke(cli, ["stats", "transcoding", *args])

    return result, mock_svc


class TestStatsTranscodingTextOutput:
    def test_empty_database(self, runner: CliRunner) -> None:
        data = _mock_stats_data(
            total=0, native_count=0, transcoded_count=0, per_direction={}
        )
        result, _ = _invoke_stats_transcoding(runner, ["--period", "24h"], data)

        assert result.exit_code == 0, result.output
        assert "Period: 24h" in result.output
        assert "Total requests: 0" in result.output
        assert "Native (no transcoding): 0" in result.output
        assert "Transcoded: 0" in result.output

    def test_with_transcoded_requests(self, runner: CliRunner) -> None:
        data = _mock_stats_data()
        result, _ = _invoke_stats_transcoding(runner, ["--period", "7d"], data)

        assert result.exit_code == 0, result.output
        assert "Period: 7d" in result.output
        assert "Total requests: 100" in result.output
        assert "Native (no transcoding): 80" in result.output
        assert "Transcoded: 20" in result.output
        assert "openai→anthropic" in result.output
        assert "anthropic→openai" in result.output
        assert "Direction" in result.output

    def test_no_direction_when_all_native(self, runner: CliRunner) -> None:
        data = _mock_stats_data(transcoded_count=0, per_direction={})
        result, _ = _invoke_stats_transcoding(runner, [], data)

        assert result.exit_code == 0, result.output
        assert "Direction" not in result.output

    def test_large_numbers_formatted_with_commas(self, runner: CliRunner) -> None:
        data = _mock_stats_data(
            total=1234567, native_count=1200000, transcoded_count=34567
        )
        result, _ = _invoke_stats_transcoding(runner, [], data)

        assert result.exit_code == 0, result.output
        assert "1,234,567" in result.output
        assert "1,200,000" in result.output
        assert "34,567" in result.output


class TestStatsTranscodingJsonOutput:
    def test_json_output(self, runner: CliRunner) -> None:
        data = _mock_stats_data()
        result, _ = _invoke_stats_transcoding(runner, ["--json"], data)

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["total"] == 100
        assert parsed["native_count"] == 80
        assert parsed["transcoded_count"] == 20
        assert parsed["per_direction"]["openai→anthropic"] == 15


class TestStatsTranscodingPeriodPassthrough:
    def test_period_passed_to_service(self, runner: CliRunner) -> None:
        data = _mock_stats_data()
        _, mock_svc = _invoke_stats_transcoding(runner, ["--period", "30d"], data)

        mock_svc.get_transcoding_stats.assert_awaited_once_with("30d")

    def test_default_period(self, runner: CliRunner) -> None:
        data = _mock_stats_data()
        _, mock_svc = _invoke_stats_transcoding(runner, [], data)

        mock_svc.get_transcoding_stats.assert_awaited_once_with("24h")
