"""Tests for provider contract rendering and config validation."""

from __future__ import annotations

import pytest

from eggpool.errors import ConfigError
from eggpool.models.config import (
    ProviderAuthConfig,
    ProviderConfig,
    ProviderModelsEndpointConfig,
    ProviderStaticHeaderConfig,
)
from eggpool.providers.contract import (
    build_auth_headers,
    build_static_headers,
    build_upstream_headers,
    compose_provider_url,
)


class TestComposeProviderUrl:
    def test_simple_base_and_path(self):
        cfg = ProviderConfig(id="t", base_url="https://api.example.com/v1")
        assert compose_provider_url(cfg, "/chat/completions") == (
            "https://api.example.com/v1/chat/completions"
        )

    def test_strips_trailing_slash_from_base(self):
        cfg = ProviderConfig(id="t", base_url="https://api.example.com/v1/")
        assert compose_provider_url(cfg, "/chat/completions") == (
            "https://api.example.com/v1/chat/completions"
        )

    def test_strips_leading_slash_from_path(self):
        cfg = ProviderConfig(id="t", base_url="https://api.example.com/v1")
        assert compose_provider_url(cfg, "chat/completions") == (
            "https://api.example.com/v1/chat/completions"
        )

    def test_host_only_base_with_versioned_path(self):
        cfg = ProviderConfig(id="t", base_url="https://api.minimaxi.com")
        assert compose_provider_url(cfg, "/v1/chat/completions") == (
            "https://api.minimaxi.com/v1/chat/completions"
        )

    def test_generalcompute_single_v1(self):
        cfg = ProviderConfig(
            id="generalcompute",
            base_url="https://api.generalcompute.com/v1",
            models_path="/models/list",
        )
        assert compose_provider_url(cfg, cfg.models_path) == (
            "https://api.generalcompute.com/v1/models/list"
        )

    def test_ollama_local(self):
        cfg = ProviderConfig(id="t", base_url="http://localhost:11434/v1")
        assert compose_provider_url(cfg, "/models") == (
            "http://localhost:11434/v1/models"
        )


class TestBuildAuthHeaders:
    def test_bearer_mode(self):
        cfg = ProviderConfig(id="t", base_url="https://api.example.com")
        assert build_auth_headers(cfg, "sk-test") == {"Authorization": "Bearer sk-test"}

    def test_bearer_custom_scheme(self):
        cfg = ProviderConfig(
            id="t",
            base_url="https://api.example.com",
            auth=ProviderAuthConfig(mode="bearer", scheme="Token"),
        )
        assert build_auth_headers(cfg, "sk-test") == {"Authorization": "Token sk-test"}

    def test_api_key_mode(self):
        cfg = ProviderConfig(
            id="t",
            base_url="https://api.example.com",
            auth=ProviderAuthConfig(mode="api_key", header="X-Api-Key"),
        )
        assert build_auth_headers(cfg, "secret") == {"X-Api-Key": "secret"}

    def test_raw_authorization_mode(self):
        cfg = ProviderConfig(
            id="t",
            base_url="https://api.example.com",
            auth=ProviderAuthConfig(mode="raw_authorization", header="X-Auth"),
        )
        assert build_auth_headers(cfg, "token123") == {"X-Auth": "token123"}

    def test_none_mode(self):
        cfg = ProviderConfig(
            id="t",
            base_url="http://localhost:11434/v1",
            auth=ProviderAuthConfig(mode="none"),
        )
        assert build_auth_headers(cfg, "anything") == {}


class TestBuildStaticHeaders:
    def test_inline_value(self):
        cfg = ProviderConfig(
            id="t",
            base_url="https://api.example.com",
            headers=[
                ProviderStaticHeaderConfig(name="X-Referer", value="https://test.com")
            ],
        )
        assert build_static_headers(cfg) == {"X-Referer": "https://test.com"}

    def test_empty_headers(self):
        cfg = ProviderConfig(id="t", base_url="https://api.example.com")
        assert build_static_headers(cfg) == {}

    def test_env_var_header(self, monkeypatch):
        monkeypatch.setenv("MY_HEADER_VAL", "env-value")
        cfg = ProviderConfig(
            id="t",
            base_url="https://api.example.com",
            headers=[
                ProviderStaticHeaderConfig(name="X-Custom", value_env="MY_HEADER_VAL")
            ],
        )
        assert build_static_headers(cfg) == {"X-Custom": "env-value"}

    def test_missing_env_var_omitted(self, monkeypatch):
        monkeypatch.delenv("MISSING_HEADER", raising=False)
        cfg = ProviderConfig(
            id="t",
            base_url="https://api.example.com",
            headers=[
                ProviderStaticHeaderConfig(name="X-Custom", value_env="MISSING_HEADER")
            ],
        )
        assert build_static_headers(cfg) == {}


