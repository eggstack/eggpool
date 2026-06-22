"""Tests for configuration loading and validation."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from eggpool.errors import ConfigError
from eggpool.models.config import AppConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def valid_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[server]
host = "127.0.0.1"
port = 9000
api_key_env = "TEST_API_KEY"
log_level = "DEBUG"
access_log = false

[upstream]
base_url = "https://api.example.com"
connect_timeout_s = 3

[database]
path = "test.sqlite3"
wal = true
synchronous = "NORMAL"

[models]
refresh_interval_s = 600
expose_mode = "union"

[routing]
strategy = "quota_fair"

[limits]
five_hour_microdollars = 50000000

[[accounts]]
name = "test_account"
api_key_env = "TEST_KEY_1"
weight = 1.0

[[accounts]]
name = "test_account_2"
api_key_env = "TEST_KEY_2"
weight = 2.0
"""
    )
    return config_file


def test_load_valid_config(valid_config: Path) -> None:
    os.environ["TEST_KEY_1"] = "key1"
    os.environ["TEST_KEY_2"] = "key2"
    try:
        config = AppConfig.from_toml(str(valid_config))
        assert config.server.host == "127.0.0.1"
        assert config.server.port == 9000
        all_accts = config.all_accounts()
        assert len(all_accts) == 2
        assert all_accts[0].name == "test_account"
    finally:
        del os.environ["TEST_KEY_1"]
        del os.environ["TEST_KEY_2"]


def test_missing_required_fields(tmp_path: Path) -> None:
    config_file = tmp_path / "bad.toml"
    config_file.write_text('[server]\nport = "not_a_number"\n')
    with pytest.raises(ConfigError, match="Config validation failed"):
        AppConfig.from_toml(str(config_file))


def test_duplicate_account_names(tmp_path: Path) -> None:
    os.environ["DUP_KEY"] = "key"
    try:
        config_file = tmp_path / "dup.toml"
        config_file.write_text(
            """
[[accounts]]
name = "same_name"
api_key_env = "DUP_KEY"

[[accounts]]
name = "same_name"
api_key_env = "DUP_KEY"
"""
        )
        with pytest.raises(ConfigError, match="Duplicate account name"):
            AppConfig.from_toml(str(config_file))
    finally:
        del os.environ["DUP_KEY"]


def test_missing_env_var_for_enabled_account(tmp_path: Path) -> None:
    config_file = tmp_path / "missing_env.toml"
    config_file.write_text(
        """
[[accounts]]
name = "missing_env"
api_key_env = "NONEXISTENT_ENV_VAR_XYZ"
"""
    )
    config = AppConfig.from_toml(str(config_file))
    with pytest.raises(ConfigError, match="is not set"):
        config.validate_account_credentials()


def test_zero_weight_rejected(tmp_path: Path) -> None:
    os.environ["ZERO_KEY"] = "key"
    try:
        config_file = tmp_path / "zero.toml"
        config_file.write_text(
            """
[[accounts]]
name = "zero_weight"
api_key_env = "ZERO_KEY"
weight = 0
"""
        )
        with pytest.raises(ConfigError):
            AppConfig.from_toml(str(config_file))
    finally:
        del os.environ["ZERO_KEY"]


def test_negative_weight_rejected(tmp_path: Path) -> None:
    os.environ["NEG_KEY"] = "key"
    try:
        config_file = tmp_path / "neg.toml"
        config_file.write_text(
            """
[[accounts]]
name = "neg_weight"
api_key_env = "NEG_KEY"
weight = -1.5
"""
        )
        with pytest.raises(ConfigError):
            AppConfig.from_toml(str(config_file))
    finally:
        del os.environ["NEG_KEY"]


def test_file_not_found() -> None:
    with pytest.raises(ConfigError, match="not found"):
        AppConfig.from_toml("/nonexistent/path/config.toml")


def test_invalid_toml(tmp_path: Path) -> None:
    config_file = tmp_path / "bad_syntax.toml"
    config_file.write_text("[unclosed\n")
    with pytest.raises(ConfigError, match="Invalid TOML"):
        AppConfig.from_toml(str(config_file))


