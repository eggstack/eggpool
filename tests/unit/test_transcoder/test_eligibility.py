"""Tests for account_supports_protocol_any."""

from __future__ import annotations

from eggpool.accounts.registry import AccountRegistry
from eggpool.models.config import AppConfig


def _build_registry() -> AccountRegistry:
    """Build a three-account fixture with mixed provider.protocols.

    - acct_openai  → provider "openai-only"  → protocols: ["openai"]
    - acct_anthropic → provider "anthropic-only" → protocols: ["anthropic"]
    - acct_both    → provider "both-proto"  → protocols: ["openai", "anthropic"]
    """
    config = AppConfig.model_validate(
        {
            "providers": {
                "openai-only": {
                    "id": "openai-only",
                    "base_url": "https://api.openai.example/v1",
                    "protocols": ["openai"],
                    "accounts": [{"name": "acct_openai", "api_key": "sk-test"}],
                },
                "anthropic-only": {
                    "id": "anthropic-only",
                    "base_url": "https://api.anthropic.example/v1",
                    "protocols": ["anthropic"],
                    "accounts": [{"name": "acct_anthropic", "api_key": "sk-test"}],
                },
                "both-proto": {
                    "id": "both-proto",
                    "base_url": "https://api.both.example/v1",
                    "protocols": ["openai", "anthropic"],
                    "accounts": [{"name": "acct_both", "api_key": "sk-test"}],
                },
            }
        }
    )
    return AccountRegistry(config)


class TestAccountSupportsProtocolAny:
    """Tests for AccountRegistry.account_supports_protocol_any."""

    def test_single_protocol_match(self) -> None:
        registry = _build_registry()
        assert registry.account_supports_protocol_any("acct_openai", ["openai"])

    def test_single_protocol_no_match(self) -> None:
        registry = _build_registry()
        assert not registry.account_supports_protocol_any("acct_openai", ["anthropic"])

    def test_multiple_protocols_one_matches(self) -> None:
        registry = _build_registry()
        assert registry.account_supports_protocol_any(
            "acct_openai", ["anthropic", "openai"]
        )

    def test_multiple_protocols_none_match(self) -> None:
        registry = _build_registry()
        assert not registry.account_supports_protocol_any(
            "acct_openai", ["anthropic", "google"]
        )

    def test_dual_protocol_provider_matches_either(self) -> None:
        registry = _build_registry()
        assert registry.account_supports_protocol_any("acct_both", ["openai"])
        assert registry.account_supports_protocol_any("acct_both", ["anthropic"])

    def test_dual_protocol_provider_matches_set(self) -> None:
        registry = _build_registry()
        assert registry.account_supports_protocol_any(
            "acct_both", ["openai", "anthropic"]
        )

    def test_anthropic_only_provider_rejects_openai(self) -> None:
        registry = _build_registry()
        assert not registry.account_supports_protocol_any("acct_anthropic", ["openai"])

    def test_anthropic_only_provider_matches_anthropic(self) -> None:
        registry = _build_registry()
        assert registry.account_supports_protocol_any("acct_anthropic", ["anthropic"])

    def test_unknown_account_returns_false(self) -> None:
        registry = _build_registry()
        assert not registry.account_supports_protocol_any(
            "acct_nonexistent", ["openai"]
        )

    def test_empty_protocols_returns_false(self) -> None:
        registry = _build_registry()
        assert not registry.account_supports_protocol_any("acct_openai", [])
