"""Tests for the update CLI command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from eggpool.cli import cli


class TestUpdateCommand:
    def test_update_check_only_with_update_available(self) -> None:
        """update --check reports update available without installing."""
        runner = CliRunner()
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"info": {"version": "0.2.0"}}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            with patch("importlib.metadata.version", return_value="0.1.0"):
                result = runner.invoke(cli, ["update", "--check"])

            assert result.exit_code == 0
            assert "Current version: 0.1.0" in result.output
            assert "Latest version:  0.2.0" in result.output
            assert "An update is available." in result.output

    def test_update_check_only_up_to_date(self) -> None:
        """update --check reports already up to date."""
        runner = CliRunner()
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"info": {"version": "0.1.0"}}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            with patch("importlib.metadata.version", return_value="0.1.0"):
                result = runner.invoke(cli, ["update", "--check"])

            assert result.exit_code == 0
            assert "Already up to date." in result.output

    def test_update_from_source_flag(self) -> None:
        """update --from-source uses git install."""
        runner = CliRunner()
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"info": {"version": "0.2.0"}}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            with (
                patch("importlib.metadata.version", return_value="0.1.0"),
                patch("subprocess.run") as mock_run,
            ):
                mock_run.return_value = MagicMock(returncode=0)
                result = runner.invoke(cli, ["update", "--from-source"])

                assert result.exit_code == 0
                assert "Updating from 0.1.0 to 0.2.0" in result.output
                # Verify git install command was used
                call_args = mock_run.call_args[0][0]
                assert "git+https://github.com/eggstack/eggpool.git@v0.2.0" in " ".join(
                    call_args
                )