def test_extra_fields_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "extra.toml"
    config_file.write_text("[server]\nunknown_field = true\n")
    with pytest.raises(ConfigError, match="Config validation failed"):
        AppConfig.from_toml(str(config_file))


def test_disabled_account_skips_env_check(tmp_path: Path) -> None:
    config_file = tmp_path / "disabled.toml"
    config_file.write_text(
        """
[[accounts]]
name = "disabled_account"
api_key_env = "TOTALLY_UNSET_ENV_VAR"
enabled = false
"""
    )
    config = AppConfig.from_toml(str(config_file))
    all_accts = config.all_accounts()
    assert all_accts[0].name == "disabled_account"
    assert all_accts[0].enabled is False


def test_bearer_mode_rejects_bearer_prefixed_key() -> None:
    config = AppConfig.from_dict(
        {
            "providers": {
                "minimax": {
                    "id": "minimax",
                    "base_url": "https://api.minimax.io/v1",
                    "protocols": ["openai"],
                    "accounts": [{"name": "default", "api_key": "Bearer sk-test"}],
                }
            }
        }
    )
    with pytest.raises(ConfigError, match="must be the raw token"):
        config.validate_account_credentials()


def test_bearer_mode_rejects_bearer_prefixed_env_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIMAX_KEY", "Bearer sk-test")
    config = AppConfig.from_dict(
        {
            "providers": {
                "minimax": {
                    "id": "minimax",
                    "base_url": "https://api.minimax.io/v1",
                    "protocols": ["openai"],
                    "accounts": [{"name": "default", "api_key_env": "MINIMAX_KEY"}],
                }
            }
        }
    )
    with pytest.raises(ConfigError, match="must be the raw token"):
        config.validate_account_credentials()


def test_bearer_mode_accepts_raw_token() -> None:
    config = AppConfig.from_dict(
        {
            "providers": {
                "minimax": {
                    "id": "minimax",
                    "base_url": "https://api.minimax.io/v1",
                    "protocols": ["openai"],
                    "accounts": [{"name": "default", "api_key": "sk-test"}],
                }
            }
        }
    )
    config.validate_account_credentials()


@pytest.mark.parametrize("api_key", ["sk-test\rvalue", "sk-test\nvalue", "sk\x00test"])
def test_account_credentials_reject_header_control_characters(api_key: str) -> None:
    config = AppConfig.from_dict(
        {
            "providers": {
                "test": {
                    "id": "test",
                    "base_url": "https://api.example.com",
                    "accounts": [{"name": "default", "api_key": api_key}],
                }
            }
        }
    )
    with pytest.raises(ConfigError, match="contains CR, LF, or NUL"):
        config.validate_account_credentials()


def test_raw_authorization_allows_bearer_prefixed_value() -> None:
    """``raw_authorization`` mode passes the key through verbatim so a
    ``Bearer <token>`` value is permitted there.
    """
    config = AppConfig.from_dict(
        {
            "providers": {
                "raw": {
                    "id": "raw",
                    "base_url": "https://api.example.com",
                    "protocols": ["openai"],
                    "auth": {"mode": "raw_authorization"},
                    "accounts": [{"name": "default", "api_key": "Bearer sk-test"}],
                }
            }
        }
    )
    config.validate_account_credentials()


def test_none_auth_accepts_account_without_key() -> None:
    config = AppConfig.from_dict(
        {
            "providers": {
                "local": {
                    "id": "local",
                    "base_url": "http://localhost:11434/v1",
                    "auth": {"mode": "none"},
                    "accounts": [{"name": "local-default"}],
                }
            }
        }
    )

    config.validate_account_credentials()
    assert config.providers["local"].accounts[0].api_key is None
    assert config.providers["local"].accounts[0].api_key_env == ""


def test_authenticated_provider_rejects_account_without_key_source() -> None:
    with pytest.raises(ConfigError, match="must set api_key or api_key_env"):
        AppConfig.from_dict(
            {
                "providers": {
                    "remote": {
                        "id": "remote",
                        "base_url": "https://api.example.com/v1",
                        "accounts": [{"name": "remote-default"}],
                    }
                }
            }
        )


