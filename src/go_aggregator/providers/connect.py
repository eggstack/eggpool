"""Interactive provider connection and configuration management."""

from __future__ import annotations

import os
import signal
import sys
import termios
import tomllib
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ConfiguredAccount:
    """Configured provider account with optional resolved API key."""

    provider_id: str
    name: str
    api_key_env: str
    api_key: str | None

    @property
    def label(self) -> str:
        """Human-readable label for terminal selection."""
        masked = _mask_secret(self.api_key) if self.api_key else "unset"
        return f"{self.provider_id}/{self.name}  {self.api_key_env}={masked}"


def load_provider_templates(providers_path: str) -> dict[str, dict[str, Any]]:
    """Load provider templates from a TOML file.

    Returns a dict mapping provider_id to a dict with keys:
    - display: human-readable display name (from TOML comment or id)
    - url: base URL
    - raw: raw TOML text of the [providers.<id>] block
    - data: parsed dict of the provider config (excluding display metadata)
    """
    path = Path(providers_path)
    if not path.exists():
        return {}

    text = path.read_text(encoding="utf-8")
    parsed: dict[str, Any] = tomllib.loads(text)

    providers_raw: dict[str, Any] = parsed.get("providers", {})
    templates: dict[str, dict[str, Any]] = {}

    for provider_id in providers_raw:
        provider_data_raw = providers_raw[provider_id]
        if not isinstance(provider_data_raw, dict):
            continue

        provider_data: dict[str, Any] = dict(provider_data_raw)  # type: ignore[reportUnknownArgumentType]
        raw_display: Any = provider_data.pop("_display", None)
        display_name: str = str(raw_display) if raw_display is not None else provider_id
        raw_url: Any = provider_data.get("base_url", "")
        base_url: str = str(raw_url) if raw_url is not None else ""
        provider_data["id"] = provider_id

        # Extract the raw TOML block for this provider from the file
        raw_block = _extract_raw_block(text, provider_id)

        templates[provider_id] = {
            "display": display_name,
            "url": base_url,
            "raw": raw_block,
            "data": provider_data,
        }

    return templates


def _extract_raw_block(text: str, provider_id: str) -> str:
    """Extract the raw TOML text for a [providers.<id>] block."""
    lines = text.split("\n")
    block_lines: list[str] = []
    in_block = False
    header = f"[providers.{provider_id}]"

    for line in lines:
        stripped = line.strip()
        if stripped == header:
            in_block = True
            block_lines = [line]
            continue
        if in_block:
            if stripped.startswith("[") and stripped.endswith("]"):
                break
            block_lines.append(line)

    return "\n".join(block_lines)


def configured_accounts(config_path: str) -> list[ConfiguredAccount]:
    """Return configured accounts in display order."""
    from go_aggregator.models.config import AppConfig

    config = AppConfig.from_toml(config_path)
    accounts: list[ConfiguredAccount] = []
    for provider_id, provider in config.providers.items():
        for account in provider.accounts:
            api_key = os.environ.get(account.api_key_env)
            accounts.append(
                ConfiguredAccount(
                    provider_id=provider_id,
                    name=account.name,
                    api_key_env=account.api_key_env,
                    api_key=api_key,
                )
            )
    return accounts


def matching_logout_accounts(
    config_path: str,
    target: str,
) -> list[ConfiguredAccount]:
    """Find accounts matching a provider id, account name, env var, or API key."""
    normalized_target = _normalize_identifier(target)
    accounts = configured_accounts(config_path)
    matches: list[ConfiguredAccount] = []

    for account in accounts:
        if account.api_key == target:
            matches.append(account)
            continue
        if account.api_key_env == target:
            matches.append(account)
            continue
        if account.name == target:
            matches.append(account)
            continue
        if account.provider_id == target:
            matches.append(account)
            continue
        if _normalize_identifier(account.provider_id) == normalized_target:
            matches.append(account)

    return matches


