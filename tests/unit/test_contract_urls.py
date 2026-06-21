"""Tests for provider URL composition across bundled templates."""

from __future__ import annotations

from pathlib import Path

import pytest

from eggpool.models.config import ProviderConfig
from eggpool.providers.contract import compose_provider_url


class TestProviderUrlComposition:
    def test_opencode_go_chat_url(self):
        cfg = ProviderConfig(
            id="opencode-go",
            base_url="https://opencode.ai/zen/go/v1",
            openai_path="/chat/completions",
        )
        assert compose_provider_url(cfg, cfg.openai_path) == (
            "https://opencode.ai/zen/go/v1/chat/completions"
        )

    def test_deepseek_openai_url(self):
        cfg = ProviderConfig(
            id="deepseek",
            base_url="https://api.deepseek.com",
            openai_path="/chat/completions",
        )
        assert compose_provider_url(cfg, cfg.openai_path) == (
            "https://api.deepseek.com/chat/completions"
        )

    def test_deepseek_anthropic_url(self):
        cfg = ProviderConfig(
            id="deepseek",
            base_url="https://api.deepseek.com",
            anthropic_path="/anthropic/messages",
        )
        assert compose_provider_url(cfg, cfg.anthropic_path) == (
            "https://api.deepseek.com/anthropic/messages"
        )

    def test_openrouter_chat_url(self):
        cfg = ProviderConfig(
            id="openrouter",
            base_url="https://openrouter.ai/api/v1",
            openai_path="/chat/completions",
        )
        assert compose_provider_url(cfg, cfg.openai_path) == (
            "https://openrouter.ai/api/v1/chat/completions"
        )

    def test_together_chat_url(self):
        cfg = ProviderConfig(
            id="together",
            base_url="https://api.together.ai/v1",
            openai_path="/chat/completions",
        )
        assert compose_provider_url(cfg, cfg.openai_path) == (
            "https://api.together.ai/v1/chat/completions"
        )

    def test_fireworks_chat_url(self):
        cfg = ProviderConfig(
            id="fireworks",
            base_url="https://api.fireworks.ai/inference/v1",
            openai_path="/chat/completions",
        )
        assert compose_provider_url(cfg, cfg.openai_path) == (
            "https://api.fireworks.ai/inference/v1/chat/completions"
        )

    def test_generalcompute_models_url_no_duplicate_v1(self):
        cfg = ProviderConfig(
            id="generalcompute",
            base_url="https://api.generalcompute.com/v1",
            openai_path="/chat/completions",
            models_path="/models/list",
        )
        url = compose_provider_url(cfg, cfg.models_path)
        assert url == "https://api.generalcompute.com/v1/models/list"
        assert url.count("/v1/") == 1, f"Duplicate /v1 in URL: {url}"

    def test_minimax_international_url(self):
        cfg = ProviderConfig(
            id="minimax",
            base_url="https://api.minimax.io/v1",
            openai_path="/chat/completions",
            models_path="/models",
        )
        assert compose_provider_url(cfg, cfg.openai_path) == (
            "https://api.minimax.io/v1/chat/completions"
        )
        assert compose_provider_url(cfg, cfg.models_path) == (
            "https://api.minimax.io/v1/models"
        )
        assert "/v1/v1" not in compose_provider_url(cfg, cfg.openai_path)

    def test_minimax_china_url(self):
        cfg = ProviderConfig(
            id="minimax-cn",
            base_url="https://api.minimaxi.com/v1",
            openai_path="/chat/completions",
            models_path="/models",
        )
        assert compose_provider_url(cfg, cfg.openai_path) == (
            "https://api.minimaxi.com/v1/chat/completions"
        )
        assert compose_provider_url(cfg, cfg.models_path) == (
            "https://api.minimaxi.com/v1/models"
        )
        assert "/v1/v1" not in compose_provider_url(cfg, cfg.openai_path)

    def test_ollama_local_url(self):
        cfg = ProviderConfig(
            id="ollama-local",
            base_url="http://localhost:11434/v1",
            openai_path="/chat/completions",
            models_path="/models",
        )
        assert compose_provider_url(cfg, cfg.openai_path) == (
            "http://localhost:11434/v1/chat/completions"
        )
        assert compose_provider_url(cfg, cfg.models_path) == (
            "http://localhost:11434/v1/models"
        )

    def test_alibaba_url(self):
        cfg = ProviderConfig(
            id="alibaba",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            openai_path="/chat/completions",
            models_path="/models",
        )
        assert compose_provider_url(cfg, cfg.openai_path) == (
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
        )

    @pytest.mark.parametrize(
        "base_url,path,expected",
        [
            (
                "https://api.example.com/v1",
                "/chat/completions",
                "https://api.example.com/v1/chat/completions",
            ),
            (
                "https://api.example.com",
                "/v1/chat/completions",
                "https://api.example.com/v1/chat/completions",
            ),
            (
                "http://localhost:11434/v1",
                "/models",
                "http://localhost:11434/v1/models",
            ),
            (
                "https://api.fireworks.ai/inference/v1",
                "/chat/completions",
                "https://api.fireworks.ai/inference/v1/chat/completions",
            ),
        ],
    )
    def test_url_composition_parametrized(self, base_url, path, expected):
        cfg = ProviderConfig(id="t", base_url=base_url)
        assert compose_provider_url(cfg, path) == expected


