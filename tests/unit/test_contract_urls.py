"""Tests for provider URL composition across bundled templates."""

from __future__ import annotations

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

    def test_minimax_host_level_base_with_versioned_path(self):
        cfg = ProviderConfig(
            id="minimax",
            base_url="https://api.minimaxi.com",
            openai_path="/v1/chat/completions",
            anthropic_path="/anthropic/v1/messages",
            models_path="/v1/models",
        )
        assert compose_provider_url(cfg, cfg.openai_path) == (
            "https://api.minimaxi.com/v1/chat/completions"
        )
        assert compose_provider_url(cfg, cfg.anthropic_path) == (
            "https://api.minimaxi.com/anthropic/v1/messages"
        )
        assert compose_provider_url(cfg, cfg.models_path) == (
            "https://api.minimaxi.com/v1/models"
        )

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
