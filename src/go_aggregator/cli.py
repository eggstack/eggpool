"""CLI entry point for the aggregator."""

from __future__ import annotations

import sys
from typing import NoReturn

import click

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

    import asyncio

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


def main() -> NoReturn:
    """Main entry point."""
    cli(obj={})  # type: ignore[call-arg]
    sys.exit(0)