def test_bearer_mode_case_insensitive_prefix_rejected() -> None:
    config = AppConfig.from_dict(
        {
            "providers": {
                "minimax": {
                    "id": "minimax",
                    "base_url": "https://api.minimax.io/v1",
                    "protocols": ["openai"],
                    "accounts": [{"name": "default", "api_key": "  bearer sk-test"}],
                }
            }
        }
    )
    with pytest.raises(ConfigError, match="must be the raw token"):
        config.validate_account_credentials()


def test_model_override_price_strings_are_normalized(tmp_path: Path) -> None:
    config_file = tmp_path / "prices.toml"
    config_file.write_text(
        """
[model_overrides."gpt-4"]
input_price_per_1k = "$3 / 1M"
output_price_per_1k = " $15/1M "
cache_read_per_million_microdollars = "$0.30 / 1M"
cache_write_per_million_microdollars = "750_000"
"""
    )

    config = AppConfig.from_toml(str(config_file))
    override = config.model_overrides["gpt-4"]

    assert override.input_price_per_1k == pytest.approx(0.003)
    assert override.output_price_per_1k == pytest.approx(0.015)
    assert override.cache_read_per_million_microdollars == 300_000
    assert override.cache_write_per_million_microdollars == 750_000


def test_negative_model_override_price_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "bad_price.toml"
    config_file.write_text(
        """
[model_overrides."gpt-4"]
input_price_per_1k = "-$3 / 1M"
"""
    )

    with pytest.raises(ConfigError, match="Config validation failed"):
        AppConfig.from_toml(str(config_file))


def test_config_example_validates() -> None:
    """Verify config.example.toml validates against the schema."""
    config = AppConfig.from_toml("config.example.toml")
    assert config.upstream.base_url == "https://opencode.ai/zen/go/v1"
    assert config.limits.five_hour_microdollars == 12_000_000
    # All accounts are commented out — users add them via `connect`
    assert len(config.all_accounts()) == 0


def test_provider_models_method_is_normalized() -> None:
    config = AppConfig.from_dict(
        {
            "providers": {
                "provider-a": {
                    "id": "provider-a",
                    "base_url": "https://example.com",
                    "models_method": " post ",
                }
            }
        }
    )
    assert config.providers["provider-a"].models_method == "POST"


def test_provider_model_overrides_preserves_pricing_fields() -> None:
    """Regression test (H1): provider-level ``[model_overrides]`` must
    accept the full set of override fields (pricing, protocol), not just
    the limit-only subset.
    """
    config = AppConfig.from_dict(
        {
            "providers": {
                "provider-a": {
                    "id": "provider-a",
                    "base_url": "https://example.com",
                    "model_overrides": {
                        "claude-opus-4": {
                            "input_price_per_1k": "$3 / 1M",
                            "output_price_per_1k": "$15 / 1M",
                            "protocol": "anthropic",
                            "max_context_tokens": 200000,
                        }
                    },
                }
            }
        }
    )
    override = config.providers["provider-a"].model_overrides["claude-opus-4"]
    assert override.input_price_per_1k == pytest.approx(0.003)
    assert override.output_price_per_1k == pytest.approx(0.015)
    assert override.protocol == "anthropic"
    assert override.max_context_tokens == 200000


def test_provider_models_method_rejects_unknown_method() -> None:
    with pytest.raises(ConfigError, match="models_method"):
        AppConfig.from_dict(
            {
                "providers": {
                    "provider-a": {
                        "id": "provider-a",
                        "base_url": "https://example.com",
                        "models_method": "POTS",
                    }
                }
            }
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "api.example.com/v1",
        "ftp://api.example.com/v1",
        "https://user:secret@api.example.com/v1",
        "https://api.example.com/v1?tenant=a",
        "https://api.example.com/v1#models",
        " https://api.example.com/v1",
    ],
)
def test_provider_base_url_rejects_non_dispatchable_values(base_url: str) -> None:
    with pytest.raises(ConfigError, match="base_url"):
        AppConfig.from_dict(
            {
                "providers": {
                    "invalid": {
                        "id": "invalid",
                        "base_url": base_url,
                        "accounts": [{"name": "default", "api_key": "sk-test"}],
                    }
                }
            }
        )