def _normalize_identifier(value: str) -> str:
    """Normalize provider ids for forgiving CLI matching."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _mask_secret(value: str | None) -> str:
    """Mask a secret for terminal display."""
    if not value:
        return "unset"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


class TerminalMenu:
    """Simple terminal menu with j/k or arrow key navigation."""

    def __init__(self, title: str, options: list[str]) -> None:
        self.title = title
        self.options = options
        self.selected = 0

    def display(self) -> None:
        """Render the menu."""
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.write(f"\033[1m{self.title}\033[0m\n\n")
        sys.stdout.write(
            "  Use \033[1mj/k\033[0m or \033[1m↑/↓\033[0m to navigate, "
            "\033[1mEnter\033[0m to select, \033[1mq\033[0m to quit\n\n"
        )

        for i, option in enumerate(self.options):
            prefix = "  → " if i == self.selected else "    "
            color = "\033[1;32m" if i == self.selected else ""
            reset = "\033[0m" if i == self.selected else ""
            sys.stdout.write(f"{prefix}{color}{option}{reset}\n")

        sys.stdout.write("\n")
        sys.stdout.flush()

    def run(self) -> str | None:
        """Run the interactive menu. Returns selected option or None if quit."""
        fd = sys.stdin.fileno()
        old_settings = None
        try:
            old_settings = termios.tcgetattr(fd)
            tty.setraw(fd)

            while True:
                self.display()
                ch = sys.stdin.read(1)

                if ch == "q" or ch == "Q":
                    return None
                if ch == "\r" or ch == "\n":
                    return self.options[self.selected]
                if ch == "\x1b":
                    next_ch = sys.stdin.read(1)
                    if next_ch == "[":
                        arrow = sys.stdin.read(1)
                        if arrow == "A":
                            self.selected = max(0, self.selected - 1)
                        elif arrow == "B":
                            self.selected = min(
                                len(self.options) - 1, self.selected + 1
                            )
                elif ch == "j":
                    self.selected = min(len(self.options) - 1, self.selected + 1)
                elif ch == "k":
                    self.selected = max(0, self.selected - 1)
        finally:
            if old_settings is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def collect_api_key(provider_name: str) -> str:
    """Prompt user for an API key."""
    sys.stdout.write(f"\n  Enter API key for {provider_name}: ")
    sys.stdout.flush()

    fd = sys.stdin.fileno()
    old_settings = None
    key_chars: list[str] = []
    try:
        old_settings = termios.tcgetattr(fd)
        tty.setraw(fd)

        while True:
            ch = sys.stdin.read(1)
            if ch == "\r" or ch == "\n":
                sys.stdout.write("\n")
                sys.stdout.flush()
                break
            if ch == "\x7f" or ch == "\x08":
                if key_chars:
                    key_chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif ch == "\x03":
                raise KeyboardInterrupt
            elif ch == "\x1b":
                sys.stdin.read(2)
            else:
                key_chars.append(ch)
                sys.stdout.write("*")
                sys.stdout.flush()
    finally:
        if old_settings is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return "".join(key_chars).strip()


def merge_provider_into_config(
    config_path: str,
    provider_data: dict[str, Any],
    api_key_env: str,
) -> bool:
    """Add a provider and account to the config TOML file.

    If the provider already exists, appends a new account.
    If not, inserts the full provider block.

    Returns True if the config was modified.
    """
    path = Path(config_path)
    if not path.exists():
        return False

    content = path.read_text(encoding="utf-8")
    provider_id = provider_data.get("id", "unknown")

    # Check if provider already exists
    existing_accounts = _get_existing_accounts(content, provider_id)
    if existing_accounts is not None and len(existing_accounts) > 0:
        # Append new account to existing provider
        account_name = _unique_account_name(provider_id, existing_accounts)
        env_name = _unique_env_name(provider_id, existing_accounts)
        content = _append_account(content, provider_id, account_name, env_name)
        # Update the api_key_env reference
        api_key_env = env_name
    else:
        # Insert new provider block
        block = _format_provider_block(provider_id, provider_data, api_key_env)
        content = _insert_provider_block(content, block)

    path.write_text(content, encoding="utf-8")
    return True


def _get_existing_accounts(content: str, provider_id: str) -> list[str] | None:
    """Get existing account names for a provider. Returns None if not found."""
    # TOML array-of-tables uses [[providers.X.accounts]] syntax
    header_single = f"[providers.{provider_id}.accounts"
    header_double = f"[[providers.{provider_id}.accounts"
    has_section = header_single in content or header_double in content
    if not has_section:
        return None

    accounts: list[str] = []
    in_section = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith(header_double) or stripped.startswith(header_single):
            in_section = True
            continue
        if in_section and stripped.startswith("[") and stripped.endswith("]"):
            in_section = False
            continue
        if in_section and stripped.startswith("name"):
            parts = stripped.split("=", 1)
            if len(parts) == 2:
                name = parts[1].strip().strip('"').strip("'")
                accounts.append(name)

    return accounts


def _unique_account_name(provider_id: str, existing: list[str]) -> str:
    """Generate a unique account name for a provider."""
    base = "default"
    if base not in existing:
        return base
    counter = 2
    while f"{base}-{counter}" in existing:
        counter += 1
    return f"{base}-{counter}"


def _unique_env_name(provider_id: str, existing_accounts: list[str]) -> str:
    """Generate a unique environment variable name based on provider id."""
    env_name = _provider_id_to_env_name(provider_id)
    if not existing_accounts:
        return env_name
    # Check if base env name is used by existing accounts
    # Since we can't easily check which env names are in use,
    # we always append a number for the second+ account
    return f"{env_name}_{len(existing_accounts) + 1}"


def _provider_id_to_env_name(provider_id: str) -> str:
    """Convert a provider ID to an environment variable name."""
    return provider_id.upper().replace("-", "_") + "_API_KEY"


def _format_provider_block(
    provider_id: str,
    data: dict[str, Any],
    api_key_env: str,
) -> str:
    """Format a provider config block as TOML text."""
    lines = [f"[providers.{provider_id}]"]
    for key, value in data.items():
        if key == "id" or key == "accounts":
            continue
        lines.append(f"{key} = {_toml_value(value)}")

    # Add account
    lines.append("")
    lines.append(f"[[providers.{provider_id}.accounts]]")
    lines.append('name = "default"')
    lines.append(f'api_key_env = "{api_key_env}"')

    return "\n".join(lines)


def _toml_value(value: Any) -> str:  # noqa: ANN401
    """Format a Python value as a TOML value string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        # pyright can't track types through map() on Any
        result = (
            "["
            + ", ".join(f'"{item}"' for item in map(str, value))  # type: ignore[arg-type]
            + "]"
        )
        return result
    return f'"{value}"'


