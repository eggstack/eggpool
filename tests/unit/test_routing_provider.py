"""Tests for routing/provider.py utilities."""

from __future__ import annotations

from eggpool.routing.provider import format_model_provider, parse_model_provider


class TestParseModelProvider:
    def test_with_suffix(self) -> None:
        model_id, provider_id = parse_model_provider(
            "gpt-4/custom-provider",
            {"custom-provider"},
        )
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
        assert model_id == "/"
        assert provider_id is None

    def test_multiple_slashes(self) -> None:
        model_id, provider_id = parse_model_provider("a/b/c", {"c"})
        assert model_id == "a/b"
        assert provider_id == "c"

    def test_slash_bearing_model_without_known_provider_suffix(self) -> None:
        model_id, provider_id = parse_model_provider("vendor/model-name", {"custom"})
        assert model_id == "vendor/model-name"
        assert provider_id is None

    def test_trailing_slash(self) -> None:
        model_id, provider_id = parse_model_provider("gpt-4/")
        assert model_id == "gpt-4/"
        assert provider_id is None


class TestFormatModelProvider:
    def test_basic(self) -> None:
        result = format_model_provider("gpt-4", "custom-provider")
        assert result == "gpt-4/custom-provider"

    def test_round_trip(self) -> None:
        original = "gpt-4/my-provider"
        model_id, provider_id = parse_model_provider(original, {"my-provider"})
        formatted = format_model_provider(model_id, provider_id or "")
        assert formatted == original
