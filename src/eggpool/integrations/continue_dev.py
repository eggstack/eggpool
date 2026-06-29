"""Continue Dev integration renderer."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.integrations.common import IntegrationContext


def _render_yaml_value(value: str) -> str:
    """Render a YAML string value with appropriate quoting."""
    if not value:
        return '""'
    needs_quote = any(c in value for c in ":{}[],#&*?|->!%@`")
    if needs_quote or value.startswith(" ") or value.endswith(" "):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _render_yaml_dict(d: dict[str, str]) -> str:
    """Render a flat dict as YAML key-value pairs."""
    return "\n".join(f"  {k}: {_render_yaml_value(v)}" for k, v in d.items())


def build_continue_yaml_snippet(
    ctx: IntegrationContext, model: str | None = None
) -> str:
    """Build a YAML model block for Continue Dev.

    Rendered manually (no pyyaml dependency). Produces a ``models``
    block compatible with Continue's ``config.yaml``.
    """
    default_model = model or (ctx.models[0]["model_id"] if len(ctx.models) == 1 else "")
    props: dict[str, str] = {
        "title": "EggPool",
        "provider": "openai",
        "model": default_model,
        "apiBase": ctx.base_url,
        "apiKey": ctx.api_key,
    }
    return f"models:\n- {_render_yaml_dict(props)}"
