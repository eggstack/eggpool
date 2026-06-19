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
    from eggpool.providers.connect import signal_reload

    config_path: str = ctx.obj["config_path"]
    try:
        ok = do_connect(config_path, providers_path)
    except KeyboardInterrupt:
        click.echo("\n  Cancelled.")
        return
    if not ok:
        sys.exit(1)

    if signal_reload():
        click.echo("  Configuration reloaded.")
    else:
        click.echo("  Server is not running.")


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
@click.argument("target")
@click.pass_context
def logout(ctx: click.Context, target: str) -> None:
    """Remove a configured provider account by provider, env var, or API key."""
    from eggpool.providers.connect import (
        TerminalMenu,
        matching_logout_accounts,
        remove_account_from_config,
        signal_reload,
    )

    config_path: str = ctx.obj["config_path"]

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
            click.echo("\n  Cancelled.")
            return
        if selected is None:
            click.echo("  Cancelled.")
            return
        selected_index = [match.label for match in matches].index(selected)
        account = matches[selected_index]

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
        require_auth_at_startup(config.server.api_key_env)
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
            new_lines.append(f'{key} = "{value}"')
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


@accounts.command("list")
@click.pass_context
def accounts_list(ctx: click.Context) -> None:
    """List configured provider accounts and their API key backends."""
    config_path: str = ctx.obj["config_path"]

    try:
        config = AppConfig.from_toml(config_path)
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not config.providers:
        click.echo("No providers configured.")
        return

    for provider_id, provider_cfg in config.providers.items():
        if not provider_cfg.accounts:
            click.echo(f"  {provider_id}: {provider_cfg.base_url} (no accounts)")
            continue
        for acct in provider_cfg.accounts:
            env_set = "yes" if os.environ.get(acct.api_key_env) else "no"
            click.echo(
                f"  {provider_id}/{acct.name}: "
                f"{provider_cfg.base_url}  "
                f"{acct.api_key_env} (set={env_set})"
            )

    total = sum(len(p.accounts) for p in config.providers.values())
    click.echo(f"\nTotal: {len(config.providers)} providers, {total} accounts")


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


def main() -> NoReturn:
    """Main entry point."""
    cli(obj={})  # type: ignore[call-arg]
    sys.exit(0)
