"""CLI entry point for the aggregator."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

import click

from eggpool.accounts.registry import account_config_rows
from eggpool.auth import require_auth_at_startup
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import AccountRepository, ProviderRepository
from eggpool.errors import AggregatorError
from eggpool.logging import configure_logging
from eggpool.models.config import AppConfig
from eggpool.providers.client_pool import ProviderClientPool


@click.group(invoke_without_command=True)
@click.option(
    "--config",
    "config_path",
    default="config.toml",
    help="Path to the TOML configuration file.",
    type=click.Path(),
)
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """EggPool - aggregate OpenCode Go subscriptions."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = os.path.abspath(config_path)


class _ConfigPathGroup(click.Group):
    """Click group that shows the resolved config path in help output."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        super().format_help(ctx, formatter)
        # Parse --config from the original command line args
        config_path = "config.toml"
        args = sys.argv[1:] if hasattr(sys, "argv") else []
        for i, arg in enumerate(args):
            if arg == "--config" and i + 1 < len(args):
                config_path = args[i + 1]
                break
            elif arg.startswith("--config="):
                config_path = arg.split("=", 1)[1]
                break
        resolved = os.path.abspath(config_path)
        formatter.write(f"\nConfig file: {resolved}\n")


# Re-apply the group class after the decorator
cli.__class__ = _ConfigPathGroup


@cli.command()
@click.pass_context
def serve(ctx: click.Context) -> None:
    """Start the aggregation proxy server."""
    from granian import Granian  # type: ignore[import-untyped]

    config_path: str = ctx.obj["config_path"]

    try:
        config = AppConfig.from_toml(config_path)
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not config.all_accounts():
        click.echo(
            "Warning: No provider accounts configured.\n"
            "  Use `eggpool connect` to add a provider, then restart.",
            err=True,
        )

    try:
        config.validate_account_credentials()
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    configure_logging(level=config.server.log_level)

    from functools import partial

    from eggpool.app import create_app

    app = create_app(config, config_path=config_path)

    # Granian requires a string target but we need the pre-built app.
    # Use target_loader to inject our app, bypassing string resolution.
    def _app_loader(_target: str) -> object:  # noqa: ARG001
        return app

    log_level = config.server.log_level.lower()
    Granian(
        "unused",  # ignored when target_loader is provided
        address=config.server.host,
        port=config.server.port,
        interface="asgi",  # type: ignore[reportArgumentType]
        workers=1,
        log_level=log_level,  # type: ignore[reportArgumentType]
        log_access=config.server.access_log,
    ).serve(target_loader=partial(_app_loader))  # type: ignore[reportArgumentType]


@cli.group(invoke_without_command=True)
@click.option(
    "--providers",
    "providers_path",
    default=None,
    help="Path to the providers template file. Uses bundled template if not specified.",
    type=click.Path(),
)
@click.pass_context
def connect(ctx: click.Context, providers_path: str | None) -> None:
    """Connect to a new provider interactively."""
    if ctx.invoked_subcommand is not None:
        ctx.obj["providers_path"] = providers_path
        return

    from eggpool.providers.connect import connect as do_connect

    config_path: str = ctx.obj["config_path"]
    try:
        ok = do_connect(config_path, providers_path)
    except KeyboardInterrupt:
        return
    if not ok:
        sys.exit(1)


@connect.command("list")
@click.pass_context
def connect_list(ctx: click.Context) -> None:
    """List providers available for connection."""
    from eggpool.providers.connect import load_provider_templates

    providers_path: str | None = ctx.obj.get("providers_path")
    templates = load_provider_templates(providers_path)
    if not templates:
        click.echo("No provider templates found")
        return

    click.echo("Available providers:")
    for provider_id, tmpl in templates.items():
        click.echo(f"  {provider_id}: {tmpl['display']} ({tmpl['url']})")


@cli.command()
@click.argument("target", required=False)
@click.pass_context
def logout(ctx: click.Context, target: str | None) -> None:
    """Remove a configured provider account.

    If TARGET is given, matches by provider id, account name, env var,
    or API key.  If omitted, shows an interactive selection menu.
    """
    from eggpool.providers.connect import (
        TerminalMenu,
        matching_logout_accounts,
        remove_account_from_config,
        select_config_account,
        signal_reload,
    )

    config_path: str = ctx.obj["config_path"]

    if target:
        try:
            matches = matching_logout_accounts(config_path, target)
        except AggregatorError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)

        if not matches:
            click.echo(f"No configured provider or API key found for {target!r}.")
            return

        account = matches[0]
        if len(matches) > 1:
            menu = TerminalMenu(
                f"Select provider account to remove for {target}:",
                [match.label for match in matches],
            )
            try:
                selected = menu.run()
            except KeyboardInterrupt:
                return
            if selected is None:
                return
            selected_index = [match.label for match in matches].index(selected)
            account = matches[selected_index]
    else:
        account = select_config_account(
            config_path, "Select provider account to remove:"
        )
        if account is None:
            return

    if not remove_account_from_config(config_path, account):
        click.echo(
            f"Could not remove {account.provider_id}/{account.name} "
            f"from {config_path}.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"Removed {account.provider_id}/{account.name} from {config_path}.")

    if signal_reload():
        click.echo("  Configuration reloaded.")
    else:
        click.echo("  Server is not running.")


@cli.command("check-config")
@click.pass_context
def check_config(ctx: click.Context) -> None:
    """Validate the configuration file."""
    config_path: str = ctx.obj["config_path"]

    try:
        config = AppConfig.from_toml(config_path)
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    try:
        require_auth_at_startup(config.server.resolved_api_key)
    except RuntimeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    try:
        config.validate_account_credentials()
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Configuration loaded successfully from {config_path}")
    click.echo(f"  Server: {config.server.host}:{config.server.port}")
    click.echo(f"  Accounts: {len(config.all_accounts())}")
    click.echo(f"  Database: {config.database.path}")


@cli.command()
@click.pass_context
def edit(ctx: click.Context) -> None:
    """Open the configuration file in the default editor."""
    import shutil

    config_path: str = ctx.obj["config_path"]
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        for name in ("hx", "vim", "vi", "nano"):
            if shutil.which(name):
                editor = name
                break
    if editor:
        os.execvp(editor, [editor, config_path])
    else:
        click.echo("No editor found. Set $EDITOR or install vim/helix.", err=True)
        sys.exit(1)


def _generate_api_key() -> str:
    """Generate a cryptographically secure API key."""
    import secrets

    return f"ep_{secrets.token_hex(32)}"


def _read_server_api_key(config_path: str) -> str:
    """Read the current server API key from config without full validation."""
    import tomllib
    from pathlib import Path

    path = Path(config_path)
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    server = raw.get("server", {})
    return server.get("api_key", "") or ""


def _read_server_port(config_path: str) -> int:
    """Read the server port from config without full validation."""
    import tomllib
    from pathlib import Path

    from eggpool.constants import DEFAULT_PORT

    path = Path(config_path)
    if not path.exists():
        return DEFAULT_PORT
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    server = raw.get("server", {})
    return server.get("port", DEFAULT_PORT)


def _detect_lan_ip() -> str:
    """Detect the LAN IP address of this machine."""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _write_server_api_key(config_path: str, new_key: str) -> None:
    """Write a server API key to the [server] section of the config."""
    from pathlib import Path

    path = Path(config_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []
    in_server = False
    found = False

    for line in lines:
        stripped = line.strip()
        if stripped == "[server]":
            in_server = True
            new_lines.append(line)
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_server = False
        if in_server and stripped.startswith("api_key"):
            new_lines.append(f'api_key = "{new_key}"')
            found = True
            continue
        new_lines.append(line)

    if not found:
        # Insert after [server] header
        for i, line in enumerate(new_lines):
            if line.strip() == "[server]":
                new_lines.insert(i + 1, f'api_key = "{new_key}"')
                break

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


@cli.command()
@click.pass_context
def getkey(ctx: click.Context) -> None:
    """Print the current server API key."""
    config_path: str = ctx.obj["config_path"]
    key = _read_server_api_key(config_path)
    if key:
        click.echo(key, nl=False)
    else:
        click.echo(
            "No API key configured. Run `eggpool newkey` to generate one.", err=True
        )
        sys.exit(1)


@cli.command()
@click.pass_context
def newkey(ctx: click.Context) -> None:
    """Generate a new server API key, overwriting the old one."""
    from eggpool.providers.connect import signal_reload, signal_restart

    config_path: str = ctx.obj["config_path"]
    old_key = _read_server_api_key(config_path)
    new_key = _generate_api_key()
    _write_server_api_key(config_path, new_key)

    if old_key:
        click.echo(f"Old key (expired): {old_key}")
    click.echo(f"New key (use this): {new_key}")

    if signal_reload():
        click.echo("Configuration reloaded.")
    elif signal_restart():
        click.echo("Server restarted.")
    else:
        click.echo("Server is not running. Start it to apply the new key.")


@cli.group()
@click.pass_context
def configsetup(ctx: click.Context) -> None:
    """Print configuration snippets for code editors."""


def _copy_to_clipboard(text: str) -> bool:
    """Try to copy text to the system clipboard. Returns True on success."""
    import shutil
    import subprocess

    for cmd in (
        ["pbcopy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True, timeout=5)  # noqa: S603
                return True
            except (subprocess.SubprocessError, OSError):
                continue
    return False


@configsetup.command("opencode")
@click.option(
    "--json-only",
    is_flag=True,
    default=False,
    help="Print only JSON to stdout (no status messages).",
)
@click.pass_context
def configsetup_opencode(ctx: click.Context, json_only: bool) -> None:
    """Print OpenCode config for connecting to this router."""
    import json as _json

    from eggpool.catalog.limits import ModelLimitResolver
    from eggpool.integrations.opencode import build_opencode_config_json
    from eggpool.models.config import AppConfig

    config_path: str = ctx.obj["config_path"]

    # Auto-generate API key if not present
    key = _read_server_api_key(config_path)
    if not key:
        key = _generate_api_key()
        _write_server_api_key(config_path, key)
        click.echo("Generated new server API key.", err=True)

    port = _read_server_port(config_path)
    lan_ip = _detect_lan_ip()
    base_url = f"http://{lan_ip}:{port}/v1"

    # Try to load catalog from the database
    models_data: list[dict[str, Any]] = []
    try:
        import asyncio

        from eggpool.db.connection import Database

        config = AppConfig.from_toml(config_path)
        db_path = config.database.path

        async def _load_catalog() -> list[dict[str, Any]]:
            db = Database(db_path)
            await db.connect()
            try:
                rows = await db.fetch_all(
                    "SELECT model_id, display_name, capabilities, source_metadata "
                    "FROM models"
                )
                result: list[dict[str, Any]] = []
                for row in rows:
                    caps_raw = row["capabilities"]
                    meta_raw = row["source_metadata"]
                    caps: dict[str, Any] = _json.loads(caps_raw) if caps_raw else {}
                    meta: dict[str, Any] = _json.loads(meta_raw) if meta_raw else {}
                    result.append(
                        {
                            "model_id": row["model_id"],
                            "display_name": row["display_name"],
                            "capabilities": caps,
                            "source_metadata": meta,
                            "effective_limits": {},
                        }
                    )
                return result
            finally:
                await db.disconnect()

        models_data = asyncio.run(_load_catalog())

        # Re-apply current config overrides
        if models_data:
            resolver = ModelLimitResolver(config)
            for m in models_data:
                eff = resolver.resolve(
                    provider_id="opencode-go",
                    model_id=m["model_id"],
                    capabilities=m.get("capabilities", {}),
                    source_metadata=m.get("source_metadata", {}),
                )
                m["effective_limits"] = {
                    "context_tokens": eff.context_tokens,
                    "input_tokens": eff.input_tokens,
                    "output_tokens": eff.output_tokens,
                    "enforce": eff.enforce,
                    "context_source": eff.context_source,
                    "input_source": eff.input_source,
                    "output_source": eff.output_source,
                }
    except Exception:
        if not json_only:
            click.echo(
                "Warning: Could not load catalog. Run 'eggpool models refresh' "
                "or start the server to populate the catalog before generating "
                "model-specific limits.",
                err=True,
            )

    config_json = build_opencode_config_json(
        base_url=base_url,
        api_key=key,
        models=models_data,
    )

    click.echo(config_json)

    if not json_only:
        click.echo("", err=True)
        if models_data:
            click.echo(f"Generated config with {len(models_data)} models.", err=True)
        else:
            click.echo(
                "Generated provider connection block (no model limits). "
                "Run 'eggpool models refresh' to populate model metadata.",
                err=True,
            )
        click.echo("Add to ~/.config/opencode/opencode.json:", err=True)

        if _copy_to_clipboard(config_json):
            click.echo("Copied to clipboard.", err=True)


@configsetup.command("claude-code")
@click.pass_context
def configsetup_claude_code(ctx: click.Context) -> None:
    """Print Claude Code config snippet for connecting to this router."""
    config_path: str = ctx.obj["config_path"]

    # Auto-generate API key if not present
    key = _read_server_api_key(config_path)
    if not key:
        key = _generate_api_key()
        _write_server_api_key(config_path, key)
        click.echo("Generated new server API key.", err=True)

    port = _read_server_port(config_path)
    lan_ip = _detect_lan_ip()

    snippet = (
        '{\n  "api_key": "'
        + key
        + '",\n  "base_url": "http://'
        + lan_ip
        + ":"
        + str(port)
        + '/v1"\n}'
    )

    click.echo("Add to ~/.claude/settings.json or pass via --api-key and --base-url:")
    click.echo("")
    click.echo(snippet)
    click.echo("")
    click.echo(f"Or run: claude --api-key {key} --base-url http://{lan_ip}:{port}/v1")

    if _copy_to_clipboard(snippet):
        click.echo("Copied to clipboard.")


@cli.group()
@click.pass_context
def deploy(ctx: click.Context) -> None:
    """Print deployment snippets (systemd, logrotate, cron, ...)."""


def _print_deploy_snippet(
    title: str,
    target_path: str,
    snippet: str,
    extra_steps: list[str],
) -> None:
    """Print a deployment snippet with installation instructions."""
    click.echo(title)
    click.echo("")
    click.echo(f"Install to {target_path}:")
    click.echo("")
    click.echo(f"  sudo tee {target_path} > /dev/null << 'EGGPOOL_EOF'")
    for line in snippet.splitlines():
        click.echo(f"{line}")
    click.echo("EGGPOOL_EOF")
    click.echo("")
    click.echo("Then run:")
    click.echo("")
    for step in extra_steps:
        click.echo(f"  {step}")
    click.echo("")
    click.echo("Snippet:")
    click.echo("")
    click.echo(snippet)

    if _copy_to_clipboard(snippet):
        click.echo("")
        click.echo("Copied to clipboard.")


@deploy.command("systemd")
@click.pass_context
def deploy_systemd(ctx: click.Context) -> None:
    """Print the systemd unit and install instructions."""
    from eggpool.deploy import SYSTEMD_UNIT

    _print_deploy_snippet(
        title="EggPool systemd unit",
        target_path="/etc/systemd/system/eggpool.service",
        snippet=SYSTEMD_UNIT,
        extra_steps=[
            "sudo systemctl daemon-reload",
            "sudo systemctl enable eggpool",
            "sudo systemctl start eggpool",
            "sudo systemctl status eggpool",
        ],
    )


@deploy.command("logrotate")
@click.pass_context
def deploy_logrotate(ctx: click.Context) -> None:
    """Print the logrotate config and install instructions."""
    from eggpool.deploy import LOGROTATE_CONF

    _print_deploy_snippet(
        title="EggPool logrotate configuration",
        target_path="/etc/logrotate.d/eggpool",
        snippet=LOGROTATE_CONF,
        extra_steps=[
            "sudo logrotate -d /etc/logrotate.d/eggpool",
        ],
    )


@deploy.command("cron")
@click.pass_context
def deploy_cron(ctx: click.Context) -> None:
    """Print a daily backup cron entry and install instructions."""
    from eggpool.deploy import CRON_BACKUP_FILE, CRON_BACKUP_SCRIPT

    click.echo("EggPool automated backup via cron")
    click.echo("")
    click.echo(
        "This sets up a daily 02:00 backup of the configuration, environment, "
        "and SQLite database under /var/backups/eggpool."
    )
    click.echo("")
    click.echo("Install the backup script:")
    click.echo("")
    click.echo("  sudo tee /usr/local/bin/eggpool-backup > /dev/null << 'EGGPOOL_EOF'")
    for line in CRON_BACKUP_SCRIPT.splitlines():
        click.echo(f"{line}")
    click.echo("EGGPOOL_EOF")
    click.echo("  sudo chmod +x /usr/local/bin/eggpool-backup")
    click.echo("")
    click.echo("Install the cron entry:")
    click.echo("")
    click.echo("  sudo tee /etc/cron.d/eggpool-backup > /dev/null << 'EGGPOOL_EOF'")
    for line in CRON_BACKUP_FILE.splitlines():
        click.echo(f"{line}")
    click.echo("EGGPOOL_EOF")
    click.echo("")
    click.echo("Snippet for /etc/cron.d/eggpool-backup:")
    click.echo("")
    click.echo(CRON_BACKUP_FILE)

    backup_blob = CRON_BACKUP_SCRIPT + CRON_BACKUP_FILE
    if _copy_to_clipboard(backup_blob):
        click.echo("")
        click.echo("Copied script + cron entry to clipboard.")


@deploy.command("all")
@click.pass_context
def deploy_all(ctx: click.Context) -> None:
    """Print every deployment snippet in sequence."""
    ctx.invoke(deploy_systemd)
    click.echo("")
    click.echo("=" * 72)
    click.echo("")
    ctx.invoke(deploy_logrotate)
    click.echo("")
    click.echo("=" * 72)
    click.echo("")
    ctx.invoke(deploy_cron)


@cli.command()
@click.pass_context
def rehash(ctx: click.Context) -> None:
    """Reload configuration in the running server."""
    from eggpool.providers.connect import signal_reload

    if signal_reload():
        click.echo("Configuration reloaded.")
    else:
        click.echo("Server is not running.", err=True)
        sys.exit(1)


def _update_server_config(config_path: str, key: str, value: str) -> None:
    """Update a [server] key in the TOML config file."""
    from pathlib import Path

    path = Path(config_path)
    if not path.exists():
        click.echo(f"Config file not found: {config_path}", err=True)
        sys.exit(1)

    # Write numeric values unquoted, strings quoted
    try:
        int(value)
        toml_value = value
    except ValueError:
        toml_value = f'"{value}"'

    lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    in_server = False
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped == "[server]":
            in_server = True
            new_lines.append(line)
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_server = False
        if in_server and stripped.startswith(f"{key} ="):
            new_lines.append(f"{key} = {toml_value}")
            updated = True
            continue
        new_lines.append(line)

    if not updated:
        click.echo(f"Key '{key}' not found in [server] section.", err=True)
        sys.exit(1)

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


@cli.command("set")
@click.argument("key", type=click.Choice(["port", "host"]))
@click.argument("value")
@click.pass_context
def set_config(ctx: click.Context, key: str, value: str) -> None:
    """Set a server configuration value and restart the server.

    Supported keys: port, host. The server must be restarted for
    port/host changes to take effect; this command restarts it
    automatically if a PID file is found.
    """
    from eggpool.providers.connect import signal_reload, signal_restart

    config_path: str = ctx.obj["config_path"]
    _update_server_config(config_path, key, value)
    click.echo(f"Set {key} = {value} in {config_path}.")

    if signal_restart():
        click.echo("Server restarted.")
    elif signal_reload():
        click.echo("Configuration reloaded.")
    else:
        click.echo("Server is not running.")


@cli.group()
def dashboard() -> None:
    """Dashboard configuration commands."""


def _read_dashboard_public(config_path: str) -> bool:
    """Read the current dashboard.public value from config."""
    from pathlib import Path

    path = Path(config_path)
    if not path.exists():
        return True  # default

    in_dashboard = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[dashboard]":
            in_dashboard = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_dashboard = False
            continue
        if in_dashboard and stripped.startswith("public ="):
            parts = stripped.split("=", 1)
            if len(parts) == 2:
                return parts[1].strip().lower() == "true"
    return True  # default


def _write_dashboard_public(config_path: str, public: bool) -> None:
    """Write the dashboard.public value to config."""
    from pathlib import Path

    path = Path(config_path)
    if not path.exists():
        click.echo(f"Config file not found: {config_path}", err=True)
        sys.exit(1)

    lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    in_dashboard = False
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped == "[dashboard]":
            in_dashboard = True
            new_lines.append(line)
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_dashboard = False
        if in_dashboard and stripped.startswith("public ="):
            new_lines.append(f"public = {'true' if public else 'false'}")
            updated = True
            continue
        new_lines.append(line)

    if not updated:
        # Insert public = ... after [dashboard] section
        result: list[str] = []
        inserted = False
        for line in new_lines:
            result.append(line)
            if line.strip() == "[dashboard]" and not inserted:
                result.append(f"public = {'true' if public else 'false'}")
                inserted = True
        new_lines = result

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


@dashboard.command("public")
@click.option(
    "--on", "set_public", flag_value="true", help="Require API key for dashboard."
)
@click.option(
    "--off",
    "set_public",
    flag_value="false",
    help="Allow public dashboard access (default).",
)
@click.pass_context
def dashboard_public(ctx: click.Context, set_public: str | None) -> None:
    """Toggle dashboard public access.

    Without options, shows the current setting and toggles it.
    With --on/--off, sets the value explicitly.
    """
    from eggpool.providers.connect import signal_reload

    config_path: str = ctx.obj["config_path"]
    current = _read_dashboard_public(config_path)

    new_value = not current if set_public is None else set_public == "true"

    _write_dashboard_public(config_path, new_value)

    if new_value:
        click.echo("Dashboard is now public (no API key required).")
    else:
        click.echo("Dashboard now requires API key authentication.")

    if signal_reload():
        click.echo("Configuration reloaded.")
    else:
        click.echo("Server is not running.")


@cli.command()
@click.pass_context
def migrate(ctx: click.Context) -> None:
    """Run database migrations."""
    config_path: str = ctx.obj["config_path"]

    try:
        config = AppConfig.from_toml(config_path)
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    async def _run() -> None:
        db = Database(
            path=config.database.path,
            busy_timeout_ms=config.database.busy_timeout_ms,
            wal=config.database.wal,
            synchronous=config.database.synchronous,
        )
        await db.connect()
        try:
            runner = MigrationRunner(db)
            await runner.run()
            click.echo("Migrations completed successfully")
        finally:
            await db.disconnect()

    try:
        asyncio.run(_run())
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@cli.group()
def models() -> None:
    """Model catalog commands."""


@models.command("refresh")
@click.pass_context
def models_refresh(ctx: click.Context) -> None:
    """Refresh the model catalog from upstream."""
    from eggpool.accounts.registry import AccountRegistry
    from eggpool.catalog.service import CatalogService

    config_path: str = ctx.obj["config_path"]

    try:
        config = AppConfig.from_toml(config_path)
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    try:
        config.validate_account_credentials()
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    async def _run() -> None:
        db = Database(
            path=config.database.path,
            busy_timeout_ms=config.database.busy_timeout_ms,
            wal=config.database.wal,
            synchronous=config.database.synchronous,
        )
        await db.connect()
        try:
            runner = MigrationRunner(db)
            await runner.run()

            provider_repo = ProviderRepository(db)
            configured_providers = {
                pid: {"base_url": pcfg.base_url, "protocols": pcfg.protocols}
                for pid, pcfg in config.providers.items()
            }
            await provider_repo.sync_from_config(configured_providers)

            account_repo = AccountRepository(db)
            await account_repo.sync_from_config(account_config_rows(config))

            registry = AccountRegistry(config)
            client_pool = ProviderClientPool.from_app_config(config)
            try:
                catalog = CatalogService(config, registry, db, client_pool)
                await catalog.refresh()
                count = catalog.cache.model_count
                click.echo(f"Refreshed catalog: {count} models found")
            finally:
                await client_pool.close()
        finally:
            await db.disconnect()

    try:
        asyncio.run(_run())
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@cli.group()
def accounts() -> None:
    """Account management commands."""


@accounts.command("list")
@click.pass_context
def accounts_list(ctx: click.Context) -> None:
    """List configured provider accounts."""
    from eggpool.providers.connect import list_config_accounts

    config_path: str = ctx.obj["config_path"]
    accts = list_config_accounts(config_path)

    if not accts:
        click.echo("No configured accounts. Run `eggpool connect` to add one.")
        return

    click.echo("Configured accounts:")
    for acct in accts:
        click.echo(f"  {acct.label}")

    click.echo(f"\nTotal: {len(accts)} accounts")


@accounts.command("status")
@click.pass_context
def accounts_status(ctx: click.Context) -> None:
    """Show account status."""
    config_path: str = ctx.obj["config_path"]

    try:
        config = AppConfig.from_toml(config_path)
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not config.all_accounts():
        click.echo(
            "No provider accounts configured.\n\n"
            "  Use `eggpool connect` to add a provider interactively,\n"
            "  or edit config.toml to add accounts manually."
        )
        return

    for acct in config.all_accounts():
        provider_id = _get_provider_for_account(config, acct.name)
        env_set = "yes" if os.environ.get(acct.api_key_env) else "no"
        click.echo(
            f"  {acct.name}: provider={provider_id}, enabled={acct.enabled}, "
            f"weight={acct.weight}, "
            f"api_key_env={acct.api_key_env} (set={env_set})"
        )

    click.echo(f"\nTotal accounts: {len(config.all_accounts())}")


def _get_provider_for_account(config: AppConfig, account_name: str) -> str:
    """Return the provider ID for an account."""
    for provider_id, provider_cfg in config.providers.items():
        for acct in provider_cfg.accounts:
            if acct.name == account_name:
                return provider_id
    return "unknown"


@cli.group("db")
def db_group() -> None:
    """Database maintenance commands."""


@db_group.command("vacuum")
@click.pass_context
def db_vacuum(ctx: click.Context) -> None:
    """Vacuum the database to reclaim space."""
    config_path: str = ctx.obj["config_path"]

    try:
        config = AppConfig.from_toml(config_path)
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    async def _run() -> None:
        db = Database(
            path=config.database.path,
            busy_timeout_ms=config.database.busy_timeout_ms,
            wal=config.database.wal,
            synchronous=config.database.synchronous,
        )
        await db.connect()
        try:
            await db.vacuum()
            click.echo("Database vacuum completed successfully")
        finally:
            await db.disconnect()

    try:
        asyncio.run(_run())
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--check", "check_only", is_flag=True, help="Only check, do not install.")
@click.option(
    "--from-source", is_flag=True, help="Force update from git source instead of PyPI."
)
@click.pass_context
def update(ctx: click.Context, check_only: bool, from_source: bool) -> None:
    """Check for updates and reinstall if a newer version is available.

    Config and database files are never overwritten. If the server is
    running it is restarted automatically after a successful update.
    """
    import importlib.metadata
    import subprocess

    import httpx

    from eggpool.providers.connect import signal_reload, signal_restart

    current_version = importlib.metadata.version("eggpool")
    click.echo(f"Current version: {current_version}")

    # Query PyPI for the latest version
    pypi_url = "https://pypi.org/pypi/eggpool/json"
    try:
        resp = httpx.get(pypi_url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        click.echo(f"Error checking for updates: {exc}", err=True)
        sys.exit(1)

    latest_version: str = data.get("info", {}).get("version", "")
    if not latest_version:
        click.echo("Could not determine latest version from PyPI.", err=True)
        sys.exit(1)

    click.echo(f"Latest version:  {latest_version}")

    if current_version == latest_version:
        click.echo("Already up to date.")
        return

    if check_only:
        click.echo("An update is available.")
        return

    click.echo(f"Updating from {current_version} to {latest_version}...")

    # Determine if we're running under pipx
    running_under_pipx = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )

    if from_source:
        # Force update from git source
        repo = "eggstack/eggpool"
        pip_target = f"git+https://github.com/{repo}.git@v{latest_version}"
        cmd = [sys.executable, "-m", "pip", "install", pip_target]
    elif running_under_pipx:
        # Running under pipx - try pipx upgrade first
        cmd = ["pipx", "upgrade", "eggpool"]
    else:
        # Try pip install --upgrade
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "eggpool"]

    click.echo(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603

    if result.returncode != 0:
        click.echo("Update failed:", err=True)
        click.echo(result.stderr, err=True)
        sys.exit(1)

    # Verify the installed version
    try:
        new_version = importlib.metadata.version("eggpool")
    except Exception:
        new_version = "unknown"

    click.echo(f"Installed version: {new_version}")

    if signal_restart():
        click.echo("Server restarted.")
    elif signal_reload():
        click.echo("Configuration reloaded.")
    else:
        click.echo("Server is not running.")


@cli.command()
@click.option(
    "--providers",
    "providers_path",
    default=None,
    help="Path to the providers template file. Uses bundled template if not specified.",
    type=click.Path(),
)
@click.pass_context
def onboard(ctx: click.Context, providers_path: str | None) -> None:
    """Run the interactive onboarding setup.

    Guides you through connecting providers, validates configuration,
    and starts the server.
    """
    from eggpool.onboard import run_onboarding

    config_path: str = ctx.obj["config_path"]
    run_onboarding(config_path, providers_path)


def _read_pid() -> int | None:
    """Read the PID from the PID file. Returns None if not found or invalid."""
    from eggpool.constants import PID_FILE

    if not PID_FILE.exists():
        return None

    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _is_process_running(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    import os

    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _wait_for_exit(pid: int, timeout: float = 10.0) -> bool:
    """Wait for a process to exit. Returns True if exited, False on timeout."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_process_running(pid):
            return True
        time.sleep(0.1)
    return False