def test_provider_mapping_key_must_match_declared_id() -> None:
    with pytest.raises(ConfigError, match="does not match"):
        AppConfig.from_dict(
            {
                "providers": {
                    "provider-a": {
                        "id": "provider-b",
                        "base_url": "https://example.com",
                    }
                }
            }
        )


def test_provider_id_rejects_trailing_newline() -> None:
    with pytest.raises(ConfigError, match="Provider ID"):
        AppConfig.from_dict(
            {
                "providers": {
                    "provider-a\n": {
                        "id": "provider-a\n",
                        "base_url": "https://example.com",
                    }
                }
            }
        )


def test_dashboard_config_defaults() -> None:
    """DashboardConfig has correct default values."""
    from eggpool.models.config import DashboardConfig

    dc = DashboardConfig()
    assert dc.enabled is True
    assert dc.public is True
    assert dc.retain_request_stats_days == 30
    assert dc.retain_event_days == 90
    assert dc.store_request_content is False
    assert dc.refresh_interval_s == 60


def test_dashboard_store_request_content_rejected_every_form() -> None:
    """Regression test (M13): every truthy form must be rejected
    regardless of Pydantic coercion order.
    """
    import pytest
    from pydantic import ValidationError

    from eggpool.models.config import DashboardConfig

    for truthy in (True, "true", "True", "1", 1, 1.0, "yes"):
        with pytest.raises(ValidationError):
            DashboardConfig(store_request_content=truthy)


def test_server_config_api_key_env_default() -> None:
    """ServerConfig.api_key_env defaults to SERVER_API_KEY."""
    from eggpool.models.config import ServerConfig

    sc = ServerConfig()
    assert sc.api_key_env == "SERVER_API_KEY"


def test_server_config_resolved_api_key_inline() -> None:
    """Inline api_key takes precedence over env var."""
    from eggpool.models.config import ServerConfig

    sc = ServerConfig(api_key="ep_test123")
    assert sc.resolved_api_key == "ep_test123"


def test_server_config_resolved_api_key_env() -> None:
    """Falls back to env var when no inline key."""
    from eggpool.models.config import ServerConfig

    sc = ServerConfig(api_key_env="MY_KEY")
    assert sc.resolved_api_key is None  # env var not set in test


def test_server_config_empty_api_key_env_disables_auth() -> None:
    """Setting api_key_env to empty string disables authentication."""
    from eggpool.models.config import ServerConfig

    sc = ServerConfig(api_key_env="")
    assert sc.api_key_env == ""


def test_provider_config_valid_id() -> None:
    """ProviderConfig accepts valid IDs."""
    from eggpool.models.config import ProviderConfig

    p = ProviderConfig(id="opencode-go", base_url="https://example.com")
    assert p.id == "opencode-go"
    assert p.protocols == ["openai"]
    assert p.connect_timeout_s == 5
    assert p.max_connections == 100
    assert p.accounts == []


@pytest.mark.parametrize("protocols", [[], ["unsupported"]])
def test_provider_config_rejects_invalid_protocols(protocols: list[str]) -> None:
    """Providers need at least one protocol implemented by the proxy."""
    from eggpool.models.config import ProviderConfig

    with pytest.raises(ValueError):
        ProviderConfig(
            id="test-provider",
            base_url="https://example.com",
            protocols=protocols,  # type: ignore[arg-type]
        )


def test_provider_config_invalid_id_rejected() -> None:
    """ProviderConfig rejects invalid IDs."""
    from eggpool.models.config import ProviderConfig

    with pytest.raises(ConfigError, match="alphanumeric"):
        ProviderConfig(id="-invalid-", base_url="https://example.com")
    with pytest.raises(ConfigError, match="alphanumeric"):
        ProviderConfig(id="has spaces", base_url="https://example.com")
    with pytest.raises(ConfigError, match="alphanumeric"):
        ProviderConfig(id="", base_url="https://example.com")


def test_provider_config_keepalive_validation() -> None:
    """ProviderConfig rejects max_keepalive > max_connections."""
    from eggpool.models.config import ProviderConfig

    with pytest.raises(ConfigError, match="max_keepalive"):
        ProviderConfig(
            id="test",
            base_url="https://example.com",
            max_connections=10,
            max_keepalive=20,
        )