def _insert_provider_block(content: str, block: str) -> str:
    """Insert a new provider block into the config content."""
    # Find the first [providers.*] section
    lines = content.split("\n")
    insert_idx = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[providers.") and stripped.endswith("]"):
            insert_idx = i
            break

    block_lines = block.split("\n")
    new_lines = lines[:insert_idx] + block_lines + [""] + lines[insert_idx:]
    return "\n".join(new_lines)


def _append_account(
    content: str,
    provider_id: str,
    account_name: str,
    env_name: str,
) -> str:
    """Append an account entry to an existing provider section."""
    lines = content.split("\n")
    # Find the end of the provider's accounts section
    insert_idx = len(lines)
    in_section = False
    header_single = f"[providers.{provider_id}.accounts"
    header_double = f"[[providers.{provider_id}.accounts"

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(header_double) or stripped.startswith(header_single):
            in_section = True
            continue
        if in_section and stripped.startswith("[") and stripped.endswith("]"):
            insert_idx = i
            in_section = False
            break

    # If we exited the loop still inside the section, append at end
    if in_section:
        insert_idx = len(lines)

    account_lines = [
        "",
        f"[[providers.{provider_id}.accounts]]",
        f'name = "{account_name}"',
        f'api_key_env = "{env_name}"',
    ]

    new_lines = lines[:insert_idx] + account_lines + lines[insert_idx:]
    return "\n".join(new_lines)


