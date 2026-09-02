"""Immutable wire-profile values and safe endpoint-template helpers."""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Literal, TypeAlias
from urllib.parse import quote

WireSurfaceName: TypeAlias = Literal[
    "openai_chat_completions",
    "openai_responses",
    "anthropic_messages",
    "gemini_interactions",
    "gemini_generate_content",
]

WIRE_SURFACE_NAMES: frozenset[str] = frozenset(
    {
        "openai_chat_completions",
        "openai_responses",
        "anthropic_messages",
        "gemini_interactions",
        "gemini_generate_content",
    }
)
_ALLOWED_TEMPLATE_FIELDS = frozenset({"model"})


@dataclass(frozen=True, slots=True)
class AuthHeaderShape:
    """One additional credential header shape, without a credential."""

    mode: str
    header: str
    scheme: str


@dataclass(frozen=True, slots=True)
class ResolvedAuthShape:
    """The selected auth rendering shape, never the account secret."""

    mode: str
    header: str
    scheme: str
    additional: tuple[AuthHeaderShape, ...] = ()


@dataclass(frozen=True, slots=True)
class WireHeaderSpec:
    """One configured non-auth header belonging to a wire profile."""

    name: str
    value: str | None = None
    value_env: str | None = None


@dataclass(frozen=True, slots=True)
class WireProfile:
    """Immutable structural dispatch facts for one upstream wire surface."""

    surface: WireSurfaceName
    request_codec: str
    response_codec: str
    stream_codec: str
    path_template: str
    stream_path_template: str | None
    auth: ResolvedAuthShape
    headers: tuple[WireHeaderSpec, ...] = ()

    def path_for(self, model_id: str, *, streaming: bool = False) -> str:
        """Render the request path for a canonical model ID."""
        template = (
            self.stream_path_template
            if streaming and self.stream_path_template is not None
            else self.path_template
        )
        return render_wire_path_template(template, model_id)


def validate_wire_path_template(value: str) -> str:
    """Validate the deliberately small wire path-template language."""
    if not value or value != value.strip():
        raise ValueError("Wire path template must be a non-empty trimmed path")
    if not value.startswith("/"):
        raise ValueError("Wire path template must be relative and start with '/'")
    if "?" in value or "#" in value:
        raise ValueError("Wire path template must not contain a query or fragment")
    try:
        fields: set[str] = set()
        for _literal, field_name, format_spec, conversion in string.Formatter().parse(
            value
        ):
            if field_name is not None:
                fields.add(field_name)
            if format_spec or conversion:
                raise ValueError(
                    "Wire path templates support only the '{model}' placeholder"
                )
    except ValueError as exc:
        if str(exc).startswith("Wire path templates"):
            raise
        raise ValueError(f"Malformed wire path template: {value!r}") from exc
    unknown = fields - _ALLOWED_TEMPLATE_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown wire path template placeholder(s): {sorted(unknown)!r}; "
            "only '{model}' is supported"
        )
    return value


def render_wire_path_template(template: str, model_id: str) -> str:
    """Render a safe canonical model ID into a validated relative path."""
    validate_wire_path_template(template)
    if not model_id or model_id != model_id.strip():
        raise ValueError("Canonical model ID must be non-empty and trimmed")
    if any(char in model_id for char in ("\r", "\n", "\x00", "?", "#")):
        raise ValueError(
            "Canonical model ID contains a forbidden control or URL character"
        )
    # Model IDs are a single path-template value. Quote slashes and other
    # delimiters so a catalog value cannot escape the configured path shape.
    encoded_model_id = quote(model_id, safe="-._~")
    return template.replace("{model}", encoded_model_id)
