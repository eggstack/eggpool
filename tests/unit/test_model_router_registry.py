"""Deterministic model-router compilation and registry contracts."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from eggpool.errors import ConfigError
from eggpool.model_router.config import ModelRouterConfig
from eggpool.model_router.registry import ModelRouterRegistry, compile_model_router
from eggpool.models.config import AppConfig
from eggpool.runtime_manager import RuntimeGenerationBuilder


def _config(
    routes: dict[str, dict[str, str]],
    **overrides: object,
) -> ModelRouterConfig:
    values: dict[str, object] = {
        "selector_model": "selector/local",
        "default_model": next(iter(routes.values()))["model"],
        "routes": routes,
    }
    values.update(overrides)
    return ModelRouterConfig.model_validate(values)


def test_route_order_and_compact_ids_ignore_source_mapping_order() -> None:
    first = compile_model_router(
        "virtual",
        _config(
            {
                "z-last": {"model": "model-z", "description": "Z"},
                "a-first": {"model": "model-a", "description": "A"},
            },
            default_model="model-a",
        ),
    )
    second = compile_model_router(
        "virtual",
        _config(
            {
                "a-first": {"model": "model-a", "description": "A"},
                "z-last": {"model": "model-z", "description": "Z"},
            },
            default_model="model-a",
        ),
    )

    assert [(route.route_id, route.label) for route in first.routes] == [
        ("0", "a-first"),
        ("1", "z-last"),
    ]
    assert first.routes == second.routes
    assert first.static_policy == second.static_policy
    assert first.config_fingerprint == second.config_fingerprint


def test_description_compilation_is_deterministically_normalized() -> None:
    router = compile_model_router(
        "virtual",
        _config(
            {
                "default": {
                    "model": "model-a",
                    "description": "  one\t\n two  ",
                }
            }
        ),
    )

    assert router.routes[0].description == "one two"


def test_fingerprint_changes_when_decision_semantics_change() -> None:
    baseline = compile_model_router(
        "virtual",
        _config({"default": {"model": "model-a", "description": "A"}}),
    )
    changed = compile_model_router(
        "virtual",
        _config(
            {"default": {"model": "model-b", "description": "A"}},
            default_model="model-b",
            selector_timeout_s=3.0,
        ),
    )

    assert len(baseline.config_fingerprint) == 64
    assert baseline.config_fingerprint != changed.config_fingerprint


def test_registry_is_immutable_and_empty_registry_is_shared() -> None:
    empty_a = ModelRouterRegistry.from_config({})
    empty_b = ModelRouterRegistry.empty()
    compiled = compile_model_router(
        "virtual",
        _config({"default": {"model": "model-a", "description": "A"}}),
    )

    assert empty_a is empty_b
    assert empty_a.virtual_model_ids == ()
    with pytest.raises(TypeError):
        compiled.route_by_id["0"] = compiled.routes[0]  # type: ignore[index]


def test_virtual_alias_wins_exact_collision_with_concrete_catalog_name() -> None:
    config = AppConfig.from_dict(
        {
            "model_routers": {
                "gpt-4": {
                    "selector_model": "selector/local",
                    "default_model": "gpt-4-real",
                    "routes": {
                        "default": {
                            "model": "gpt-4-real",
                            "description": "Virtual alias route.",
                        }
                    },
                }
            }
        }
    )
    registry = ModelRouterRegistry.from_config(config.model_routers)

    assert registry.is_virtual("gpt-4")
    assert registry.get("gpt-4") is not None
    assert registry.get("gpt-4/provider-a") is None


def test_compiled_policy_has_an_aggregate_size_ceiling() -> None:
    routes = {
        f"route-{index:03d}": {
            "model": "model-a",
            "description": "x" * 512,
        }
        for index in range(140)
    }
    config = AppConfig.from_dict(
        {
            "model_routers": {
                "large": {
                    "selector_model": "selector/local",
                    "default_model": "model-a",
                    "routes": routes,
                }
            }
        }
    )

    with pytest.raises(ConfigError, match="compiled policy"):
        ModelRouterRegistry.from_config(config.model_routers)


@pytest.mark.asyncio
async def test_generation_builder_carries_the_compiled_registry() -> None:
    config = AppConfig()
    account_registry = MagicMock()
    account_registry.get_provider_ids.return_value = ()
    account_registry.get_enabled_states.return_value = ()
    compiled_registry = ModelRouterRegistry.from_config(
        {"virtual": _config({"default": {"model": "model-a", "description": "A"}})}
    )
    services = {
        name: MagicMock()
        for name in (
            "catalog",
            "router",
            "coordinator",
            "client_pool",
            "outbound_manager",
            "health_manager",
            "cost_calculator",
            "transcoder_policy",
            "dispatch_overhead_recorder",
            "dispatch_span_recorder",
            "account_backoff_repo",
            "stats_service",
            "supervisor",
        )
    }
    services["registry"] = account_registry
    services["model_router_registry"] = compiled_registry

    result = await RuntimeGenerationBuilder().build_initial(
        config,
        MagicMock(),
        **services,
    )

    assert result.generation.model_router_registry is compiled_registry
