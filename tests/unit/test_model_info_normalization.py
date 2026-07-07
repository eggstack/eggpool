"""Unit tests for model-info normalization primitives.

Pinned test cases from the model-info identity normalization plan.
Every function in ``src/eggpool/model_info/normalization.py`` is
exercised with both positive and negative cases.
"""

from __future__ import annotations

from eggpool.model_info.normalization import (
    collapse_duplicate_vendor,
    normalize_model_key,
    normalize_vendor_key,
    split_source_id,
    strip_provider_namespace,
    tokenize_model_key,
)


class TestNormalizeModelKey:
    def test_minimax_m3_hyphen(self) -> None:
        assert normalize_model_key("MiniMax-M3") == "minimaxm3"

    def test_minimax_m3_lowercase(self) -> None:
        assert normalize_model_key("minimax-m3") == "minimaxm3"

    def test_minimax_m3_space(self) -> None:
        assert normalize_model_key("MiniMax M3") == "minimaxm3"

    def test_minimax_duplicate_vendor_display_name(self) -> None:
        assert normalize_model_key("MiniMax: MiniMax M3") == "minimaxm3"

    def test_minimax_slash_source_id(self) -> None:
        assert normalize_model_key("minimax/minimax-m3") == "minimaxminimaxm3"

    def test_claude_sonnet_space(self) -> None:
        assert normalize_model_key("Claude Sonnet 4.5") == "claudesonnet45"

    def test_claude_sonnet_hyphen(self) -> None:
        assert normalize_model_key("claude-sonnet-4.5") == "claudesonnet45"

    def test_gpt_underscore(self) -> None:
        assert normalize_model_key("GPT_5.5-mini") == "gpt55mini"

    def test_empty_string(self) -> None:
        assert normalize_model_key("") == ""

    def test_negative_gpt55_vs_gpt55mini(self) -> None:
        assert normalize_model_key("gpt55") != normalize_model_key("gpt55mini")

    def test_negative_v4_vs_v4pro(self) -> None:
        assert normalize_model_key("v4") != normalize_model_key("v4pro")

    def test_negative_deepseekv4_vs_deepseekv4pro(self) -> None:
        assert normalize_model_key("deepseekv4") != normalize_model_key("deepseekv4pro")

    def test_negative_claudesonnet4_vs_claudesonnet45(self) -> None:
        assert normalize_model_key("claudesonnet4") != normalize_model_key(
            "claudesonnet45"
        )


class TestSplitSourceId:
    def test_minimax_vendor_model(self) -> None:
        assert split_source_id("minimax/minimax-m3") == ("minimax", "minimax-m3")

    def test_anthropic_claude(self) -> None:
        assert split_source_id("anthropic/claude-sonnet-4.5") == (
            "anthropic",
            "claude-sonnet-4.5",
        )

    def test_no_slash(self) -> None:
        assert split_source_id("minimax-m3") == (None, "minimax-m3")

    def test_provider_namespace(self) -> None:
        assert split_source_id("opencode-go/minimax-m3") == (
            "opencode-go",
            "minimax-m3",
        )


class TestTokenizeModelKey:
    def test_claude_sonnet(self) -> None:
        assert tokenize_model_key("claude-sonnet-4.5") == (
            "claude",
            "sonnet",
            "4",
            "5",
        )

    def test_gpt_underscore(self) -> None:
        assert tokenize_model_key("GPT_5.5-mini") == ("gpt", "5", "5", "mini")

    def test_minimax_m3(self) -> None:
        assert tokenize_model_key("MiniMax-M3") == ("minimax", "m3")

    def test_slash_delimited(self) -> None:
        assert tokenize_model_key("minimax/minimax-m3") == (
            "minimax",
            "minimax",
            "m3",
        )


class TestStripProviderNamespace:
    def test_known_provider_strips(self) -> None:
        assert (
            strip_provider_namespace("opencode-go/minimax-m3", {"opencode-go"})
            == "minimax-m3"
        )

    def test_unknown_provider_no_strip(self) -> None:
        assert (
            strip_provider_namespace("minimax/minimax-m3", {"opencode-go"})
            == "minimax/minimax-m3"
        )

    def test_no_slash(self) -> None:
        assert strip_provider_namespace("minimax-m3", {"opencode-go"}) == "minimax-m3"

    def test_multiple_providers(self) -> None:
        assert (
            strip_provider_namespace("openai/gpt-4", {"opencode-go", "openai"})
            == "gpt-4"
        )


class TestCollapseDuplicateVendor:
    def test_colon_separated(self) -> None:
        assert collapse_duplicate_vendor("MiniMax: MiniMax M3") == "MiniMax M3"

    def test_colon_separated_lowercase(self) -> None:
        assert collapse_duplicate_vendor("minimax: minimax m3") == "minimax m3"

    def test_dash_separated(self) -> None:
        assert collapse_duplicate_vendor("MiniMax - MiniMax M3") == "MiniMax M3"

    def test_no_duplicate_unchanged(self) -> None:
        assert collapse_duplicate_vendor("minimax-m3") == "minimax-m3"

    def test_no_separator(self) -> None:
        assert collapse_duplicate_vendor("minimaxm3") == "minimaxm3"

    def test_case_insensitive_match(self) -> None:
        assert collapse_duplicate_vendor("Minimax: MINIMAX m3") == "MINIMAX m3"


class TestNormalizeVendorKey:
    def test_none_returns_none(self) -> None:
        assert normalize_vendor_key(None) is None

    def test_empty_returns_none(self) -> None:
        assert normalize_vendor_key("") is None

    def test_vendor_normalized(self) -> None:
        assert normalize_vendor_key("MiniMax") == "minimax"

    def test_vendor_with_whitespace(self) -> None:
        assert normalize_vendor_key("  MiniMax  ") == "minimax"
