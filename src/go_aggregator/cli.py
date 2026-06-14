"""CLI entry point for the aggregator."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import NoReturn

import click
import httpx

from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.errors import AggregatorError
from go_aggregator.logging import configure_logging
from go_aggregator.models.config import AppConfig


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
    """OpenCode Go Aggregator - aggregate OpenCode Go subscriptions."""
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

    configure_logging(level=config.server.log_level)

    from go_aggregator.app import create_app

    app = create_app(config, config_path=config_path)
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        access_log=config.server.access_log,
        timeout_graceful_shutdown=30,
    )


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

    click.echo(f"Configuration loaded successfully from {config_path}")
    click.echo(f"  Server: {config.server.host}:{config.server.port}")
    click.echo(f"  Accounts: {len(config.accounts)}")
    click.echo(f"  Database: {config.database.path}")


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

    asyncio.run(_run())


@cli.group()
def models() -> None:
    """Model catalog commands."""


@models.command("refresh")
@click.pass_context
def models_refresh(ctx: click.Context) -> None:
    """Refresh the model catalog from upstream."""
    from go_aggregator.accounts.registry import AccountRegistry
    from go_aggregator.catalog.service import CatalogService

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

            registry = AccountRegistry(config)
            client = httpx.AsyncClient(base_url=config.upstream.base_url)
            try:
                catalog = CatalogService(config, registry, db, client)
                await catalog.refresh()
                count = catalog.cache.model_count
                click.echo(f"Refreshed catalog: {count} models found")
            finally:
                await client.aclose()
        finally:
            await db.disconnect()

    asyncio.run(_run())


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

    if not config.accounts:
        click.echo("No accounts configured.")
        return

    for acct in config.accounts:
        env_set = "yes" if os.environ.get(acct.api_key_env) else "no"
        click.echo(
            f"  {acct.name}: enabled={acct.enabled}, "
            f"weight={acct.weight}, "
            f"api_key_env={acct.api_key_env} (set={env_set})"
        )

    click.echo(f"\nTotal accounts: {len(config.accounts)}")


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
            await db.connection.execute("VACUUM")
            click.echo("Database vacuum completed successfully")
        finally:
            await db.disconnect()

    asyncio.run(_run())


def main() -> NoReturn:
    """Main entry point."""
    cli(obj={})  # type: ignore[call-arg]
    sys.exit(0)