def test_provider_config_with_accounts(tmp_path: Path) -> None:
    """ProviderConfig can contain nested accounts."""
    from eggpool.models.config import ProviderConfig

    os.environ["PROV_KEY"] = "key"
    try:
        p = ProviderConfig(
            id="my-provider",
            base_url="https://api.example.com",
            accounts=[
                {
                    "name": "acct1",
                    "api_key_env": "PROV_KEY",
                },
            ],
        )
        assert len(p.accounts) == 1
        assert p.accounts[0].name == "acct1"
    finally:
        del os.environ["PROV_KEY"]


def test_account_named_proxy_resolves_from_proxy_config(tmp_path: Path) -> None:
    """Accounts can reference reusable named proxy configurations."""
    os.environ["PROXY_KEY"] = "key"
    try:
        config_file = tmp_path / "proxy.toml"
        config_file.write_text(
            """
[proxies.local-pproxy]
url = "socks5://127.0.0.1:1080"

[providers.my-provider]
id = "my-provider"
base_url = "https://api.example.com"

[[providers.my-provider.accounts]]
name = "acct1"
api_key_env = "PROXY_KEY"
proxy = "local-pproxy"
"""
        )
        config = AppConfig.from_toml(str(config_file))
        account = config.providers["my-provider"].accounts[0]
        assert config.resolve_account_proxy_url(account) == "socks5://127.0.0.1:1080"
    finally:
        del os.environ["PROXY_KEY"]


def test_account_inline_proxy_url_env_resolves(tmp_path: Path) -> None:
    """Accounts can keep proxy URLs in environment variables."""
    os.environ["PROXY_KEY"] = "key"
    os.environ["ACCOUNT_PROXY_URL"] = "ss://chacha20:secret@proxy.example.com:8388"
    try:
        config_file = tmp_path / "proxy_env.toml"
        config_file.write_text(
            """
[providers.my-provider]
id = "my-provider"
base_url = "https://api.example.com"

[[providers.my-provider.accounts]]
name = "acct1"
api_key_env = "PROXY_KEY"
proxy_url_env = "ACCOUNT_PROXY_URL"
"""
        )
        config = AppConfig.from_toml(str(config_file))
        account = config.providers["my-provider"].accounts[0]
        assert (
            config.resolve_account_proxy_url(account)
            == "ss://chacha20:secret@proxy.example.com:8388"
        )
    finally:
        del os.environ["PROXY_KEY"]
        del os.environ["ACCOUNT_PROXY_URL"]


def test_account_unknown_proxy_rejected(tmp_path: Path) -> None:
    """Account proxy references must point at a configured proxy."""
    config_file = tmp_path / "unknown_proxy.toml"
    config_file.write_text(
        """
[providers.my-provider]
id = "my-provider"
base_url = "https://api.example.com"

[[providers.my-provider.accounts]]
name = "acct1"
api_key_env = "PROXY_KEY"
proxy = "missing"
"""
    )
    with pytest.raises(ConfigError, match="unknown proxy"):
        AppConfig.from_toml(str(config_file))


def test_account_multiple_proxy_sources_rejected(tmp_path: Path) -> None:
    """Account proxy config must not be ambiguous."""
    config_file = tmp_path / "bad_proxy.toml"
    config_file.write_text(
        """
[proxies.local]
url = "http://127.0.0.1:8081"

[providers.my-provider]
id = "my-provider"
base_url = "https://api.example.com"

[[providers.my-provider.accounts]]
name = "acct1"
api_key_env = "PROXY_KEY"
proxy = "local"
proxy_url = "http://127.0.0.1:8082"
"""
    )
    with pytest.raises(ConfigError, match="at most one"):
        AppConfig.from_toml(str(config_file))


