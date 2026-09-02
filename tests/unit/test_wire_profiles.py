"""Focused tests for wire-surface registry and provider contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from eggpool.config_validation import ConfigSchemaError, compute_runtime_fingerprint
from eggpool.errors import ConfigError
from eggpool.models.config import (
    AppConfig,
    ProviderAuthConfig,
    ProviderConfig,
    ProviderStaticHeaderConfig,
    ProviderWireSurfaceConfig,
)
from eggpool.providers.contract import (
    build_wire_profile_headers,
    compose_provider_url,
)
from eggpool.wire.registry import (
    WireRegistryError,
    load_wire_registry,
    load_wire_registry_text,
    resolve_provider_wire_profiles,
)
from eggpool.wire.types import render_wire_path_template


def test_legacy_provider_synthesizes_chat_responses_and_messages() -> None:
    provider = ProviderConfig(
        id="legacy",
        base_url="https://example.test/v1",
        protocols=["openai", "anthropic"],
        responses_path="/responses",
    )

    assert set(provider.wire_surfaces) == {
        "openai_chat_completions",
        "openai_responses",
        "anthropic_messages",
    }
    assert provider.wire_surfaces["openai_responses"].path_template == "/responses"


def test_explicit_surfaces_override_legacy_fields() -> None:
    provider = ProviderConfig(
        id="explicit",
        base_url="https://example.test/v1",
        protocols=["openai"],
        openai_path="/old-chat",
        wire_surfaces={
            "openai_chat_completions": ProviderWireSurfaceConfig(
                path_template="/new-chat", priority=7
            )
        },
    )

    profiles = resolve_provider_wire_profiles(provider)
    assert len(profiles) == 1
    assert profiles[0].path_template == "/new-chat"
    assert profiles[0].path_for("model-x") == "/new-chat"


def test_surface_auth_uses_same_key_without_emitting_other_surface_auth() -> None:
    provider = ProviderConfig(
        id="multi",
        base_url="https://example.test/v1",
        protocols=["openai", "anthropic"],
        auth=ProviderAuthConfig(mode="bearer"),
        wire_surfaces={
            "openai_responses": ProviderWireSurfaceConfig(
                path_template="/responses",
                auth=ProviderAuthConfig(mode="bearer"),
            ),
            "anthropic_messages": ProviderWireSurfaceConfig(
                path_template="/messages",
                auth=ProviderAuthConfig(mode="api_key", header="x-api-key"),
            ),
        },
    )
    profiles = resolve_provider_wire_profiles(provider)

    profiles_by_surface = {profile.surface: profile for profile in profiles}
    response_headers = build_wire_profile_headers(
        provider, profiles_by_surface["openai_responses"], "secret"
    )
    messages_headers = build_wire_profile_headers(
        provider, profiles_by_surface["anthropic_messages"], "secret"
    )
    assert response_headers == {"Authorization": "Bearer secret"}
    assert messages_headers == {"x-api-key": "secret"}


def test_surface_static_header_collision_is_rejected() -> None:
    with pytest.raises(ConfigError, match="conflicts with selected auth"):
        ProviderConfig(
            id="collision",
            base_url="https://example.test",
            wire_surfaces={
                "openai_chat_completions": ProviderWireSurfaceConfig(
                    path_template="/chat",
                    headers=[
                        ProviderStaticHeaderConfig(
                            name="Authorization", value="not-a-credential"
                        )
                    ],
                )
            },
        )


def test_model_path_template_is_safe_and_supports_stream_path() -> None:
    assert render_wire_path_template(
        "/models/{model}:generateContent", "gemini-2.5"
    ) == ("/models/gemini-2.5:generateContent")
    assert render_wire_path_template("/models/{model}", "provider/model") == (
        "/models/provider%2Fmodel"
    )
    with pytest.raises(ValueError, match="Unknown wire path"):
        render_wire_path_template("/models/{account}", "model")


def test_unknown_template_placeholder_is_rejected_by_provider_config() -> None:
    with pytest.raises(ValueError, match="Unknown wire path"):
        ProviderWireSurfaceConfig(path_template="/models/{account}")


def test_unknown_template_placeholder_is_rejected_by_check_config(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid.toml"
    path.write_text(
        """
[providers.example]
id = "example"
base_url = "https://example.test"

[providers.example.wire_surfaces.openai_chat_completions]
path_template = "/models/{account}"
""",
        encoding="utf-8",
    )
    from eggpool.config_validation import validate_config_file

    with pytest.raises(ConfigSchemaError, match="Unknown wire path"):
        validate_config_file(path)


def test_unknown_codec_id_fails_closed() -> None:
    with pytest.raises(WireRegistryError, match="unknown codec"):
        load_wire_registry_text(
            """
[profiles.openai_chat_completions]
request_codec = "not-python-registered"
response_codec = "openai_chat"
stream_codec = "openai_chat_sse"
"""
        )


def test_hints_for_unavailable_provider_surface_are_ignored() -> None:
    provider = ProviderConfig(
        id="opencode-go",
        base_url="https://example.test/v1",
        protocols=["openai"],
        wire_surfaces={
            "gemini_interactions": {"path_template": "/interactions"},
        },
    )
    registry = load_wire_registry()
    assert registry.hints_for_provider(provider) == ()


def test_opencode_go_resolves_three_profiles_without_provider_branch() -> None:
    provider = ProviderConfig(
        id="opencode-go",
        base_url="https://opencode.ai/zen/go/v1",
        protocols=["openai", "anthropic"],
        auth=ProviderAuthConfig(mode="bearer"),
        wire_surfaces={
            "openai_chat_completions": ProviderWireSurfaceConfig(
                path_template="/chat/completions", priority=100
            ),
            "openai_responses": ProviderWireSurfaceConfig(
                path_template="/responses", priority=90
            ),
            "anthropic_messages": ProviderWireSurfaceConfig(
                path_template="/messages",
                priority=100,
                auth=ProviderAuthConfig(mode="api_key", header="x-api-key"),
            ),
        },
    )
    profiles = resolve_provider_wire_profiles(provider)
    assert [profile.surface for profile in profiles] == [
        "openai_responses",
        "anthropic_messages",
        "openai_chat_completions",
    ]
    assert compose_provider_url(provider, profiles[0].path_for("gpt-5.6-luna")) == (
        "https://opencode.ai/zen/go/v1/responses"
    )


def test_wire_surface_changes_invalidate_runtime_fingerprint() -> None:
    first = AppConfig.from_dict(
        {
            "providers": {
                "p": {
                    "id": "p",
                    "base_url": "https://example.test/v1",
                    "wire_surfaces": {
                        "openai_chat_completions": {"path_template": "/chat"}
                    },
                }
            }
        }
    )
    second = first.model_copy(deep=True)
    second.providers["p"].wire_surfaces[
        "openai_chat_completions"
    ].path_template = "/chat-v2"
    assert compute_runtime_fingerprint(first) != compute_runtime_fingerprint(second)


def test_wire_negotiation_config_is_bounded() -> None:
    config = AppConfig.from_dict(
        {"routing": {"wire_negotiation": {"max_concurrent_per_provider": 8}}}
    )
    assert config.routing.wire_negotiation.max_concurrent_per_provider == 8
    with pytest.raises(ConfigError):
        AppConfig.from_dict({"routing": {"wire_negotiation": {"cache_max_entries": 0}}})