_BUNDLED_CONFIG = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "eggpool"
    / "_share"
    / "config.example.toml"
)

# Template provider configs that mirror config.example.toml.
# Each entry must match the uncommented template values exactly.
_TEMPLATE_PROVIDERS: dict[str, dict] = {
    "opencode-go": {
        "id": "opencode-go",
        "base_url": "https://opencode.ai/zen/go/v1",
        "protocols": ["openai", "anthropic"],
        "openai_path": "/chat/completions",
        "anthropic_path": "/messages",
        "models_path": "/models",
        "auth": {"mode": "bearer", "header": "Authorization", "scheme": "Bearer"},
    },
    "deepseek": {
        "id": "deepseek",
        "base_url": "https://api.deepseek.com",
        "protocols": ["openai", "anthropic"],
        "openai_path": "/chat/completions",
        "anthropic_path": "/anthropic/messages",
        "models_path": "/models",
        "auth": {"mode": "bearer"},
    },
    "openrouter": {
        "id": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "protocols": ["openai"],
        "openai_path": "/chat/completions",
        "models_path": "/models",
        "auth": {"mode": "bearer"},
    },
    "together": {
        "id": "together",
        "base_url": "https://api.together.ai/v1",
        "protocols": ["openai"],
        "openai_path": "/chat/completions",
        "models_path": "/models",
        "auth": {"mode": "bearer"},
    },
    "fireworks": {
        "id": "fireworks",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "protocols": ["openai"],
        "openai_path": "/chat/completions",
        "models_path": "/models",
        "auth": {"mode": "bearer"},
    },
    "zai": {
        "id": "zai",
        "base_url": "https://api.z.ai/api/paas/v4",
        "protocols": ["openai"],
        "openai_path": "/chat/completions",
        "models_path": "/models",
        "auth": {"mode": "bearer"},
    },
    "alibaba": {
        "id": "alibaba",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "protocols": ["openai"],
        "openai_path": "/chat/completions",
        "models_path": "/models",
        "auth": {"mode": "bearer"},
    },
    "novita": {
        "id": "novita",
        "base_url": "https://api.novita.ai/openai",
        "protocols": ["openai"],
        "openai_path": "/chat/completions",
        "models_path": "/models",
        "auth": {"mode": "bearer"},
    },
    "minimax": {
        "id": "minimax",
        "base_url": "https://api.minimax.io/v1",
        "protocols": ["openai"],
        "openai_path": "/chat/completions",
        "models_path": "/models",
        "auth": {"mode": "bearer"},
    },
    "minimax-cn": {
        "id": "minimax-cn",
        "base_url": "https://api.minimaxi.com/v1",
        "protocols": ["openai"],
        "openai_path": "/chat/completions",
        "models_path": "/models",
        "auth": {"mode": "bearer"},
    },
    "generalcompute": {
        "id": "generalcompute",
        "base_url": "https://api.generalcompute.com/v1",
        "protocols": ["openai"],
        "openai_path": "/chat/completions",
        "models_method": "POST",
        "models_path": "/models/list",
        "auth": {"mode": "bearer"},
    },
    "neuralwatt": {
        "id": "neuralwatt",
        "base_url": "https://api.neuralwatt.com/v1",
        "protocols": ["openai"],
        "openai_path": "/chat/completions",
        "models_path": "/models",
        "auth": {"mode": "bearer"},
    },
    "ollama-local": {
        "id": "ollama-local",
        "base_url": "http://localhost:11434/v1",
        "protocols": ["openai"],
        "openai_path": "/chat/completions",
        "models_path": "/models",
        "auth": {"mode": "none"},
    },
    "ollama-cloud": {
        "id": "ollama-cloud",
        "base_url": "https://ollama.com/v1",
        "protocols": ["openai"],
        "openai_path": "/chat/completions",
        "models_path": "/models",
        "auth": {"mode": "bearer"},
    },
}


