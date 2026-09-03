"""Typed configuration and compiled registry for optional model routers."""

from __future__ import annotations

from eggpool.model_router.config import ModelRouteConfig, ModelRouterConfig
from eggpool.model_router.registry import (
    CompiledModelRoute,
    CompiledModelRouter,
    ModelRouterRegistry,
    compile_model_router,
)

__all__ = [
    "CompiledModelRoute",
    "CompiledModelRouter",
    "ModelRouteConfig",
    "ModelRouterConfig",
    "ModelRouterRegistry",
    "compile_model_router",
]
