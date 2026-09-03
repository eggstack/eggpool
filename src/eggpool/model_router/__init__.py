"""Typed configuration and compiled registry for optional model routers."""

from __future__ import annotations

from eggpool.model_router.affinity import (
    AFFINITY_CACHE_MAX_ENTRIES,
    AFFINITY_SESSION_HEADER,
    AffinityDecision,
    AffinityResolution,
    AffinityStats,
    ModelRouterAffinity,
    SessionIdentity,
    automatic_session_identity,
    session_identity_from_header,
)
from eggpool.model_router.config import ModelRouteConfig, ModelRouterConfig
from eggpool.model_router.registry import (
    CompiledModelRoute,
    CompiledModelRouter,
    ModelRouterRegistry,
    compile_model_router,
)
from eggpool.model_router.selector import ModelRouterSelector, ModelSelection

__all__ = [
    "CompiledModelRoute",
    "CompiledModelRouter",
    "AFFINITY_CACHE_MAX_ENTRIES",
    "AFFINITY_SESSION_HEADER",
    "AffinityDecision",
    "AffinityResolution",
    "AffinityStats",
    "ModelRouteConfig",
    "ModelRouterAffinity",
    "ModelRouterConfig",
    "ModelRouterRegistry",
    "ModelRouterSelector",
    "ModelSelection",
    "SessionIdentity",
    "automatic_session_identity",
    "compile_model_router",
    "session_identity_from_header",
]