def _build_provider_configs() -> dict[str, ProviderConfig]:
    return {
        pid: ProviderConfig.model_validate(dict(vals))
        for pid, vals in _TEMPLATE_PROVIDERS.items()
    }


_provider_configs_cache: dict[str, ProviderConfig] | None = None


def _get_provider_configs() -> dict[str, ProviderConfig]:
    global _provider_configs_cache  # noqa: PLW0603
    if _provider_configs_cache is None:
        _provider_configs_cache = _build_provider_configs()
    return _provider_configs_cache


class TestTemplateLinter:
    """Validate every bundled provider template for duplicate-version URLs."""

    def test_all_providers_parse(self):
        configs = _get_provider_configs()
        assert len(configs) >= 10, f"Expected at least 10 providers, got {len(configs)}"

    @pytest.mark.parametrize(
        "provider_id",
        [
            "opencode-go",
            "deepseek",
            "openrouter",
            "together",
            "fireworks",
            "zai",
            "alibaba",
            "novita",
            "minimax",
            "minimax-cn",
            "generalcompute",
            "neuralwatt",
            "ollama-local",
            "ollama-cloud",
        ],
    )
    def test_no_duplicate_version_in_chat_url(self, provider_id):
        cfg = _get_provider_configs()[provider_id]
        url = compose_provider_url(cfg, cfg.openai_path)
        assert url.count("/v1/") <= 1, (
            f"{provider_id}: chat URL contains multiple /v1/ segments: {url}"
        )
        assert "/v1/v1" not in url, (
            f"{provider_id}: chat URL contains duplicate /v1/v1: {url}"
        )

    @pytest.mark.parametrize(
        "provider_id",
        ["opencode-go", "deepseek"],
    )
    def test_no_duplicate_version_in_anthropic_url(self, provider_id):
        cfg = _get_provider_configs()[provider_id]
        if cfg.anthropic_path is None:
            pytest.skip(f"{provider_id} has no anthropic_path")
        url = compose_provider_url(cfg, cfg.anthropic_path)
        assert "/v1/v1" not in url, (
            f"{provider_id}: anthropic URL contains duplicate /v1/v1: {url}"
        )

    @pytest.mark.parametrize(
        "provider_id",
        [
            "opencode-go",
            "deepseek",
            "openrouter",
            "together",
            "fireworks",
            "zai",
            "alibaba",
            "novita",
            "minimax",
            "minimax-cn",
            "generalcompute",
            "neuralwatt",
            "ollama-local",
            "ollama-cloud",
        ],
    )
    def test_no_duplicate_version_in_models_url(self, provider_id):
        cfg = _get_provider_configs()[provider_id]
        models_path = cfg.models_path or "/models"
        url = compose_provider_url(cfg, models_path)
        assert "/v1/v1" not in url, (
            f"{provider_id}: models URL contains duplicate /v1/v1: {url}"
        )

    def test_generalcompute_models_url_single_v1(self):
        cfg = _get_provider_configs()["generalcompute"]
        url = compose_provider_url(cfg, cfg.models_path or "/models/list")
        assert url == "https://api.generalcompute.com/v1/models/list"

    def test_minimax_international_default_url(self):
        cfg = _get_provider_configs()["minimax"]
        assert cfg.base_url == "https://api.minimax.io/v1"
        assert cfg.openai_path == "/chat/completions"
        url = compose_provider_url(cfg, cfg.openai_path)
        assert url == "https://api.minimax.io/v1/chat/completions"

    def test_minimax_china_default_url(self):
        cfg = _get_provider_configs()["minimax-cn"]
        assert cfg.base_url == "https://api.minimaxi.com/v1"
        assert cfg.openai_path == "/chat/completions"
        url = compose_provider_url(cfg, cfg.openai_path)
        assert url == "https://api.minimaxi.com/v1/chat/completions"

    def test_minimax_templates_no_double_v1(self):
        for provider_id in ("minimax", "minimax-cn"):
            cfg = _get_provider_configs()[provider_id]
            for path in (cfg.openai_path, cfg.anthropic_path, cfg.models_path):
                if not path:
                    continue
                url = compose_provider_url(cfg, path)
                assert "/v1/v1" not in url, (
                    f"{provider_id}: {path} composes duplicate /v1/v1: {url}"
                )

    def test_ollama_local_no_auth(self):
        cfg = _get_provider_configs()["ollama-local"]
        assert cfg.auth.mode == "none"

    def test_bundled_config_file_exists(self):
        assert _BUNDLED_CONFIG.exists(), f"Bundled config not found: {_BUNDLED_CONFIG}"
