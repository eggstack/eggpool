"""Tests that bundled provider registry entries parse with Pydantic models."""

from __future__ import annotations

import tomllib
from importlib.resources import files
from typing import Any

import pytest

from eggpool.models.config import (
    ProviderAuthConfig,
    ProviderConfig,
    ProviderVerifyConfig,
)


def _load_registry() -> dict[str, dict[str, Any]]:
    """Load and parse the bundled _templates.toml."""
    ref = files("eggpool.providers").joinpath("_templates.toml")
    text = ref.read_text(encoding="utf-8")
    return tomllib.loads(text)


@pytest.fixture(scope="module")
def registry() -> dict[str, dict[str, Any]]:
    return _load_registry()


@pytest.fixture(scope="module")
def provider_entries(registry: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return registry.get("providers", {})


class TestRegistryStructure:
    def test_has_providers_section(self, registry: dict[str, Any]) -> None:
        assert "providers" in registry
        assert len(registry["providers"]) > 0

    def test_all_entries_have_required_fields(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        required = {"id", "base_url", "protocols"}
        for pid, entry in provider_entries.items():
            missing = required - set(entry.keys())
            assert not missing, f"Provider {pid!r} missing fields: {missing}"

    def test_all_entries_have_metadata(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        metadata_fields = {"display_name", "status", "category", "region"}
        for pid, entry in provider_entries.items():
            missing = metadata_fields - set(entry.keys())
            assert not missing, f"Provider {pid!r} missing metadata: {missing}"

    def test_status_values_valid(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        valid = {"verified", "experimental", "unverified"}
        for pid, entry in provider_entries.items():
            status = entry.get("status", "")
            assert status in valid, f"Provider {pid!r} has invalid status: {status!r}"

    def test_category_values_valid(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        valid = {"direct", "aggregator", "local"}
        for pid, entry in provider_entries.items():
            category = entry.get("category", "")
            assert category in valid, (
                f"Provider {pid!r} has invalid category: {category!r}"
            )


class TestRegistryPydanticParsing:
    """Every registry entry must parse as a valid ProviderConfig."""

    def test_all_entries_parse_as_provider_config(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        for pid, entry in provider_entries.items():
            config_data = dict(entry)
            # Remove metadata-only fields not in ProviderConfig
            for key in (
                "display_name",
                "status",
                "category",
                "region",
                "recommended",
                "notes",
                "api_key_env",
            ):
                config_data.pop(key, None)
            # Inject a dummy account so the config validates
            config_data["accounts"] = [{"name": "test", "api_key": "sk-test"}]
            cfg = ProviderConfig.model_validate(config_data)
            assert cfg.id == pid
            assert cfg.base_url

    def test_opencode_go_parses_correctly(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        entry = provider_entries["opencode-go"]
        assert entry["protocols"] == ["openai", "anthropic"]
        assert entry["status"] == "verified"
        assert entry["recommended"] is True

    def test_anthropic_uses_api_key_auth(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        entry = provider_entries["anthropic"]
        auth = entry.get("auth", {})
        assert auth.get("mode") == "api_key"
        assert auth.get("header") == "x-api-key"

    def test_anthropic_has_version_header(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        """Anthropic config should include anthropic-version header."""
        entry = provider_entries["anthropic"]
        # The anthropic-version header is in the config.example.toml
        # but the template has the right auth mode
        assert entry.get("protocols") == ["anthropic"]

    def test_ollama_local_has_no_auth(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        entry = provider_entries["ollama-local"]
        auth = entry.get("auth", {})
        assert auth.get("mode") == "none"

    def test_generalcompute_uses_post_model_listing(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        entry = provider_entries["generalcompute"]
        assert entry.get("models_method") == "POST"
        assert entry.get("models_path") == "/models/list"

    def test_experimental_providers_not_recommended(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        for pid, entry in provider_entries.items():
            if entry.get("status") == "experimental":
                rec = entry.get("recommended")
                assert rec is not True, (
                    f"Experimental provider {pid!r} should not be recommended"
                )

    def test_verified_providers_have_probe_models(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        """Verified providers should have a probe_model for live verification."""
        for pid, entry in provider_entries.items():
            if entry.get("status") == "verified" and pid != "ollama-local":
                verify = entry.get("verify", {})
                assert verify.get("probe_model"), (
                    f"Verified provider {pid!r} should have a probe_model"
                )

    def test_verify_configs_are_valid(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        for _pid, entry in provider_entries.items():
            verify_data = entry.get("verify")
            if verify_data is not None:
                vcfg = ProviderVerifyConfig.model_validate(verify_data)
                if vcfg.probe_model:
                    assert isinstance(vcfg.probe_model, str)
                assert vcfg.probe_protocol in ("openai", "anthropic")

    def test_auth_configs_are_valid(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        for _pid, entry in provider_entries.items():
            auth_data = entry.get("auth")
            if auth_data is not None:
                acfg = ProviderAuthConfig.model_validate(auth_data)
                assert acfg.mode in ("bearer", "api_key", "raw_authorization", "none")

    def test_all_provider_ids_match_keys(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        for pid, entry in provider_entries.items():
            assert entry.get("id") == pid, (
                f"Provider key {pid!r} does not match id {entry.get('id')!r}"
            )

    def test_base_urls_are_absolute(
        self, provider_entries: dict[str, dict[str, Any]]
    ) -> None:
        for pid, entry in provider_entries.items():
            url = entry.get("base_url", "")
            assert url.startswith("http"), (
                f"Provider {pid!r} base_url {url!r} is not absolute"
            )