def remove_account_from_config(
    config_path: str,
    account: ConfiguredAccount,
) -> bool:
    """Remove an account from the config TOML file.

    If the account is the provider's final account, remove the provider section.
    Returns True if the config was modified.
    """
    path = Path(config_path)
    if not path.exists():
        return False

    content = path.read_text(encoding="utf-8")
    updated, removed = _remove_account_block(
        content,
        provider_id=account.provider_id,
        account_name=account.name,
        api_key_env=account.api_key_env,
    )
    if not removed:
        return False

    if _provider_account_count(updated, account.provider_id) == 0:
        updated = _remove_provider_block(updated, account.provider_id)

    path.write_text(updated, encoding="utf-8")
    return True


def _remove_account_block(
    content: str,
    provider_id: str,
    account_name: str,
    api_key_env: str,
) -> tuple[str, bool]:
    """Remove a single [[providers.<id>.accounts]] block."""
    lines = content.split("\n")
    header = f"[[providers.{provider_id}.accounts]]"
    output: list[str] = []
    i = 0
    removed = False

    while i < len(lines):
        if lines[i].strip() != header:
            output.append(lines[i])
            i += 1
            continue

        block_start = i
        block_end = i + 1
        while block_end < len(lines):
            stripped = lines[block_end].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                break
            block_end += 1

        block = lines[block_start:block_end]
        if (
            _block_value(block, "name") == account_name
            and _block_value(block, "api_key_env") == api_key_env
        ):
            removed = True
            if output and output[-1] == "":
                output.pop()
            i = block_end
            if i < len(lines) and lines[i] == "":
                i += 1
            continue

        output.extend(block)
        i = block_end

    return "\n".join(output), removed


def _block_value(lines: list[str], key: str) -> str | None:
    """Return a simple scalar TOML value from a block."""
    prefix = f"{key} ="
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        value = stripped.split("=", 1)[1].strip()
        return value.strip('"').strip("'")
    return None


def _provider_account_count(content: str, provider_id: str) -> int:
    """Count account tables for a provider."""
    header = f"[[providers.{provider_id}.accounts]]"
    return sum(1 for line in content.split("\n") if line.strip() == header)


def _remove_provider_block(content: str, provider_id: str) -> str:
    """Remove a [providers.<id>] section and its child tables."""
    lines = content.split("\n")
    header = f"[providers.{provider_id}]"
    output: list[str] = []
    i = 0

    while i < len(lines):
        if lines[i].strip() != header:
            output.append(lines[i])
            i += 1
            continue

        if output and output[-1] == "":
            output.pop()
        i += 1
        child_prefix = f"[providers.{provider_id}."
        child_array_prefix = f"[[providers.{provider_id}."
        while i < len(lines):
            stripped = lines[i].strip()
            is_section = stripped.startswith("[") and stripped.endswith("]")
            if is_section and not (
                stripped.startswith(child_prefix)
                or stripped.startswith(child_array_prefix)
            ):
                break
            i += 1
        if i < len(lines) and lines[i] == "":
            i += 1

    return "\n".join(output)


def find_shell_profile() -> Path | None:
    """Detect the user's shell profile file."""
    home = Path.home()
    shell = os.environ.get("SHELL", "")

    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        bash_profile = home / ".bash_profile"
        if bash_profile.exists():
            return bash_profile
        return home / ".bashrc"
    if "fish" in shell:
        return home / ".config" / "fish" / "config.fish"

    # Fallback
    for name in (".profile", ".bash_profile", ".bashrc", ".zshrc"):
        p = home / name
        if p.exists():
            return p

    return home / ".profile"


