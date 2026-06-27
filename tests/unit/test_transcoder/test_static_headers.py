"""Tests for PROTOCOL_REQUIRED_STATIC_HEADERS injection in build_upstream_headers."""

from __future__ import annotations

from eggpool.models.config import ProviderConfig
from eggpool.providers.contract import build_upstream_headers
from eggpool.transcoder.static_headers import PROTOCOL_REQUIRED_STATIC_HEADERS


class TestProtocolRequiredStaticHeaders:
    """Verify build_upstream_headers injects protocol-required defaults."""

    def test_anthropic_version_injected_when_absent(self) -> None:
        cfg = ProviderConfig(id="t", base_url="https://api.example.com")
        headers = build_upstream_headers(cfg, "sk-test", protocol="anthropic")
        assert headers["anthropic-version"] == "2023-06-01"

    def test_anthropic_version_not_injected_when_protocol_none(self) -> None:
        cfg = ProviderConfig(id="t", base_url="https://api.example.com")
        headers = build_upstream_headers(cfg, "sk-test", protocol=None)
        assert "anthropic-version" not in headers

    def test_anthropic_version_not_injected_when_protocol_openai(self) -> None:
        cfg = ProviderConfig(id="t", base_url="https://api.example.com")
        headers = build_upstream_headers(cfg, "sk-test", protocol="openai")
        assert "anthropic-version" not in headers

    def test_operator_declared_header_wins_over_default(self) -> None:
        from eggpool.models.config import ProviderStaticHeaderConfig

        cfg = ProviderConfig(
            id="t",
            base_url="https://api.example.com",
            headers=[
                ProviderStaticHeaderConfig(name="anthropic-version", value="2024-01-01")
            ],
        )
        headers = build_upstream_headers(cfg, "sk-test", protocol="anthropic")
        assert headers["anthropic-version"] == "2024-01-01"

    def test_operator_case_variant_wins_over_default(self) -> None:
        from eggpool.models.config import ProviderStaticHeaderConfig

        cfg = ProviderConfig(
            id="t",
            base_url="https://api.example.com",
            headers=[
                ProviderStaticHeaderConfig(
                    name="Anthropic-Version", value="custom-value"
                )
            ],
        )
        headers = build_upstream_headers(cfg, "sk-test", protocol="anthropic")
        assert headers["Anthropic-Version"] == "custom-value"

    def test_injected_header_does_not_shadow_auth(self) -> None:
        cfg = ProviderConfig(id="t", base_url="https://api.example.com")
        headers = build_upstream_headers(cfg, "sk-test", protocol="anthropic")
        assert headers["Authorization"] == "Bearer sk-test"

    def test_no_headers_when_no_protocol_and_no_operator_headers(self) -> None:
        cfg = ProviderConfig(id="t", base_url="https://api.example.com")
        headers = build_upstream_headers(cfg, "sk-test")
        assert headers == {"Authorization": "Bearer sk-test"}

    def test_static_table_has_anthropic_entry(self) -> None:
        assert "anthropic" in PROTOCOL_REQUIRED_STATIC_HEADERS
        assert "anthropic-version" in PROTOCOL_REQUIRED_STATIC_HEADERS["anthropic"]
