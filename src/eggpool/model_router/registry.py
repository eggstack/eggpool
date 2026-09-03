"""Immutable, generation-owned compilation for model-router configuration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar

from eggpool.errors import ConfigError
from eggpool.model_router.config import (
    COMPILED_POLICY_MAX_BYTES,
    SELECTOR_PROTOCOL_VERSION,
    ModelRouterConfig,
    normalize_route_description,
    validate_model_router_mapping,
    validate_virtual_model_id,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


@dataclass(frozen=True, slots=True)
class CompiledModelRoute:
    """Immutable route data consumed by later selector/request phases."""

    route_id: str
    label: str
    model: str
    description: str


@dataclass(frozen=True, slots=True)
class CompiledModelRouter:
    """Immutable request-path representation of one model router."""

    virtual_model: str
    selector_model: str
    default_model: str
    routes: tuple[CompiledModelRoute, ...]
    route_by_id: Mapping[str, CompiledModelRoute]
    config_fingerprint: str
    static_policy: bytes
    sticky: bool
    affinity_ttl_s: float
    selector_timeout_s: float
    max_input_bytes: int
    repair_attempts: int


def _length_delimited_hash(fields: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for field in fields:
        encoded = field.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _compile_policy(
    virtual_model: str,
    router: ModelRouterConfig,
    routes: tuple[CompiledModelRoute, ...],
) -> bytes:
    policy = {
        "default_model": router.default_model,
        "protocol_version": SELECTOR_PROTOCOL_VERSION,
        "routes": [
            {
                "description": route.description,
                "id": route.route_id,
                "label": route.label,
                "model": route.model,
            }
            for route in routes
        ],
        "selector_model": router.selector_model,
        "virtual_model": virtual_model,
    }
    encoded = json.dumps(
        policy,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > COMPILED_POLICY_MAX_BYTES:
        raise ConfigError(
            f"compiled policy for model router {virtual_model!r} exceeds "
            f"the {COMPILED_POLICY_MAX_BYTES}-byte limit"
        )
    return encoded


def compile_model_router(
    virtual_model: str,
    router: ModelRouterConfig,
) -> CompiledModelRouter:
    """Compile one structurally valid router without catalog/network access."""
    validate_virtual_model_id(virtual_model)
    routes = tuple(
        CompiledModelRoute(
            route_id=str(index),
            label=label,
            model=route_config.model,
            description=normalize_route_description(route_config.description),
        )
        for index, (label, route_config) in enumerate(sorted(router.routes.items()))
    )
    route_by_id: Mapping[str, CompiledModelRoute] = MappingProxyType(
        {route.route_id: route for route in routes}
    )
    static_policy = _compile_policy(virtual_model, router, routes)
    fingerprint = _length_delimited_hash(
        (
            SELECTOR_PROTOCOL_VERSION,
            virtual_model,
            router.selector_model,
            router.default_model,
            *(
                field
                for route in routes
                for field in (
                    route.label,
                    route.model,
                    route.description,
                )
            ),
            str(router.sticky),
            repr(router.affinity_ttl_s),
            repr(router.selector_timeout_s),
            str(router.max_input_bytes),
            str(router.repair_attempts),
        )
    )
    return CompiledModelRouter(
        virtual_model=virtual_model,
        selector_model=router.selector_model,
        default_model=router.default_model,
        routes=routes,
        route_by_id=route_by_id,
        config_fingerprint=fingerprint,
        static_policy=static_policy,
        sticky=router.sticky,
        affinity_ttl_s=router.affinity_ttl_s,
        selector_timeout_s=router.selector_timeout_s,
        max_input_bytes=router.max_input_bytes,
        repair_attempts=router.repair_attempts,
    )


class ModelRouterRegistry:
    """Immutable lookup registry owned by one runtime generation."""

    _empty: ClassVar[ModelRouterRegistry | None] = None

    def __init__(self, routers: Mapping[str, CompiledModelRouter]) -> None:
        self._routers: Mapping[str, CompiledModelRouter] = MappingProxyType(
            dict(routers)
        )

    @classmethod
    def empty(cls) -> ModelRouterRegistry:
        """Return the shared feature-off registry."""
        if cls._empty is None:
            cls._empty = cls({})
        return cls._empty

    @classmethod
    def from_config(
        cls,
        model_routers: Mapping[str, ModelRouterConfig],
    ) -> ModelRouterRegistry:
        """Compile all routers in deterministic virtual-ID order."""
        if not model_routers:
            return cls.empty()
        validate_model_router_mapping(model_routers)
        return cls(
            {
                virtual_model: compile_model_router(
                    virtual_model,
                    model_routers[virtual_model],
                )
                for virtual_model in sorted(model_routers)
            }
        )

    def get(self, virtual_model_id: str) -> CompiledModelRouter | None:
        """Return an exact virtual alias match, if configured."""
        return self._routers.get(virtual_model_id)

    def is_virtual(self, model_id: str) -> bool:
        """Return whether ``model_id`` is an exact configured virtual alias."""
        return model_id in self._routers

    @property
    def virtual_model_ids(self) -> tuple[str, ...]:
        """Return exact aliases in deterministic order."""
        return tuple(self._routers)

    def __len__(self) -> int:
        return len(self._routers)

    def __iter__(self) -> Iterator[str]:
        return iter(self._routers)