@cli.command()
@click.option("--timeout", default=10.0, help="Seconds to wait for shutdown.")
@click.pass_context
def stop(ctx: click.Context, timeout: float) -> None:
    """Stop the running server."""
    import os
    import signal

    from eggpool.constants import PID_FILE

    pid = _read_pid()
    if pid is None:
        click.echo("Server is not running (no PID file found).")
        return

    if not _is_process_running(pid):
        click.echo("Server is not running (stale PID file).")
        with contextlib.suppress(OSError):
            PID_FILE.unlink(missing_ok=True)
        return

    click.echo(f"Stopping server (PID {pid})...")

    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        click.echo(f"Error sending signal: {exc}", err=True)
        with contextlib.suppress(OSError):
            PID_FILE.unlink(missing_ok=True)
        return

    if _wait_for_exit(pid, timeout):
        click.echo("Server stopped.")
    else:
        click.echo(
            f"Server did not stop within {timeout}s. Try 'kill -9' or check process.",
            err=True,
        )


@cli.command()
@click.option("--timeout", default=10.0, help="Seconds to wait for shutdown.")
@click.pass_context
def restart(ctx: click.Context, timeout: float) -> None:
    """Fully restart the server (stop then start).

    This stops the current process and starts a fresh one.
    For config-only reloads, use 'eggpool rehash' instead.
    """
    import contextlib
    import os
    import signal
    import subprocess
    import sys as _sys

    from eggpool.constants import PID_FILE

    config_path: str = ctx.obj["config_path"]

    pid = _read_pid()
    if pid is not None and _is_process_running(pid):
        click.echo(f"Stopping server (PID {pid})...")
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGTERM)

        if not _wait_for_exit(pid, timeout):
            click.echo(
                f"Server did not stop within {timeout}s. "
                "Try 'kill -9' or check process.",
                err=True,
            )
            return

        click.echo("Server stopped.")

    # Clean up stale PID file
    with contextlib.suppress(OSError):
        PID_FILE.unlink(missing_ok=True)

    click.echo("Starting server...")

    # Start the server as a subprocess
    subprocess.Popen(  # noqa: S603
        [_sys.executable, "-m", "eggpool", "--config", config_path, "serve"],
        cwd=os.getcwd(),
        start_new_session=True,
    )

    click.echo("Server started.")


@cli.command("init-config")
@click.argument("target", required=False, type=click.Path())
@click.option("--force", is_flag=True, help="Overwrite existing config file.")
@click.pass_context
def init_config(ctx: click.Context, target: str | None, force: bool) -> None:
    """Write config.example.toml into the current directory (or TARGET)."""
    from importlib.resources import as_file, files

    ref = files("eggpool._share").joinpath("config.example.toml")
    with as_file(ref) as source_path:
        if not source_path.exists():
            click.echo("Error: bundled config.example.toml not found", err=True)
            sys.exit(1)

        target_path = Path(target) if target else Path("config.toml")

        if target_path.exists() and not force:
            click.echo(
                f"Error: {target_path} already exists. Use --force to overwrite.",
                err=True,
            )
            sys.exit(1)

        import shutil

        shutil.copy2(source_path, target_path)
        click.echo(f"Config written to {target_path}")


def main() -> NoReturn:
    """Main entry point."""
    cli(obj={})  # type: ignore[call-arg]
    sys.exit(0)
