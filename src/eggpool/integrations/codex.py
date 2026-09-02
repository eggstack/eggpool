"""Codex integration renderer.

Plan 143 rewrites this module to match the current Codex CLI schema:

* Codex selects providers through the ``[model_providers.<id>]`` table,
  not the legacy ``[provider.<id>]`` block.
* The wire API is selected with ``wire_api = "responses"``; EggPool's
  ``POST /v1/responses`` is the supported stateless client surface. The
  selected provider may use another registered wire profile through the
  canonical codec boundary.
* API keys are referenced through ``env_key`` rather than embedded
  directly in the generated TOML. Operators who already have
  ``EGGPOOL_API_KEY`` exported do not need to edit the snippet.
* The chosen model is set via the top-level ``model`` /
  ``model_provider`` keys, not a provider-local ``default_model``.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

from eggpool.integrations.common import render_toml_string

if TYPE_CHECKING:
    from eggpool.integrations.common import IntegrationContext


CODEX_PROVIDER_NAME = "eggpool"
CODEX_PROVIDER_LABEL = "EggPool"
CODEX_ENV_KEY = "EGGPOOL_API_KEY"
CODEX_WIRE_API = "responses"


def build_codex_toml_snippet(ctx: IntegrationContext, model: str | None = None) -> str:
    """Build a TOML provider config snippet for current Codex.

    Generates a ``[model_providers.eggpool]`` block plus the
    top-level ``model`` / ``model_provider`` selection keys. The
    EggPool server key is referenced via ``env_key = "EGGPOOL_API_KEY"``
    so the operator controls when the secret is exposed — no
    plaintext key is embedded in the snippet.
    """
    lines = [
        f"model_provider = {render_toml_string(CODEX_PROVIDER_NAME)}",
    ]
    if model:
        lines.append(f"model = {render_toml_string(model)}")
    lines.extend(
        [
            "",
            f"[model_providers.{CODEX_PROVIDER_NAME}]",
            f"name = {render_toml_string(CODEX_PROVIDER_LABEL)}",
            f"base_url = {render_toml_string(ctx.base_url)}",
            f"wire_api = {render_toml_string(CODEX_WIRE_API)}",
            f"env_key = {render_toml_string(CODEX_ENV_KEY)}",
        ]
    )
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
