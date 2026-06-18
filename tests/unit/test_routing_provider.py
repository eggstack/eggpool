"""Tests for routing/provider.py utilities."""

from __future__ import annotations

from go_aggregator.routing.provider import format_model_provider, parse_model_provider


class TestParseModelProvider:
    def test_with_suffix(self) -> None:
        model_id, provider_id = parse_model_provider("gpt-4/custom-provider")
        assert model_id == "gpt-4"
        assert provider_id == "custom-provider"

    def test_without_suffix(self) -> None:
        model_id, provider_id = parse_model_provider("gpt-4")
        assert model_id == "gpt-4"
        assert provider_id is None

    def test_empty_string(self) -> None:
        model_id, provider_id = parse_model_provider("")
        assert model_id == ""
        assert provider_id is None

    def test_only_slash(self) -> None:
        model_id, provider_id = parse_model_provider("/")
        assert model_id == ""
        assert provider_id == ""

    def test_multiple_slashes(self) -> None:
        model_id, provider_id = parse_model_provider("a/b/c")
        assert model_id == "a/b"
        assert provider_id == "c"


class TestFormatModelProvider:
    def test_basic(self) -> None:
        result = format_model_provider("gpt-4", "custom-provider")
        assert result == "gpt-4/custom-provider"

    def test_round_trip(self) -> None:
        original = "gpt-4/my-provider"
        model_id, provider_id = parse_model_provider(original)
        formatted = format_model_provider(model_id, provider_id or "")
        assert formatted == original
