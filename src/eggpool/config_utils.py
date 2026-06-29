"""Configuration utility functions for CLI and integrations."""

from __future__ import annotations

import os
import secrets
import socket
from dataclasses import dataclass
from typing import Literal, cast

from eggpool.toml_edit import (
    render_toml_string,
    section_has_key,
    update_section_value,
)


def generate_api_key() -> str:
    """Generate a cryptographically secure API key."""
    return f"ep_{secrets.token_hex(32)}"


def load_raw_config(config_path: str) -> dict[str, object]:
    """Load the raw TOML config as a nested dict without Pydantic validation.

    Returns an empty dict if the file is missing or unparseable.
    """
    import tomllib
    from pathlib import Path

    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return cast("dict[str, object]", raw)


def get_section(raw: dict[str, object], name: str) -> dict[str, object]:
    """Return a top-level TOML section as a dict, or empty dict."""
    section = raw.get(name)
    if isinstance(section, dict):
        return cast("dict[str, object]", section)
    return {}


def read_server_api_key(config_path: str) -> str:
    """Read the current server API key from config without full validation."""
    raw = load_raw_config(config_path)
    server = get_section(raw, "server")
    value = server.get("api_key", "")
    return value if isinstance(value, str) else ""


def read_server_port(config_path: str) -> int:
    """Read the server port from config without full validation."""
    from eggpool.constants import DEFAULT_PORT

    raw = load_raw_config(config_path)
    server = get_section(raw, "server")
    value = server.get("port", DEFAULT_PORT)
    return value if isinstance(value, int) else DEFAULT_PORT


def detect_lan_ip() -> str:
    """Detect the LAN IP address of this machine."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


@dataclass(frozen=True)
class ServerKeyResolution:
    """Structured result of server API key resolution."""

    api_key: str
    source: Literal["inline", "env", "generated"]
    env_var: str | None = None
    config_mutated: bool = False


def write_server_api_key(config_path: str, new_key: str) -> tuple[bool, str | None]:
    """Write a server API key to the [server] section of the config.

    Returns (success, warning_or_none). If the [server] section declares
    ``api_key_env`` instead of an inline ``api_key`` line, the directive
    is preserved and no inline key is written.
    """
    from pathlib import Path

    path = Path(config_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    has_api_key_env = section_has_key(lines, "server", "api_key_env")
    result = update_section_value(
        lines,
        "server",
        "api_key",
        render_toml_string(new_key),
        insert_missing_key=not has_api_key_env,
    )

    if not result.section_found:
        return False, "No [server] section found in config. API key was not written."
    if not result.key_found and has_api_key_env:
        return True, (
            "[server] uses api_key_env; rotate the env-var to apply the new key."
        )

    path.write_text("\n".join(result.lines) + "\n", encoding="utf-8")
    return True, None


def resolve_server_api_key(config_path: str) -> ServerKeyResolution:
    """Resolve the effective server API key with full metadata.

    Resolution order:
    1. Inline ``[server].api_key`` — reuse as-is.
    2. ``[server].api_key_env`` with env var present — use env value, no mutation.
    3. ``[server].api_key_env`` with env var absent — raise an error.
    4. Neither inline nor env — generate and persist a new key.
    """
    raw = load_raw_config(config_path)
    server = get_section(raw, "server")

    # Case 1: inline key
    inline_key = server.get("api_key")
    if isinstance(inline_key, str) and inline_key:
        return ServerKeyResolution(api_key=inline_key, source="inline")

    # Case 2/3: api_key_env configured
    env_var = server.get("api_key_env")
    if isinstance(env_var, str) and env_var:
        env_value = os.environ.get(env_var)
        if env_value:
            return ServerKeyResolution(api_key=env_value, source="env", env_var=env_var)
        # Env var configured but not present — abort
        raise SystemExit(
            f"[server].api_key_env is set to {env_var}, but that environment "
            f"variable is not available to this process. Export it before "
            f"running configsetup, or run eggpool newkey to switch to an "
            f"inline key."
        )

    # Case 4: generate and persist
    new_key = generate_api_key()
    success, _warning = write_server_api_key(config_path, new_key)
    if not success:
        raise OSError(
            f"Cannot persist new API key to {config_path}. "
            "Refusing to proceed without a durable key."
        )
    return ServerKeyResolution(api_key=new_key, source="generated", config_mutated=True)
