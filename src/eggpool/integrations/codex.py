"""Codex integration renderer."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.integrations.common import IntegrationContext


def build_codex_toml_snippet(ctx: IntegrationContext, model: str | None = None) -> str:
    """Build a TOML provider config snippet for Codex.

    Renders a ``[provider.eggpool]`` section with the EggPool endpoint
    and API key, suitable for pasting into a Codex config file.
    """
    lines = [
        "[provider.eggpool]",
        f'base_url = "{ctx.base_url}"',
        f'api_key = "{ctx.api_key}"',
    ]
    if model:
        lines.append(f'default_model = "{model}"')
    if ctx.models:
        lines.append("")
        for m in ctx.models:
            mid = m["model_id"]
            lines.append(f"[provider.eggpool.models.{mid}]")
            limits = m.get("effective_limits", {})
            ctx_tokens = limits.get("context_tokens")
            if ctx_tokens is not None and ctx_tokens > 0:
                lines.append(f"context_window = {ctx_tokens}")
    return "\n".join(lines)


def detect_codex_version() -> str | None:
    """Detect the installed Codex CLI version.

    Returns the version string or None if Codex is not installed.
    """
    binary = shutil.which("codex")
    if binary is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        version = result.stdout.strip()
        return version if version else None
    except (subprocess.SubprocessError, OSError):
        return None
