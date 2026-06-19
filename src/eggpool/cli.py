"""CLI entry point for the aggregator."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import NoReturn

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


@click.group()
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
    ctx.obj["config_path"] = config_path


@cli.command()
@click.pass_context
def serve(ctx: click.Context) -> None:
    """Start the aggregation proxy server."""
    import uvicorn

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

    configure_logging(level=config.server.log_level)

    from eggpool.app import create_app

    app = create_app(config, config_path=config_path)
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        access_log=config.server.access_log,
        timeout_graceful_shutdown=30,
    )


@cli.group(invoke_without_command=True)
@click.option(
    "--providers",
    "providers_path",
    default="providers.toml",
    help="Path to the providers template file.",
    type=click.Path(),
)
@click.pass_context
def connect(ctx: click.Context, providers_path: str) -> None:
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

    providers_path: str = ctx.obj.get("providers_path", "providers.toml")
    templates = load_provider_templates(providers_path)
    if not templates:
        click.echo(f"No provider templates found in {providers_path}")
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
    """Read the current server API key from config."""
    from eggpool.models.config import AppConfig

    config = AppConfig.from_toml(config_path)
    return config.server.api_key or ""


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
@click.pass_context
def configsetup_opencode(ctx: click.Context) -> None:
    """Print OpenCode config snippet for connecting to this router."""
    config_path: str = ctx.obj["config_path"]
    key = _read_server_api_key(config_path)
    if not key:
        click.echo("No API key configured. Run `eggpool newkey` first.", err=True)
        sys.exit(1)

    config = AppConfig.from_toml(config_path)
    port = config.server.port

    snippet = (
        f"{{\n"
        f'  "providers": {{\n'
        f'    "eggpool": {{\n'
        f'      "api_key": "{key}",\n'
        f'      "base_url": "http://localhost:{port}/v1"\n'
        f"    }}\n"
        f"  }}\n"
        f"}}"
    )

    click.echo("Add to ~/.config/opencode/opencode.json:")
    click.echo("")
    click.echo(snippet)
    click.echo("")

    if _copy_to_clipboard(snippet):
        click.echo("Copied to clipboard.")


@configsetup.command("claude-code")
@click.pass_context
def configsetup_claude_code(ctx: click.Context) -> None:
    """Print Claude Code config snippet for connecting to this router."""
    config_path: str = ctx.obj["config_path"]
    key = _read_server_api_key(config_path)
    if not key:
        click.echo("No API key configured. Run `eggpool newkey` first.", err=True)
        sys.exit(1)

    config = AppConfig.from_toml(config_path)
    port = config.server.port

    snippet = (
        f'{{\n  "api_key": "{key}",\n  "base_url": "http://localhost:{port}/v1"\n}}'
    )

    click.echo("Add to ~/.claude/settings.json or pass via --api-key and --base-url:")
    click.echo("")
    click.echo(snippet)
    click.echo("")
    click.echo(f"Or run: claude --api-key {key} --base-url http://localhost:{port}/v1")

    if _copy_to_clipboard(snippet):
        click.echo("Copied to clipboard.")


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
        click.echo("No accounts configured.")
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
@click.pass_context
def update(ctx: click.Context, check_only: bool) -> None:
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

    # Query GitHub for the latest release tag
    repo = "eggstack/eggpool"
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        resp = httpx.get(api_url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        click.echo(f"Error checking for updates: {exc}", err=True)
        sys.exit(1)

    latest_tag: str = data.get("tag_name", "")
    if not latest_tag:
        click.echo("Could not determine latest version from GitHub.", err=True)
        sys.exit(1)

    latest_version = latest_tag.lstrip("v")
    click.echo(f"Latest version:  {latest_version}")

    if current_version == latest_version:
        click.echo("Already up to date.")
        return

    if check_only:
        click.echo("An update is available.")
        return

    click.echo(f"Updating from {current_version} to {latest_version}...")

    # Prefer pip install from the GitHub repo
    pip_target = f"git+https://github.com/{repo}.git@{latest_tag}"
    cmd = [sys.executable, "-m", "pip", "install", pip_target]

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


def main() -> NoReturn:
    """Main entry point."""
    cli(obj={})  # type: ignore[call-arg]
    sys.exit(0)