def test_flat_config_normalized_to_default_provider(tmp_path: Path) -> None:
    """Flat accounts are normalized into a default provider."""
    os.environ["NORM_KEY_1"] = "key1"
    os.environ["NORM_KEY_2"] = "key2"
    try:
        config_file = tmp_path / "norm.toml"
        config_file.write_text(
            """
[upstream]
base_url = "https://api.upstream.com"

[[accounts]]
name = "alpha"
api_key_env = "NORM_KEY_1"

[[accounts]]
name = "beta"
api_key_env = "NORM_KEY_2"
weight = 2.0
"""
        )
        config = AppConfig.from_toml(str(config_file))
        assert config.accounts == []
        assert "opencode-go" in config.providers
        provider = config.providers["opencode-go"]
        assert provider.base_url == "https://api.upstream.com"
        assert provider.protocols == ["openai", "anthropic"]
        all_accts = config.all_accounts()
        assert len(all_accts) == 2
        assert all_accts[0].name == "alpha"
        assert all_accts[1].name == "beta"
        assert all_accts[1].weight == 2.0
    finally:
        del os.environ["NORM_KEY_1"]
        del os.environ["NORM_KEY_2"]


def test_provider_config_from_toml(tmp_path: Path) -> None:
    """Providers section is parsed from TOML correctly."""
    os.environ["PROV_TOML_KEY"] = "key"
    try:
        config_file = tmp_path / "providers.toml"
        config_file.write_text(
            """
[upstream]
base_url = "https://default.example.com"

[providers.my-provider]
id = "my-provider"
base_url = "https://custom.example.com"
protocols = ["openai", "anthropic"]

[[providers.my-provider.accounts]]
name = "acct_a"
api_key_env = "PROV_TOML_KEY"

[providers.other-provider]
id = "other-provider"
base_url = "https://other.example.com"

[[providers.other-provider.accounts]]
name = "acct_b"
api_key_env = "PROV_TOML_KEY"
"""
        )
        config = AppConfig.from_toml(str(config_file))
        assert len(config.providers) == 2
        assert config.accounts == []
        my_prov = config.providers["my-provider"]
        assert my_prov.base_url == "https://custom.example.com"
        assert len(my_prov.accounts) == 1
        assert my_prov.accounts[0].name == "acct_a"
        other_prov = config.providers["other-provider"]
        assert len(other_prov.accounts) == 1
        assert other_prov.accounts[0].name == "acct_b"
    finally:
        del os.environ["PROV_TOML_KEY"]


def test_duplicate_account_names_across_providers(tmp_path: Path) -> None:
    """Duplicate account names across providers are rejected."""
    os.environ["CROSS_DUP_KEY"] = "key"
    try:
        config_file = tmp_path / "cross_dup.toml"
        config_file.write_text(
            """
[providers.p1]
id = "p1"
base_url = "https://p1.example.com"

[[providers.p1.accounts]]
name = "shared_name"
api_key_env = "CROSS_DUP_KEY"

[providers.p2]
id = "p2"
base_url = "https://p2.example.com"

[[providers.p2.accounts]]
name = "shared_name"
api_key_env = "CROSS_DUP_KEY"
"""
        )
        with pytest.raises(ConfigError, match="Duplicate account name"):
            AppConfig.from_toml(str(config_file))
    finally:
        del os.environ["CROSS_DUP_KEY"]


def test_backward_compatible_flat_config() -> None:
    """Flat config without providers still works via normalization."""
    config = AppConfig(
        upstream={"base_url": "https://upstream.example.com"},
        accounts=[{"name": "a", "api_key_env": "KEY"}],
    )
    assert config.providers["opencode-go"].base_url == "https://upstream.example.com"
    assert config.accounts == []
    assert len(config.all_accounts()) == 1
    assert config.all_accounts()[0].name == "a"


