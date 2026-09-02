"""Typed upstream wire-surface contracts."""

from __future__ import annotations

from eggpool.wire.registry import (
    BUILTIN_CODEC_FACTORIES,
    WireHint,
    WireProfileDefinition,
    WireRegistry,
    WireRegistryError,
    load_wire_registry,
    load_wire_registry_text,
    resolve_provider_wire_profiles,
)
from eggpool.wire.types import (
    AuthHeaderShape,
    ResolvedAuthShape,
    WireHeaderSpec,
    WireProfile,
    WireSurfaceName,
    render_wire_path_template,
    validate_wire_path_template,
)

__all__ = [
    "AuthHeaderShape",
    "BUILTIN_CODEC_FACTORIES",
    "ResolvedAuthShape",
    "WireHeaderSpec",
    "WireHint",
    "WireProfile",
    "WireProfileDefinition",
    "WireRegistry",
    "WireRegistryError",
    "WireSurfaceName",
    "load_wire_registry",
    "load_wire_registry_text",
    "render_wire_path_template",
    "resolve_provider_wire_profiles",
    "validate_wire_path_template",
]
