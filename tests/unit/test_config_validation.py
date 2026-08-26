"""Tests for ``eggpool.config_validation``."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

from eggpool.config_validation import (
    ConfigAccountCredentialError,
    ConfigFileAccessError,
    ConfigInternalError,
    ConfigParseError,
    ConfigSchemaError,
    ConfigStartupAuthError,
    ConfigValidationError,
    ConfigValidationWarning,
    compute_runtime_fingerprint,
    validate_config_file,
)
from eggpool.errors import AggregatorError

SERVER_API_KEY = "ep_test_server_key_1234567890"
ACCOUNT_API_KEY = "sk-test-account-key-1234567890"


def _write_config(tmp_path: Path, body: str) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(body, encoding="utf-8")
    return config_path


def _valid_body() -> str:
    return (
        f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
        "[providers.opencode-go]\n"
        'id = "opencode-go"\n'
        'base_url = "https://opencode.ai/zen/go/v1"\n'
        'protocols = ["openai", "anthropic"]\n'
        "\n[providers.opencode-go.models_endpoint]\n"
        'method = "GET"\n'
        'path = "/models"\n'
        "\n[[providers.opencode-go.accounts]]\n"
        'name = "default"\n'
        f'api_key = "{ACCOUNT_API_KEY}"\n'
        "enabled = true\n"
        "weight = 1.0\n"
    )


class TestValidConfig:
    def test_valid_config_returns_typed_result(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, _valid_body())
        result = validate_config_file(path)
        assert result.config.server.api_key == SERVER_API_KEY
        assert result.config.all_accounts()[0].name == "default"
        assert (
            result.content_digest == hashlib.sha256(Path(path).read_bytes()).hexdigest()
        )
        assert isinstance(result.runtime_fingerprint, str)
        assert len(result.runtime_fingerprint) == 64
        assert result.warnings == ()

    def test_fingerprint_failure_is_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = _write_config(tmp_path, _valid_body())

        def fail(_config: object) -> str:
            raise RuntimeError("fingerprint test failure")

        monkeypatch.setattr(
            "eggpool.config_validation.compute_runtime_fingerprint", fail
        )
        result = validate_config_file(path)

        assert result.runtime_fingerprint == ""
        assert "runtime fingerprint computation failed" in caplog.text

    def test_fingerprint_is_deterministic(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, _valid_body())
        first = validate_config_file(path)
        second = validate_config_file(path)
        assert first.runtime_fingerprint == second.runtime_fingerprint

    def test_body_limit_changes_runtime_fingerprint(self) -> None:
        from eggpool.models.config import AppConfig

        first = AppConfig()
        second = AppConfig()
        second.server.max_request_body_bytes += 1
        assert compute_runtime_fingerprint(first) != compute_runtime_fingerprint(second)

    def test_fingerprint_ignores_blank_account_order(self, tmp_path: Path) -> None:
        body_alpha = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.opencode-go]\n"
            'id = "opencode-go"\n'
            'base_url = "https://opencode.ai/zen/go/v1"\n'
            'protocols = ["openai"]\n'
            "\n[providers.opencode-go.models_endpoint]\n"
            'method = "GET"\npath = "/models"\n'
            "\n[[providers.opencode-go.accounts]]\n"
            'name = "alpha"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "\n[[providers.opencode-go.accounts]]\n"
            'name = "bravo"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
        )
        body_bravo = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.opencode-go]\n"
            'id = "opencode-go"\n'
            'base_url = "https://opencode.ai/zen/go/v1"\n'
            'protocols = ["openai"]\n'
            "\n[providers.opencode-go.models_endpoint]\n"
            'method = "GET"\npath = "/models"\n'
            "\n[[providers.opencode-go.accounts]]\n"
            'name = "bravo"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "\n[[providers.opencode-go.accounts]]\n"
            'name = "alpha"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
        )
        a = validate_config_file(_write_config(tmp_path, body_alpha))
        b = validate_config_file(_write_config(tmp_path, body_bravo))
        assert a.runtime_fingerprint == b.runtime_fingerprint

    def test_fingerprint_redacts_secrets(self, tmp_path: Path) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.opencode-go]\n"
            'id = "opencode-go"\n'
            'base_url = "https://opencode.ai/zen/go/v1"\n'
            'protocols = ["openai"]\n'
            "\n[providers.opencode-go.models_endpoint]\n"
            'method = "GET"\npath = "/models"\n'
            "\n[[providers.opencode-go.accounts]]\n"
            'name = "default"\n'
            f'api_key = "sk-one-real-secret-12345"\n'
        )
        result = validate_config_file(_write_config(tmp_path, body))
        assert "sk-one-real-secret-12345" not in result.runtime_fingerprint

    def test_digest_uses_exact_bytes(self, tmp_path: Path) -> None:
        body = _valid_body()
        path = _write_config(tmp_path, body)
        result = validate_config_file(path)
        assert result.content_digest == hashlib.sha256(body.encode("utf-8")).hexdigest()


class TestValidationFailures:
    def test_optional_dependency_internal_failure_is_typed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eggpool.models.config import AppConfig

        def fail(_config: AppConfig) -> None:
            raise ImportError("optional dependency probe failed")

        monkeypatch.setattr(AppConfig, "validate_optional_dependencies", fail)
        with pytest.raises(ConfigInternalError):
            validate_config_file(_write_config(tmp_path, _valid_body()))

    def test_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.toml"
        with pytest.raises(ConfigFileAccessError) as exc_info:
            validate_config_file(missing)
        assert isinstance(exc_info.value, AggregatorError)
        assert "config file not found" in str(exc_info.value)

    def test_unreadable_file(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, _valid_body())
        path.chmod(0o000)
        try:
            with pytest.raises(ConfigFileAccessError):
                validate_config_file(path)
        finally:
            path.chmod(0o644)

    def test_malformed_toml(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "this = is not valid toml = = =")
        with pytest.raises(ConfigParseError) as exc_info:
            validate_config_file(path)
        assert "invalid TOML" in str(exc_info.value)

    def test_schema_failure(self, tmp_path: Path) -> None:
        body = (
            "[server]\n"
            'host = "127.0.0.1"\n'
            "port = 99999\n"  # out of range
        )
        path = _write_config(tmp_path, body)
        with pytest.raises(ConfigSchemaError) as exc_info:
            validate_config_file(path)
        assert "schema validation failed" in str(exc_info.value)

    def test_startup_auth_failure(self, tmp_path: Path) -> None:
        body = (
            "[server]\n"
            'api_key = "short"\n'  # too short for the 8-512 char rule
        )
        path = _write_config(tmp_path, body)
        with pytest.raises(ConfigStartupAuthError):
            validate_config_file(path)

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20"])
    def test_non_loopback_without_server_key_is_rejected(
        self, tmp_path: Path, host: str
    ) -> None:
        body = f'[server]\nhost = "{host}"\napi_key_env = ""\n'
        with pytest.raises(ConfigStartupAuthError) as exc_info:
            validate_config_file(_write_config(tmp_path, body))
        assert "required when binding" in str(exc_info.value)

    def test_account_credential_failure(self, tmp_path: Path, monkeypatch) -> None:
        from eggpool import constants

        monkeypatch.setattr(
            constants, "PLACEHOLDER_API_KEYS", frozenset({"sk-test-placeholder"})
        )
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.opencode-go]\n"
            'id = "opencode-go"\n'
            'base_url = "https://opencode.ai/zen/go/v1"\n'
            'protocols = ["openai"]\n'
            "\n[providers.opencode-go.models_endpoint]\n"
            'method = "GET"\npath = "/models"\n'
            "\n[[providers.opencode-go.accounts]]\n"
            'name = "default"\n'
            'api_key = "sk-test-placeholder"\n'
        )
        path = _write_config(tmp_path, body)
        with pytest.raises(ConfigAccountCredentialError):
            validate_config_file(path)

    def test_warning_only_config_succeeds(self, tmp_path: Path) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.bad]\n"
            'id = "bad"\n'
            'base_url = "https://api.example.com"\n'
            'protocols = ["openai"]\n'
            'anthropic_path = "/v1/messages"\n'
            "\n[[providers.bad.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
        path = _write_config(tmp_path, body)
        result = validate_config_file(path)
        assert any(
            "anthropic" in warning.message.lower() for warning in result.warnings
        )

    def test_validation_helper_does_not_raise_systemexit(self, tmp_path: Path) -> None:
        """``validate_config_file`` is server-callable; never ``SystemExit``."""
        path = _write_config(tmp_path, "not = valid = toml =")
        with pytest.raises(Exception) as exc_info:
            validate_config_file(path)
        assert not isinstance(exc_info.value, SystemExit)
        assert isinstance(exc_info.value, ConfigValidationError)


class TestConfigValidationResultStructure:
    def test_frozen(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, _valid_body())
        result = validate_config_file(path)
        with pytest.raises((AttributeError, TypeError)):
            result.content_digest = "tampered"  # type: ignore[misc]

    def test_warning_is_dataclass(self) -> None:
        warning = ConfigValidationWarning(
            code="test.code", message="message", section="section"
        )
        assert warning.to_display() == "[section] message"
        assert warning.to_display().startswith("[section]")

    def test_warning_no_section(self) -> None:
        warning = ConfigValidationWarning(code="x", message="hello")
        assert warning.to_display() == "hello"


def test_config_validation_error_subclasses_inherit_aggregator() -> None:
    for cls in (
        ConfigFileAccessError,
        ConfigParseError,
        ConfigSchemaError,
        ConfigStartupAuthError,
        ConfigAccountCredentialError,
    ):
        assert issubclass(cls, ConfigValidationError)
        assert issubclass(cls, AggregatorError)


def test_validate_config_file_signature_is_click_free() -> None:
    """The validation helper must never import Click or raise SystemExit."""
    source = inspect.getsource(validate_config_file)
    assert "click" not in source
    assert "sys.exit" not in source
    code_body = source.split('"""', 2)[-1] if '"""' in source else source
    assert "SystemExit" not in code_body


def test_validation_warning_payload_is_logger_safe() -> None:
    """Warning messages must never embed credential values."""
    warning = ConfigValidationWarning(
        code="anthropic_path_unused",
        message=(
            "anthropic_path is set but 'anthropic' is not in protocols; the "
            "field will be ignored"
        ),
        section="providers.minimax",
    )
    rendered = warning.to_display()
    assert "sk-" not in rendered
    assert "Bearer " not in rendered


class TestRuntimeFingerprintSecrets:
    def test_strips_account_api_keys(self) -> None:
        """The fingerprint treats every per-account ``api_key`` field as secret."""
        from eggpool.models.config import AppConfig

        config_alpha = AppConfig.from_dict(
            {
                "server": {"api_key": SERVER_API_KEY},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": "sk-first-secret-key-001",
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        config_beta = AppConfig.from_dict(
            {
                "server": {"api_key": SERVER_API_KEY},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": "sk-second-secret-key-002",
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        assert compute_runtime_fingerprint(config_alpha) == compute_runtime_fingerprint(
            config_beta
        )
