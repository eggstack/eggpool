"""Structural model-router configuration contracts."""

from __future__ import annotations

import pytest

from eggpool.config_reload_policy import ReloadDisposition, compute_diff
from eggpool.errors import ConfigError
from eggpool.models.config import AppConfig


def _router(
    *,
    selector_model: str = "selector/local",
    default_model: str = "model-a",
    routes: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "selector_model": selector_model,
        "default_model": default_model,
        "routes": routes
        or {
            "default": {
                "model": default_model,
                "description": "General purpose route.",
            }
        },
    }


def test_model_routers_are_empty_by_default() -> None:
    config = AppConfig()

    assert config.model_routers == {}


def test_unknown_top_level_model_router_field_is_rejected() -> None:
    with pytest.raises(ConfigError, match="Config validation failed"):
        AppConfig.from_dict({"model_routers": {"demo": _router()}, "unknown": True})


@pytest.mark.parametrize(
    "virtual_model",
    ["", "   ", "model/qualified", "line\nfeed", "model\x00id", "x" * 129],
)
def test_invalid_virtual_model_ids_are_rejected(virtual_model: str) -> None:
    with pytest.raises(ConfigError):
        AppConfig.from_dict({"model_routers": {virtual_model: _router()}})


def test_exact_virtual_alias_and_provider_qualified_references_are_preserved() -> None:
    config = AppConfig.from_dict(
        {
            "model_routers": {
                "Implementer-Hard": _router(
                    selector_model="selector/local-provider",
                    default_model="model-a/provider-a",
                    routes={
                        "default": {
                            "model": "model-a/provider-a",
                            "description": "Provider-qualified target.",
                        }
                    },
                )
            }
        }
    )

    assert config.model_routers["Implementer-Hard"].selector_model == (
        "selector/local-provider"
    )
    assert config.model_routers["Implementer-Hard"].routes["default"].model == (
        "model-a/provider-a"
    )


def test_multiple_independent_routers_are_valid() -> None:
    config = AppConfig.from_dict(
        {
            "model_routers": {
                "first": _router(default_model="first-model"),
                "second": _router(default_model="second-model"),
            }
        }
    )

    assert tuple(config.model_routers) == ("first", "second")


@pytest.mark.parametrize(
    "router_update, match",
    [
        ({"routes": {}}, "at least one route"),
        (
            {"routes": {"default": {"model": "model-a", "description": "   "}}},
            "description must not be empty",
        ),
        ({"default_model": "not-a-route"}, "default_model must exactly match"),
    ],
)
def test_invalid_route_definitions_are_rejected(
    router_update: dict[str, object], match: str
) -> None:
    router = _router()
    router.update(router_update)

    with pytest.raises(ConfigError, match=match):
        AppConfig.from_dict({"model_routers": {"demo": router}})


def test_route_labels_reject_control_characters() -> None:
    with pytest.raises(ConfigError, match="route label"):
        AppConfig.from_dict(
            {
                "model_routers": {
                    "demo": _router(
                        routes={
                            "bad\nlabel": {
                                "model": "model-a",
                                "description": "valid",
                            }
                        }
                    )
                }
            }
        )


@pytest.mark.parametrize(
    "routes, match",
    [
        (
            {"x" * 129: {"model": "model-a", "description": "valid"}},
            "route label must be at most",
        ),
        (
            {"default": {"model": "model-a", "description": "x" * 513}},
            "route description must be at most",
        ),
    ],
)
def test_route_text_has_utf8_byte_bounds(
    routes: dict[str, dict[str, str]], match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        AppConfig.from_dict({"model_routers": {"demo": _router(routes=routes)}})


def test_virtual_to_virtual_references_are_rejected_across_routers() -> None:
    with pytest.raises(ConfigError, match="cannot target virtual model"):
        AppConfig.from_dict(
            {
                "model_routers": {
                    "first": _router(selector_model="second"),
                    "second": _router(),
                }
            }
        )

    with pytest.raises(ConfigError, match="cannot target virtual model"):
        AppConfig.from_dict(
            {
                "model_routers": {
                    "first": _router(
                        default_model="second",
                        routes={
                            "default": {
                                "model": "second",
                                "description": "virtual target",
                            }
                        },
                    ),
                    "second": _router(),
                }
            }
        )


def test_structurally_valid_missing_catalog_models_are_accepted() -> None:
    config = AppConfig.from_dict(
        {
            "model_routers": {
                "future": _router(
                    selector_model="temporarily-offline/local",
                    default_model="not-discovered-yet",
                )
            }
        }
    )

    assert config.model_routers["future"].default_model == "not-discovered-yet"


def test_model_router_diff_is_one_live_metadata_only_change() -> None:
    old = AppConfig()
    new = AppConfig.from_dict({"model_routers": {"demo": _router()}})

    changes = compute_diff(old, new).changes

    assert [(change.path, change.disposition) for change in changes] == [
        ("model_routers", ReloadDisposition.LIVE)
    ]
    assert changes[0].old_display == "0 configured routers"
    assert changes[0].new_display == "1 configured router"
    assert "selector/local" not in changes[0].new_display


def test_router_numeric_bounds_are_conservative() -> None:
    with pytest.raises(ConfigError):
        AppConfig.from_dict(
            {
                "model_routers": {
                    "demo": {
                        **_router(),
                        "selector_timeout_s": 0.01,
                    }
                }
            }
        )