def export_env_var(env_name: str, value: str) -> Path | None:
    """Write an export statement to the shell profile. Returns the profile path."""
    profile = find_shell_profile()
    if profile is None:
        return None

    profile.parent.mkdir(parents=True, exist_ok=True)

    # Check if already exists
    if profile.exists():
        existing = profile.read_text(encoding="utf-8")
        if f"export {env_name}=" in existing:
            # Replace existing value
            file_lines = existing.split("\n")
            new_lines: list[str] = []
            for line in file_lines:
                if line.strip().startswith(f"export {env_name}="):
                    new_lines.append(f"export {env_name}={value}")
                else:
                    new_lines.append(line)
            profile.write_text("\n".join(new_lines), encoding="utf-8")
            return profile

    # Append
    with profile.open("a", encoding="utf-8") as f:
        f.write("\n# Added by gorouter connect\n")
        f.write(f"export {env_name}={value}\n")

    return profile


def send_reload_signal(config_path: str) -> bool:
    """Send SIGHUP to the running gorouter process to trigger config reload.

    Returns True if signal was sent successfully.
    """
    pid_file = Path(config_path).parent / ".gorouter.pid"
    if not pid_file.exists():
        return False

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGHUP)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def connect(
    config_path: str,
    providers_path: str = "providers.toml",
) -> bool:
    """Run the interactive provider connection flow.

    Returns True if a provider was successfully connected.
    """
    templates = load_provider_templates(providers_path)
    if not templates:
        sys.stdout.write(f"  No provider templates found in {providers_path}\n")
        return False

    # Build display options
    options: list[str] = []
    provider_ids: list[str] = []
    for pid, tmpl in templates.items():
        options.append(f"{tmpl['display']}  ({tmpl['url']})")
        provider_ids.append(pid)

    # Show interactive selector
    menu = TerminalMenu("Select a provider to connect:", options)
    result = menu.run()

    if result is None:
        sys.stdout.write("  Cancelled.\n")
        return False

    idx = options.index(result)
    provider_id = provider_ids[idx]
    tmpl = templates[provider_id]

    # Determine env var name
    from go_aggregator.models.config import AppConfig

    try:
        config = AppConfig.from_toml(config_path)
    except Exception:
        config = None

    existing_accounts: list[str] = []
    if config is not None and provider_id in config.providers:
        existing_accounts = [a.name for a in config.providers[provider_id].accounts]

    env_name = _unique_env_name(provider_id, existing_accounts)
    account_name = _unique_account_name(provider_id, existing_accounts)

    # Prompt for API key
    sys.stdout.write(f"\n  Provider: {tmpl['display']}\n")
    api_key = collect_api_key(tmpl["display"])

    if not api_key:
        sys.stdout.write("  No API key provided. Aborted.\n")
        return False

    # Export to shell profile
    profile = export_env_var(env_name, api_key)
    if profile:
        sys.stdout.write(f"  Exported {env_name} to {profile}\n")
    else:
        sys.stdout.write(
            f"  Could not detect shell profile. Set manually: export {env_name}=<key>\n"
        )

    # Merge into config
    provider_data = tmpl["data"].copy()
    provider_data["id"] = provider_id
    ok = merge_provider_into_config(config_path, provider_data, env_name)

    if not ok:
        sys.stdout.write(f"  Failed to update config at {config_path}\n")
        return False

    sys.stdout.write(f"  Added {account_name} to {provider_id} in {config_path}\n")

    # Attempt to signal running server for reload
    if send_reload_signal(config_path):
        sys.stdout.write("  Sent reload signal to running server.\n")
    else:
        sys.stdout.write(
            "  No running server detected. Changes take effect on next start.\n"
        )

    sys.stdout.write(
        f"\n  Run `source {profile or '~/.profile'}` to load the env var, "
        f"then restart or let the server reload.\n"
    )

    return True