class TestProviderContractConfig:
    """Tests for provider contract config additions."""

    def test_auth_config_defaults(self):
        from eggpool.models.config import ProviderAuthConfig

        cfg = ProviderAuthConfig()
        assert cfg.mode == "bearer"
        assert cfg.header == "Authorization"
        assert cfg.scheme == "Bearer"

    def test_models_endpoint_disabled(self):
        from eggpool.models.config import ProviderModelsEndpointConfig

        ep = ProviderModelsEndpointConfig(method="DISABLED")
        assert ep.method == "DISABLED"
        assert ep.path == "/models"

    def test_verify_config_defaults(self):
        from eggpool.models.config import ProviderVerifyConfig

        v = ProviderVerifyConfig()
        assert v.probe_model is None
        assert v.probe_protocol == "openai"
        assert v.require_models is True

    def test_provider_config_with_all_contract_fields(self):
        from eggpool.models.config import (
            ProviderAuthConfig,
            ProviderConfig,
            ProviderModelsEndpointConfig,
            ProviderStaticHeaderConfig,
            ProviderVerifyConfig,
        )

        cfg = ProviderConfig(
            id="test",
            base_url="https://api.example.com",
            auth=ProviderAuthConfig(mode="api_key", header="X-Key"),
            headers=[ProviderStaticHeaderConfig(name="X-Ref", value="test.com")],
            models_endpoint=ProviderModelsEndpointConfig(
                method="POST", body={"key": "val"}
            ),
            verify=ProviderVerifyConfig(probe_model="gpt-4"),
        )
        assert cfg.auth.mode == "api_key"
        assert len(cfg.headers) == 1
        assert cfg.models_endpoint is not None
        assert cfg.verify.probe_model == "gpt-4"

    def test_old_style_config_backwards_compatible(self):
        """Config without contract fields still loads."""
        from eggpool.models.config import ProviderConfig

        cfg = ProviderConfig(
            id="old",
            base_url="https://api.example.com/v1",
            models_method="GET",
            models_path="/models",
        )
        assert cfg.auth.mode == "bearer"
        assert cfg.models_endpoint is not None
        assert cfg.models_endpoint.method == "GET"

    def test_duplicate_v1_in_openai_path_rejected(self):
        from eggpool.models.config import ProviderConfig

        with pytest.raises(ConfigError, match="duplicate version prefix"):
            ProviderConfig(
                id="bad",
                base_url="https://api.example.com/v1",
                openai_path="/v1/chat/completions",
            )

    def test_duplicate_v1_in_anthropic_path_rejected(self):
        from eggpool.models.config import ProviderConfig

        with pytest.raises(ConfigError, match="duplicate version prefix"):
            ProviderConfig(
                id="bad",
                base_url="https://api.example.com/v1",
                anthropic_path="/v1/messages",
            )


class TestRoutingPriority:
    """Tests for the per-provider ``routing_priority`` field."""

    def test_routing_priority_defaults_to_zero(self):
        from eggpool.models.config import ProviderConfig

        cfg = ProviderConfig(id="p", base_url="https://api.example.com/v1")
        assert cfg.routing_priority == 0

    def test_routing_priority_parses_positive_integer(self):
        from eggpool.models.config import ProviderConfig

        cfg = ProviderConfig(
            id="p",
            base_url="https://api.example.com/v1",
            routing_priority=3,
        )
        assert cfg.routing_priority == 3

    def test_routing_priority_rejects_negative(self):
        from pydantic import ValidationError

        from eggpool.models.config import ProviderConfig

        with pytest.raises(ValidationError):
            ProviderConfig(
                id="p",
                base_url="https://api.example.com/v1",
                routing_priority=-1,
            )

    def test_routing_priority_rejects_non_integer(self):
        from pydantic import ValidationError

        from eggpool.models.config import ProviderConfig

        with pytest.raises(ValidationError):
            ProviderConfig.model_validate(
                {
                    "id": "p",
                    "base_url": "https://api.example.com/v1",
                    "routing_priority": "high",
                }
            )


class TestCollapseModels:
    """Tests for the top-level ``[models].collapse_models`` flag."""

    def test_collapse_models_defaults_to_false(self):
        from eggpool.models.config import ModelsConfig

        cfg = ModelsConfig()
        assert cfg.collapse_models is False

    def test_collapse_models_parses_true(self):
        from eggpool.models.config import ModelsConfig

        cfg = ModelsConfig(collapse_models=True)
        assert cfg.collapse_models is True

    def test_collapse_models_rejects_non_boolean(self):
        from pydantic import ValidationError

        from eggpool.models.config import ModelsConfig

        with pytest.raises(ValidationError):
            ModelsConfig.model_validate({"collapse_models": [1, 2]})
