"""Continue Dev integration renderer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eggpool.integrations.common import render_yaml_string, resolve_optional_model

if TYPE_CHECKING:
    from eggpool.integrations.common import IntegrationContext


def _render_yaml_value(value: str) -> str:
    """Render a YAML string value with appropriate quoting."""
    return render_yaml_string(value)


def build_continue_yaml_snippet(
    ctx: IntegrationContext, model: str | None = None
) -> str:
    """Build a YAML model block for Continue Dev.

    Rendered manually (no pyyaml dependency). Produces a ``models``
    block compatible with Continue's ``config.yaml``.
    """
    default_model = resolve_optional_model(ctx, model)
    props: list[tuple[str, str]] = [
        ("title", "EggPool"),
        ("provider", "openai"),
    ]
    if default_model:
        props.append(("model", default_model))
    props.append(("apiBase", ctx.base_url))
    props.append(("apiKey", ctx.api_key))

    lines = ["models:"]
    for i, (key, value) in enumerate(props):
        if i == 0:
            lines.append(f"  - {key}: {_render_yaml_value(value)}")
        else:
            lines.append(f"    {key}: {_render_yaml_value(value)}")
    return "\n".join(lines)