class TestBuildUpstreamHeaders:
    def test_auth_plus_static(self):
        cfg = ProviderConfig(
            id="t",
            base_url="https://api.example.com",
            auth=ProviderAuthConfig(mode="bearer"),
            headers=[ProviderStaticHeaderConfig(name="X-Referer", value="test")],
        )
        headers = build_upstream_headers(cfg, "sk-test")
        assert headers == {
            "Authorization": "Bearer sk-test",
            "X-Referer": "test",
        }

    def test_none_auth_plus_static(self):
        cfg = ProviderConfig(
            id="t",
            base_url="http://localhost:11434/v1",
            auth=ProviderAuthConfig(mode="none"),
            headers=[ProviderStaticHeaderConfig(name="X-Custom", value="val")],
        )
        headers = build_upstream_headers(cfg, "")
        assert headers == {"X-Custom": "val"}
        assert "Authorization" not in headers


class TestProviderConfigContract:
    def test_old_style_config_synthesizes_models_endpoint(self):
        cfg = ProviderConfig(
            id="test",
            base_url="https://api.example.com/v1",
            models_method="POST",
            models_path="/models/list",
        )
        assert cfg.models_endpoint is not None
        assert cfg.models_endpoint.method == "POST"
        assert cfg.models_endpoint.path == "/models/list"

    def test_new_style_models_endpoint_preserved(self):
        ep = ProviderModelsEndpointConfig(
            method="POST",
            path="/models/list",
            body={"key": "value"},
            query={"limit": "100"},
        )
        cfg = ProviderConfig(
            id="test",
            base_url="https://api.example.com/v1",
            models_endpoint=ep,
        )
        assert cfg.models_endpoint is not None
        assert cfg.models_endpoint.body == {"key": "value"}
        assert cfg.models_endpoint.query == {"limit": "100"}

    def test_auth_config_defaults(self):
        cfg = ProviderConfig(id="t", base_url="https://api.example.com")
        assert cfg.auth.mode == "bearer"
        assert cfg.auth.header == "Authorization"
        assert cfg.auth.scheme == "Bearer"

    def test_verify_config_defaults(self):
        cfg = ProviderConfig(id="t", base_url="https://api.example.com")
        assert cfg.verify.probe_model is None
        assert cfg.verify.probe_protocol == "openai"
        assert cfg.verify.require_models is True


class TestDuplicateVersionValidation:
    def test_duplicate_v1_rejected(self):
        with pytest.raises(ConfigError, match="duplicate version prefix"):
            ProviderConfig(
                id="bad",
                base_url="https://api.example.com/v1",
                openai_path="/v1/chat/completions",
            )

    def test_duplicate_api_v1_rejected(self):
        with pytest.raises(ConfigError, match="duplicate version prefix"):
            ProviderConfig(
                id="bad",
                base_url="https://api.example.com/api/v1",
                openai_path="/api/v1/chat/completions",
            )

    def test_minimax_host_level_v1_path_ok(self):
        cfg = ProviderConfig(
            id="minimax",
            base_url="https://api.minimaxi.com",
            openai_path="/v1/chat/completions",
            anthropic_path="/anthropic/v1/messages",
            models_path="/v1/models",
        )
        assert cfg.openai_path == "/v1/chat/completions"

    def test_duplicate_v1_in_models_path_rejected(self):
        with pytest.raises(ConfigError, match="duplicate version prefix"):
            ProviderConfig(
                id="bad",
                base_url="https://api.example.com/v1",
                models_path="/v1/models",
            )

    def test_compatible_mode_v1_ok_when_no_duplicate(self):
        cfg = ProviderConfig(
            id="alibaba",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            openai_path="/chat/completions",
            models_path="/models",
        )
        assert cfg.base_url.endswith("/compatible-mode/v1")


class TestProviderStaticHeaderConfig:
    def test_both_value_and_value_env_rejected(self):
        with pytest.raises(ConfigError):
            ProviderStaticHeaderConfig(name="X-Test", value="a", value_env="B")

    def test_neither_value_nor_value_env_ok(self):
        h = ProviderStaticHeaderConfig(name="X-Test")
        assert h.value is None
        assert h.value_env is None


class TestProviderModelsEndpointConfig:
    def test_disabled_endpoint(self):
        ep = ProviderModelsEndpointConfig(method="DISABLED")
        assert ep.method == "DISABLED"

    def test_post_with_body(self):
        ep = ProviderModelsEndpointConfig(
            method="POST",
            body={"key": "val"},
            query={"q": "1"},
        )
        assert ep.body == {"key": "val"}
        assert ep.query == {"q": "1"}
