"""Closed, packaged wire-profile registry.

The TOML file selects identifiers from the Python-owned codec table. It cannot
import code or provide executable behavior.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from eggpool.wire.types import (
    WIRE_SURFACE_NAMES,
    AuthHeaderShape,
    ResolvedAuthShape,
    WireHeaderSpec,
    WireProfile,
    WireSurfaceName,
)

if TYPE_CHECKING:
    from eggpool.models.config import ProviderConfig

_PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\Z")
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_REGISTRY_TOP_LEVEL_KEYS = frozenset({"profiles", "hints"})
_PROFILE_KEYS = frozenset({"request_codec", "response_codec", "stream_codec"})
_HINT_KEYS = frozenset(
    {"provider_id", "model_id", "preferred_surface", "verified_on", "source"}
)


class WireRegistryError(ValueError):
    """The packaged wire registry is malformed or unsafe to use."""


@dataclass(frozen=True, slots=True)
class WireProfileDefinition:
    """Codec IDs for one built-in surface."""

    surface: WireSurfaceName
    request_codec: str
    response_codec: str
    stream_codec: str


@dataclass(frozen=True, slots=True)
class WireHint:
    """Low-authority provider/model surface preference metadata."""

    provider_id: str
    model_id: str
    preferred_surface: WireSurfaceName
    verified_on: str
    source: str


@dataclass(frozen=True, slots=True)
class RegisteredWireCodec:
    """Placeholder binding proving a codec ID is Python-registered.

    Codec behavior is intentionally implemented by later wire-codec phases;
    the registry never turns TOML strings into imports or callables.
    """

    codec_id: str


CodecFactory = Callable[[], RegisteredWireCodec]


def _codec_factory(codec_id: str) -> CodecFactory:
    def factory() -> RegisteredWireCodec:
        return RegisteredWireCodec(codec_id)

    return factory


BUILTIN_CODEC_FACTORIES: Mapping[str, CodecFactory] = MappingProxyType(
    {
        codec_id: _codec_factory(codec_id)
        for codec_id in (
            "openai_chat",
            "openai_chat_sse",
            "openai_responses",
            "openai_responses_sse",
            "anthropic_messages",
            "anthropic_messages_sse",
            "gemini_interactions",
            "gemini_interactions_sse",
            "gemini_generate_content",
            "gemini_generate_content_sse",
        )
    }
)


@dataclass(frozen=True, slots=True)
class WireRegistry:
    """Validated immutable registry content."""

    profiles: Mapping[str, WireProfileDefinition]
    hints: tuple[WireHint, ...]

    def hints_for_provider(self, provider: ProviderConfig) -> tuple[WireHint, ...]:
        """Return applicable hints, ignoring unavailable low-authority surfaces."""
        surfaces = provider.wire_surfaces
        return tuple(
            hint
            for hint in self.hints
            if hint.provider_id == provider.id and hint.preferred_surface in surfaces
        )


def load_wire_registry() -> WireRegistry:
    """Load and validate the packaged developer-facing registry."""
    try:
        raw_text = (
            files("eggpool.providers")
            .joinpath("_wire_profiles.toml")
            .read_text(encoding="utf-8")
        )
    except OSError as exc:
        raise WireRegistryError("Packaged _wire_profiles.toml is unavailable") from exc
    return load_wire_registry_text(raw_text)


def load_wire_registry_text(text: str) -> WireRegistry:
    """Parse registry text; exposed for focused validation tests."""
    try:
        raw: dict[str, object] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise WireRegistryError(f"Invalid wire registry TOML: {exc}") from exc
    if set(raw) - _REGISTRY_TOP_LEVEL_KEYS:
        unsupported = sorted(set(raw) - _REGISTRY_TOP_LEVEL_KEYS)
        raise WireRegistryError(f"Unsupported wire registry keys: {unsupported!r}")

    raw_profiles_value = raw.get("profiles")
    if not isinstance(raw_profiles_value, dict) or not raw_profiles_value:
        raise WireRegistryError(
            "Wire registry must define a non-empty [profiles] table"
        )
    raw_profiles = cast("dict[str, object]", raw_profiles_value)
    profiles: dict[str, WireProfileDefinition] = {}
    for surface, raw_profile in raw_profiles.items():
        if surface in profiles:
            raise WireRegistryError(f"Duplicate wire profile ID {surface!r}")
        if surface not in WIRE_SURFACE_NAMES:
            raise WireRegistryError(f"Unknown wire surface name {surface!r}")
        if not isinstance(raw_profile, dict):
            raise WireRegistryError(f"Wire profile {surface!r} must be a table")
        profile = cast("dict[str, object]", raw_profile)
        extra = set(profile) - _PROFILE_KEYS
        if extra:
            raise WireRegistryError(
                f"Wire profile {surface!r} has unsupported keys: {sorted(extra)!r}"
            )
        missing = _PROFILE_KEYS - set(profile)
        if missing:
            raise WireRegistryError(
                f"Wire profile {surface!r} is missing keys: {sorted(missing)!r}"
            )
        codec_ids: list[str] = []
        for key in ("request_codec", "response_codec", "stream_codec"):
            codec_id = profile[key]
            if not isinstance(codec_id, str) or not codec_id:
                raise WireRegistryError(
                    f"Wire profile {surface!r} field {key!r} must be a string"
                )
            if codec_id not in BUILTIN_CODEC_FACTORIES:
                raise WireRegistryError(
                    f"Wire profile {surface!r} references unknown codec {codec_id!r}"
                )
            codec_ids.append(codec_id)
        typed_surface = cast("WireSurfaceName", surface)
        profiles[surface] = WireProfileDefinition(typed_surface, *codec_ids)

    raw_hints_value = raw.get("hints", [])
    if not isinstance(raw_hints_value, list):
        raise WireRegistryError("Wire registry hints must be an array of tables")
    raw_hints = cast("list[object]", raw_hints_value)
    hints: list[WireHint] = []
    for raw_hint in raw_hints:
        if not isinstance(raw_hint, dict):
            raise WireRegistryError("Each wire registry hint must be a table")
        hint = cast("dict[str, object]", raw_hint)
        extra = set(hint) - _HINT_KEYS
        missing = _HINT_KEYS - set(hint)
        if extra or missing:
            problems = sorted(extra | missing)
            raise WireRegistryError(
                f"Wire hint must define exactly {_HINT_KEYS!r}; "
                f"unsupported/missing keys: {problems!r}"
            )
        provider_id = hint["provider_id"]
        model_id = hint["model_id"]
        preferred_surface = hint["preferred_surface"]
        verified_on = hint["verified_on"]
        source = hint["source"]
        if (
            not isinstance(provider_id, str)
            or _PROVIDER_ID_RE.fullmatch(provider_id) is None
        ):
            raise WireRegistryError(f"Malformed wire hint provider ID {provider_id!r}")
        if not isinstance(model_id, str) or _MODEL_ID_RE.fullmatch(model_id) is None:
            raise WireRegistryError(f"Malformed wire hint model ID {model_id!r}")
        if not isinstance(preferred_surface, str) or preferred_surface not in profiles:
            raise WireRegistryError(
                f"Wire hint for {provider_id!r}/{model_id!r} references unknown "
                f"profile {preferred_surface!r}"
            )
        if not isinstance(verified_on, str) or not verified_on:
            raise WireRegistryError("Wire hint verified_on must be a non-empty string")
        if not isinstance(source, str) or not source.strip():
            raise WireRegistryError("Wire hint source must be a non-empty string")
        hints.append(
            WireHint(
                provider_id=provider_id,
                model_id=model_id,
                preferred_surface=cast("WireSurfaceName", preferred_surface),
                verified_on=verified_on,
                source=source,
            )
        )
    return WireRegistry(MappingProxyType(profiles), tuple(hints))


def resolve_provider_wire_profiles(
    provider: ProviderConfig,
    *,
    registry: WireRegistry | None = None,
) -> tuple[WireProfile, ...]:
    """Resolve configured provider surfaces into immutable wire profiles."""
    selected_registry = registry or load_wire_registry()
    result: list[WireProfile] = []
    for surface, surface_config in sorted(
        provider.wire_surfaces.items(), key=lambda item: (item[1].priority, item[0])
    ):
        definition = selected_registry.profiles.get(surface)
        if definition is None:
            raise WireRegistryError(
                f"No registry definition for wire surface {surface!r}"
            )
        auth_config = surface_config.auth or provider.auth
        additional = tuple(
            AuthHeaderShape(entry.mode, entry.header, entry.scheme)
            for entry in auth_config.additional
        )
        auth = ResolvedAuthShape(
            mode=auth_config.mode,
            header=auth_config.header,
            scheme=auth_config.scheme,
            additional=additional,
        )
        headers = tuple(
            WireHeaderSpec(header.name, header.value, header.value_env)
            for header in surface_config.headers
        )
        result.append(
            WireProfile(
                surface=surface,
                request_codec=definition.request_codec,
                response_codec=definition.response_codec,
                stream_codec=definition.stream_codec,
                path_template=surface_config.path_template,
                stream_path_template=surface_config.stream_path_template,
                auth=auth,
                headers=headers,
            )
        )
    return tuple(result)
