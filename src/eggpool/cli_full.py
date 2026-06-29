"""CLI entry point for the aggregator."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

import click

from eggpool.accounts.registry import account_config_rows
from eggpool.auth import require_auth_at_startup
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import AccountRepository, ProviderRepository
from eggpool.deploy_user import (
    DeployUser,
    default_config_dir,
    default_data_dir,
    default_state_dir,
    resolve_deploy_user,
    resolve_env_path,
)
from eggpool.errors import AggregatorError, ConfigError
from eggpool.lifecycle import (
    InstallMethod,
    default_backup_dir,
    list_backups,
    read_backup_contents,
    resolve_uninstall_paths,
    restore_backup,
    select_backup,
    verify_binary_removed,
)
from eggpool.lifecycle import (
    uninstall as do_uninstall,
)
from eggpool.lifecycle.backup import (
    BackupContents,
    create_backup,
    create_runtime_backup,
)
from eggpool.logging import configure_logging
from eggpool.models.config import AppConfig
from eggpool.providers.client_pool import ProviderClientPool
from eggpool.providers.contract import PROVIDER_STATUS_SYMBOLS, compose_provider_url
from eggpool.providers.outbound import OutboundClientManager
from eggpool.toml_edit import (
    render_toml_string,
    section_has_key,
    update_section_value,
)
from eggpool.update_checker import async_check_for_update


class _ConfigPathGroup(click.Group):
    """Click group that shows the resolved config path in help output."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        super().format_help(ctx, formatter)
        # Resolve the config path from Click's parsed context instead of
        # re-parsing sys.argv manually. ``ctx.params`` is populated by
        # Click's own argument parser, so this honors --config regardless
        # of where on the command line it appears.
        config_path_raw = ctx.params.get("config_path") or "config.toml"
        config_path = (
            config_path_raw if isinstance(config_path_raw, str) else "config.toml"
        )
        # Prefer the resolved absolute path stored on the parent context
        # when the group callback has already run.
        parent_obj = ctx.parent.obj if ctx.parent is not None else None
        if isinstance(parent_obj, dict):
            stored_raw: object = cast("dict[str, object]", parent_obj).get(
                "config_path", config_path
            )
            if isinstance(stored_raw, str):
                config_path = stored_raw
        resolved = os.path.abspath(config_path)
        formatter.write(f"\nConfig file: {resolved}\n")


@click.group(cls=_ConfigPathGroup, invoke_without_command=True)
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to the TOML configuration file. Falls back to $EGGPOOL_CONFIG, "
    "then ~/.config/eggpool/config.toml, then ./config.toml.",
    type=click.Path(),
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """EggPool - aggregate OpenCode Go subscriptions."""
    ctx.ensure_object(dict)
    from eggpool.deploy_user import resolve_config_path

    resolved = resolve_config_path(cli_value=config_path)
    ctx.obj["config_path"] = str(resolved)
    ctx.obj["config_path_resolved"] = True
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        return

    _skip_ensure_config = {"help", "version", "init-config", "uninstall", "recover"}
    if ctx.invoked_subcommand not in _skip_ensure_config:
        from eggpool.config import ensure_config

        ensure_config(ctx.obj["config_path"])


def _app_loader(target: str) -> Any:
    """Build the ASGI app for a Granian worker from the config-path target.

    Granian spawns worker subprocesses via multiprocessing, which produces
    a fresh Python interpreter that re-imports ``eggpool.cli``. Module-level
    mutable state set in the parent process is not inherited by the worker,
    so the app must be reconstructed from the config path inside the worker.
    """
    from eggpool.app import create_app

    return create_app(config_path=target)


@cli.command()
@click.option(
    "--daemon",
    "daemon",
    is_flag=True,
    help=(
        "Spawn a detached supervisor in the background and return the "
        "shell promptly. The child runs the normal foreground `serve` "
        "command; stdin is closed and stdout/stderr are redirected to "
        "the daemon log file (see --log-file). Use this for personal / "
        "SBC deployments. Systemd units should NOT use --daemon; run "
        "foreground `serve` and let systemd manage the process."
    ),
)
@click.option(
    "--log-file",
    "log_file",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help=(
        "Log destination for the detached supervisor when --daemon is "
        "set. Defaults to $EGGPOOL_LOG_FILE, otherwise the resolver's "
        "state-dir log (~/.local/state/eggpool/eggpool.log). Ignored "
        "without --daemon; the foreground command always logs to the "
        "calling terminal."
    ),
)
@click.option(
    "--quiet",
    "quiet",
    is_flag=True,
    help=(
        "With --daemon, send the supervisor's stdout/stderr to "
        "/dev/null when no log file is configured. Has no effect "
        "without --daemon; foreground `serve` always streams to the "
        "terminal so the operator can see Granian's output."
    ),
)
@click.option(
    "--as-root",
    "as_root",
    is_flag=True,
    help=(
        "Allow --daemon to start when the current effective UID is 0. "
        "Refused by default to prevent accidentally daemonizing a "
        "personal deployment as root; use this flag for intentional "
        "system-wide installs."
    ),
)
@click.pass_context
def serve(
    ctx: click.Context,
    daemon: bool,
    log_file: str | None,
    quiet: bool,
    as_root: bool,
) -> None:
    """Start the aggregation proxy server.

    Foreground mode (the default) is the Granian supervisor. Granian
    keeps ``workers=1`` so the total process count is two (supervisor
    + one worker) plus a small thread pool sized by ``[server].threads``.
    The supervisor owns the PID file: it writes ``os.getpid()`` before
    ``Granian.serve()`` and clears the file when Granian returns. The
    ASGI worker is a child of the supervisor and never touches the
    PID file, so ``eggpool stop`` always signals the right process.

    Daemon mode (``--daemon``) is a one-shot detach: this process
    validates the config, refuses to start a second instance, and
    spawns a detached child that runs the normal foreground
    supervisor. The child writes its own PID file and clears it on
    exit. The parent returns promptly with a short success message
    pointing at the log file. The child is **not** passed any
    ``--daemon`` flag; detachment is purely a parent-side concern.
    See ``plans/daemon-and-runtime.md`` for the full design.
    """
    from eggpool import runtime

    config_path: str = ctx.obj["config_path"]

    if daemon and os.geteuid() == 0 and not as_root:
        click.echo(
            "Error: refusing to daemonize as root for personal deployment.\n"
            "  Run as your normal user, or pass --as-root if this is intentional.",
            err=True,
        )
        sys.exit(1)

    if daemon:
        _serve_daemon(ctx, config_path, log_file=log_file, quiet=quiet)
        return

    from granian import Granian  # type: ignore[import-untyped]

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

    # Refuse to start a second instance. The PID file is the primary
    # signal, but we also probe /v1/healthz so a server started by a
    # different installation (or that escaped the supervisor's
    # lifecycle) is still detected.
    existing_pid = runtime.read_pid()
    if existing_pid is not None and runtime.is_process_running(existing_pid):
        click.echo(
            f"Error: server is already running (PID {existing_pid}).\n"
            "  Run `eggpool stop` or `eggpool restart` to manage it.",
            err=True,
        )
        sys.exit(1)
    if runtime.probe_healthz(config.server.host, config.server.port):
        click.echo(
            f"Error: another process is already serving "
            f"{config.server.host}:{config.server.port}.\n"
            "  Run `eggpool stop` or use a different host/port.",
            err=True,
        )
        sys.exit(1)
    runtime.clear_pid_file()

    log_level = config.server.log_level.lower()
    runtime.write_pid_file()
    try:
        Granian(
            config_path,
            address=config.server.host,
            port=config.server.port,
            interface="asgi",  # type: ignore[reportArgumentType]
            workers=1,
            runtime_threads=config.server.threads,  # type: ignore[reportArgumentType]
            process_name="eggpool",
            log_level=log_level,  # type: ignore[reportArgumentType]
            log_access=config.server.access_log,
        ).serve(target_loader=_app_loader)  # type: ignore[reportArgumentType]
    finally:
        runtime.clear_pid_file()


def _serve_daemon(
    ctx: click.Context,
    config_path: str,
    *,
    log_file: str | None,
    quiet: bool,
) -> None:
    """Spawn a detached ``serve`` supervisor and report success.

    The parent only validates the config and refuses to start a second
    instance. The child is the normal foreground ``serve`` command; it
    owns its own PID file lifecycle and log destination. The parent
    does **not** wait for the child to come up before returning -- the
    operator can ``eggpool croncheck`` (or read the log) to confirm.
    """
    from eggpool import runtime
    from eggpool.runtime_paths import default_log_file, default_pid_file

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

    # Refuse to start a second instance. The PID file is the primary
    # signal, but we also probe /v1/healthz so a server started by a
    # different installation (or that escaped the supervisor's
    # lifecycle) is still detected.
    existing_pid = runtime.read_pid()
    if existing_pid is not None and runtime.is_process_running(existing_pid):
        click.echo(
            f"Error: server is already running (PID {existing_pid}).\n"
            "  Run `eggpool stop` or `eggpool restart` to manage it.",
            err=True,
        )
        sys.exit(1)
    if runtime.probe_healthz(config.server.host, config.server.port):
        click.echo(
            f"Error: another process is already serving "
            f"{config.server.host}:{config.server.port}.\n"
            "  Run `eggpool stop` or use a different host/port.",
            err=True,
        )
        sys.exit(1)
    runtime.clear_pid_file()

    pid_file = default_pid_file()
    log_target = Path(log_file) if log_file else default_log_file()

    try:
        proc = runtime.start_server(
            config_path,
            daemon=True,
            log_path=str(log_target),
            quiet=quiet,
        )
    except (OSError, FileNotFoundError) as exc:
        click.echo(f"Error: failed to start server: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Spawned eggpool supervisor (PID {proc.pid}).")
    click.echo(f"  PID file: {pid_file}")
    click.echo(f"  Log file: {log_target}")
    click.echo(
        "  Tail the log or run `eggpool croncheck` to confirm the "
        "supervisor finished starting up."
    )


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

    # Read the active config so we can annotate each provider with its
    # routing_priority. This is best-effort: if the config is missing or
    # malformed we still list the templates without priorities.
    config_path: str = ctx.obj.get("config_path", "config.toml")
    priorities: dict[str, int] = {}
    try:
        config = AppConfig.from_toml(config_path)
        priorities = {
            pid: pcfg.routing_priority for pid, pcfg in config.providers.items()
        }
    except (FileNotFoundError, AggregatorError, Exception):
        pass

    click.echo("Available providers:")
    for provider_id, tmpl in templates.items():
        status = tmpl.get("status", "unverified")
        notes = tmpl.get("notes", "")
        marker = "*" if tmpl.get("recommended") else " "
        status_label = PROVIDER_STATUS_SYMBOLS.get(status, "?")
        note_str = f" — {notes}" if notes else ""
        priority_str = ""
        if provider_id in priorities:
            priority_str = f" (priority {priorities[provider_id]})"
        elif tmpl.get("status") in ("verified", "experimental"):
            # Verified/experimental template with no config block: show
            # the default priority so operators can see what they'd get.
            priority_str = " (priority 0)"
        display_line = (
            f"  {marker} {provider_id}: {tmpl['display']}"
            f" ({tmpl['url']}) [{status_label}]{priority_str}{note_str}"
        )
        click.echo(display_line)

    verified = sum(1 for t in templates.values() if t.get("status") == "verified")
    experimental = sum(
        1 for t in templates.values() if t.get("status") == "experimental"
    )
    unverified = sum(
        1
        for t in templates.values()
        if t.get("status") not in ("verified", "experimental")
    )
    click.echo(
        f"\n  {verified} verified, {experimental} experimental, {unverified} unverified"
    )
    click.echo("  ✓ verified  ~ experimental  ? unverified")


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
        restart_server,
        select_config_account,
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

    if restart_server(config_path):
        click.echo("  Server restarted.")
    else:
        click.echo("  Server is not running.")


def _check_stale_contracts(config: AppConfig, config_path: str) -> list[str]:
    """Return advisories for provider contract shapes that have grown stale.

    These are warnings, not errors: the config may still load and run, but the
    shape suggests the provider block was written for an older eggpool version
    whose behavior has since changed. Each advisory is a single human-readable
    line; the caller prints them and exits 0.

    Inspects both the parsed ``AppConfig`` and the raw TOML so legacy
    ``models_method``/``models_path`` keys (which the parser strips into a
    synthesized ``models_endpoint`` table) can still be flagged for migration.
    """
    warnings: list[str] = []
    raw = _load_raw_config(config_path)
    raw_providers_section = _get_section(raw, "providers")
    raw_providers: dict[str, object] = raw_providers_section

    for provider in config.providers.values():
        endpoint = provider.models_endpoint

        if (
            endpoint is not None
            and endpoint.method == "DISABLED"
            and not provider.static_models
        ):
            warnings.append(
                f"[{provider.id}] models_endpoint is DISABLED but "
                "static_models is empty; the catalog will not list any "
                "models from this provider"
            )

        if (
            endpoint is not None
            and endpoint.method == "DISABLED"
            and provider.verify.require_models
        ):
            warnings.append(
                f"[{provider.id}] models_endpoint is DISABLED but "
                "verify.require_models is true; the contract is contradictory"
            )

        if provider.anthropic_path and "anthropic" not in provider.protocols:
            warnings.append(
                f"[{provider.id}] anthropic_path is set but 'anthropic' is "
                "not in protocols; the field will be ignored"
            )

        if provider.openai_path and "openai" not in provider.protocols:
            warnings.append(
                f"[{provider.id}] openai_path is set but 'openai' is not "
                "in protocols; the field will be ignored"
            )

        if endpoint is not None:
            try:
                compose_provider_url(provider, endpoint.path)
            except ConfigError:
                warnings.append(
                    f"[{provider.id}] base_url + models_endpoint.path "
                    "produces a duplicate /v1 segment; see docs/providers.md"
                )

        if provider.auth.mode != "none":
            for header in provider.headers:
                if header.name.casefold() == "authorization":
                    warnings.append(
                        f"[{provider.id}] static header 'Authorization' is "
                        f"set but auth.mode is '{provider.auth.mode}'; the "
                        "auth header will be replaced"
                    )
                    break

        if (
            provider.auth.mode == "api_key"
            and provider.auth.header == "Authorization"
            and "anthropic" in provider.protocols
        ):
            warnings.append(
                f"[{provider.id}] auth.mode='api_key' with "
                "header='Authorization' looks wrong; Anthropic-compatible "
                "providers typically use header='x-api-key'"
            )

        raw_section_obj = raw_providers.get(provider.id)
        if isinstance(raw_section_obj, dict):
            raw_section = cast("dict[str, object]", raw_section_obj)
            has_legacy_key = (
                "models_method" in raw_section or "models_path" in raw_section
            )
            has_endpoint_table = "models_endpoint" in raw_section
            if has_legacy_key and not has_endpoint_table:
                warnings.append(
                    f"[{provider.id}] using legacy models_method/models_path; "
                    f"consider migrating to "
                    f"[[providers.{provider.id}.models_endpoint]]"
                )

    return warnings


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

    stale_warnings = _check_stale_contracts(config, config_path)
    for message in stale_warnings:
        click.echo(f"  warning: {message}")
    if stale_warnings:
        click.echo(f"  {len(stale_warnings)} contract warning(s)")


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


def generate_api_key() -> str:
    """Generate a cryptographically secure API key."""
    import secrets

    return f"ep_{secrets.token_hex(32)}"


def _redact_key(key: str) -> str:
    """Return a short, non-secret fingerprint of an API key for display."""
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


def _load_raw_config(config_path: str) -> dict[str, object]:
    """Load the raw TOML config as a nested dict without Pydantic validation.

    Returns an empty dict if the file is missing or unparseable. Used by
    CLI commands that only need a few scalar fields (api_key, port,
    dashboard.public) and want to avoid the cost of full ``AppConfig``
    validation.
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
    # tomllib.load returns Mapping[str, Any] at the top level; the cast
    # narrows it for downstream helpers that consume ``dict[str, object]``.
    return cast("dict[str, object]", raw)


def _get_section(raw: dict[str, object], name: str) -> dict[str, object]:
    """Return a top-level TOML section as a dict, or empty dict."""
    section = raw.get(name)
    if isinstance(section, dict):
        return cast("dict[str, object]", section)
    return {}


def _read_server_api_key(config_path: str) -> str:
    """Read the current server API key from config without full validation."""
    raw = _load_raw_config(config_path)
    server = _get_section(raw, "server")
    value = server.get("api_key", "")
    return value if isinstance(value, str) else ""


def _read_server_port(config_path: str) -> int:
    """Read the server port from config without full validation."""
    from eggpool.constants import DEFAULT_PORT

    raw = _load_raw_config(config_path)
    server = _get_section(raw, "server")
    value = server.get("port", DEFAULT_PORT)
    return value if isinstance(value, int) else DEFAULT_PORT


def _detect_lan_ip() -> str:
    """Detect the LAN IP address of this machine."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def write_server_api_key(config_path: str, new_key: str) -> None:
    """Write a server API key to the [server] section of the config.

    If the [server] section declares ``api_key_env`` instead of an inline
    ``api_key = "..."`` line, the directive is preserved and no inline key
    is written. The operator must rotate the env-var to actually pick up
    the new value.
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
        click.echo(
            "Warning: No [server] section found in config. API key was not written.",
            err=True,
        )
        return
    if not result.key_found and has_api_key_env:
        click.echo(
            "Warning: [server] uses api_key_env; rotate the env-var "
            "to apply the new key.",
            err=True,
        )

    path.write_text("\n".join(result.lines) + "\n", encoding="utf-8")


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
@click.option(
    "--show-old",
    is_flag=True,
    help="Print the full previous key to stdout. Disabled by default to "
    "avoid leaking a key that may have just been rotated for security "
    "reasons.",
)
@click.pass_context
def newkey(ctx: click.Context, show_old: bool) -> None:
    """Generate a new server API key, overwriting the old one."""
    from eggpool.providers.connect import restart_server

    config_path: str = ctx.obj["config_path"]
    old_key = _read_server_api_key(config_path)
    new_key = generate_api_key()
    write_server_api_key(config_path, new_key)

    if old_key:
        if show_old:
            click.echo(f"Old key (expired): {old_key}")
        else:
            click.echo(f"Old key (expired, redacted): {_redact_key(old_key)}")
    click.echo(f"New key (use this): {new_key}")

    if restart_server(config_path):
        click.echo("Server restarted.")
    else:
        click.echo("Server is not running. Start it to apply the new key.")


@cli.group()
@click.pass_context
def configsetup(ctx: click.Context) -> None:
    """Print configuration snippets for code editors."""


def _detect_install_method() -> str:
    """Detect how eggpool was installed: 'pipx', 'uv-tool', 'source', or 'pip'."""
    in_venv = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )

    if in_venv:
        # Check the resolved eggpool binary (not the running Python)
        # against the canonical tool layout. Detecting the Python is
        # not enough: pipx and uv-tool both create a venv, but only the
        # binary path matches the tool's directory conventions.
        eggpool_exe = shutil.which("eggpool")
        candidates: list[Path] = []
        if eggpool_exe is not None:
            candidates.append(Path(eggpool_exe).resolve())
        candidates.append(Path(sys.executable).resolve())

        for exe in candidates:
            parts = exe.parts
            if "uv" in parts and "tools" in parts:
                return "uv-tool"
            if "pipx" in parts and ("venvs" in parts or "shared" in parts):
                return "pipx"

        # Fallback: generic venv (manual or unknown tool). We do not
        # classify as pipx just because pipx is on PATH; that produces
        # false positives on dev machines.
        return "pip"

    # Source checkout (pyproject.toml nearby)
    cli_path = Path(__file__).resolve()
    if (cli_path.parent.parent.parent / "pyproject.toml").exists():
        return "source"

    return "pip"


def _resolve_eggpool_binary() -> str | None:
    """Resolve the path to the eggpool binary for systemd ExecStart.

    Returns ``None`` when no eggpool binary is on PATH and the
    ``--install`` action cannot proceed. Callers that only need a
    best-effort value should pass the result through ``or
    "<fallback>"`` to fall back to ``sys.executable -m eggpool`` for
    source installs.
    """
    import shutil

    which = shutil.which("eggpool")
    if which is not None:
        return str(Path(which).resolve())
    return None


def _resolve_data_dir() -> Path:
    """Resolve the data directory for the current user."""
    from eggpool.constants import DEFAULT_DATABASE_PATH

    return Path(DEFAULT_DATABASE_PATH).parent


def _write_file(path: str, content: str) -> None:
    """Write a file, creating parent directories as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    click.echo(f"  Written {path}")


def _run_with_database(
    config: AppConfig,
    operation: Callable[[Database], Coroutine[object, object, object] | object],
) -> None:
    """Connect a :class:`Database`, run ``operation(db)``, disconnect.

    Centralizes the connect/try/finally/disconnect boilerplate used by
    ``migrate``, ``db vacuum``, and ``models refresh`` so the commands
    stay focused on their actual work.

    ``operation`` may be either an awaitable coroutine ``op(db)`` or a
    sync function returning a value. ``AggregatorError`` raised by the
    operation is converted into a non-zero exit.
    """
    import asyncio

    from eggpool.db.connection import Database

    async def _runner() -> object:
        db = Database(
            path=config.database.path,
            busy_timeout_ms=config.database.busy_timeout_ms,
            wal=config.database.wal,
            synchronous=config.database.synchronous,
        )
        await db.connect()
        try:
            result = operation(db)
            if asyncio.iscoroutine(result):
                return await cast("Coroutine[object, object, object]", result)
            return result
        finally:
            await db.disconnect()

    try:
        asyncio.run(_runner())
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


def _run_systemctl(
    args: list[str], quiet: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a systemctl command and return the completed process.

    Never raises for a non-zero return code; the caller decides what to
    do with the result. When ``quiet`` is false and the command wrote
    stdout, the captured output is echoed to the terminal.
    """
    cmd = ["systemctl", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        err_msg = result.stderr.strip() or "unknown error"
        click.echo(f"  Warning: {' '.join(cmd)} failed: {err_msg}", err=True)
    elif not quiet and result.stdout.strip():
        click.echo(result.stdout.strip())
    return result


def _run_systemctl_required(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a systemctl command and exit non-zero on failure.

    Unlike :func:`_run_systemctl`, this helper is for steps that must
    succeed for the deploy to be considered complete. The captured
    stderr is printed before the process exits so the operator can see
    why the install aborted.
    """
    result = _run_systemctl(args)
    if result.returncode != 0:
        click.echo(
            f"Error: {' '.join(['systemctl', *args])} failed.",
            err=True,
        )
        sys.exit(1)
    return result


def _stop_running_server() -> bool:
    """Stop the eggpool server if it is currently running.

    Returns True if the server is confirmed stopped, False if it was
    not running or did not exit within the timeout window.
    """
    pid = _read_pid()
    if pid is None or not _is_process_running(pid):
        return False

    click.echo(f"Stopping running server (PID {pid})...")
    from eggpool import runtime

    if not runtime.send_sigterm(pid):
        click.echo("  Warning: failed to send SIGTERM.", err=True)
    stopped = _wait_for_exit(pid, timeout=10.0)
    if stopped:
        click.echo("Server stopped.")
    else:
        click.echo(
            f"Server (PID {pid}) did not stop within 10s; "
            "it may be stuck. Continuing anyway.",
            err=True,
        )
    return stopped


def _print_install_hint() -> None:
    """Print a hint about how to run eggpool based on the detected install method."""
    method = _detect_install_method()
    if method == "source":
        click.echo("  (running from source: use 'uv run eggpool ...')", err=True)
    elif method == "pipx":
        click.echo("  (installed via pipx: use 'eggpool ...')", err=True)
    else:
        click.echo("  (use 'eggpool ...' or 'python -m eggpool ...')", err=True)


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
    """Print OpenCode config for connecting to this router.

    When ``[models].collapse_models`` is false (the default), the generated
    models map uses provider-suffixed IDs (``model-id/provider-id``) with
    per-provider limits. When true, the map uses unsuffixed IDs with
    conservative-merged limits across all providers that serve the model.
    """
    import json as _json

    from eggpool.catalog.limits import (
        EffectiveModelLimits,
        ModelLimitResolver,
        conservative_limits,
    )
    from eggpool.integrations.opencode import build_opencode_config_json
    from eggpool.models.config import AppConfig

    config_path: str = ctx.obj["config_path"]

    # Auto-generate API key if not present. A read-only filesystem or
    # permission error here would otherwise leave the user with a
    # key in stdout/clipboard that they cannot reuse on the next run.
    key = _read_server_api_key(config_path)
    if not key:
        try:
            key = generate_api_key()
            write_server_api_key(config_path, key)
            click.echo("Generated new server API key.", err=True)
        except OSError as exc:
            click.echo(
                f"Error: cannot persist new API key to {config_path}: {exc}. "
                "Refusing to print a key that would not survive a restart.",
                err=True,
            )
            sys.exit(1)

    port = _read_server_port(config_path)
    lan_ip = _detect_lan_ip()
    base_url = f"http://{lan_ip}:{port}/v1"

    # Try to load catalog from the database
    models_data: list[dict[str, Any]] = []
    config: AppConfig | None = None
    collapse_models = False
    try:
        import asyncio

        from eggpool.db.connection import Database

        config = AppConfig.from_toml(config_path)
        db_path = config.database.path
        collapse_models = config.models.collapse_models

        async def _load_catalog() -> list[dict[str, Any]]:
            db = Database(db_path)
            await db.connect()
            try:
                if collapse_models:
                    rows = await db.fetch_all(
                        "SELECT model_id, display_name, capabilities, "
                        "source_metadata FROM models"
                    )
                    out: list[dict[str, Any]] = []
                    for row in rows:
                        caps_raw = row["capabilities"]
                        meta_raw = row["source_metadata"]
                        caps: dict[str, Any] = _json.loads(caps_raw) if caps_raw else {}
                        meta: dict[str, Any] = _json.loads(meta_raw) if meta_raw else {}
                        out.append(
                            {
                                "model_id": row["model_id"],
                                "display_name": row["display_name"],
                                "capabilities": caps,
                                "source_metadata": meta,
                                "effective_limits": {},
                            }
                        )
                    return out

                rows = await db.fetch_all(
                    """
                    SELECT DISTINCT
                        am.model_id,
                        a.provider_id,
                        COALESCE(pmm.display_name, m.display_name) AS display_name,
                        COALESCE(pmm.capabilities, m.capabilities) AS capabilities,
                        COALESCE(pmm.source_metadata, m.source_metadata)
                            AS source_metadata
                    FROM account_models am
                    JOIN accounts a ON a.id = am.account_id
                    JOIN models m ON m.model_id = am.model_id
                    LEFT JOIN provider_model_metadata pmm
                        ON pmm.model_id = am.model_id
                       AND pmm.provider_id = a.provider_id
                    WHERE am.enabled = 1
                      AND a.enabled = 1
                      AND am.model_id <> '__deprecated__'
                      AND COALESCE(pmm.protocol, m.protocol) IN ('openai', 'anthropic')
                    """
                )
                if not rows:
                    rows = await db.fetch_all(
                        """
                        SELECT model_id, provider_id, display_name,
                               capabilities, source_metadata
                        FROM provider_model_metadata
                        WHERE model_id <> '__deprecated__'
                          AND protocol IN ('openai', 'anthropic')
                        """
                    )
                out = []
                for row in rows:
                    caps_raw = row["capabilities"]
                    meta_raw = row["source_metadata"]
                    caps: dict[str, Any] = _json.loads(caps_raw) if caps_raw else {}
                    meta: dict[str, Any] = _json.loads(meta_raw) if meta_raw else {}
                    base_model_id = row["model_id"]
                    provider_id = row["provider_id"]
                    out.append(
                        {
                            "model_id": (
                                f"{base_model_id}/{provider_id}"
                                if provider_id
                                else base_model_id
                            ),
                            "base_model_id": base_model_id,
                            "provider_id": provider_id,
                            "display_name": row["display_name"],
                            "capabilities": caps,
                            "source_metadata": meta,
                            "effective_limits": {},
                        }
                    )
                return out
            finally:
                await db.disconnect()

        models_data = asyncio.run(_load_catalog())
    except Exception:
        click.echo(
            "Warning: Could not load catalog. Run 'eggpool models refresh' "
            "or start the server to populate the catalog before generating "
            "model-specific limits.",
            err=True,
        )

    if config is not None:
        seen_model_keys = {str(m.get("model_id")) for m in models_data}
        for provider_id, provider_cfg in config.providers.items():
            if not provider_cfg.static_models:
                continue
            if not any(account.enabled for account in provider_cfg.accounts):
                continue
            for static in provider_cfg.static_models:
                exposed_id = (
                    static.id if collapse_models else f"{static.id}/{provider_id}"
                )
                if exposed_id in seen_model_keys:
                    continue
                capabilities: dict[str, Any] = {}
                if static.supports_tools is not None:
                    capabilities["supports_tools"] = static.supports_tools
                if static.supports_vision is not None:
                    capabilities["supports_vision"] = static.supports_vision
                if static.max_context_tokens is not None:
                    capabilities["max_context_tokens"] = static.max_context_tokens
                if static.max_input_tokens is not None:
                    capabilities["max_input_tokens"] = static.max_input_tokens
                if static.max_output_tokens is not None:
                    capabilities["max_output_tokens"] = static.max_output_tokens
                models_data.append(
                    {
                        "model_id": exposed_id,
                        "base_model_id": static.id,
                        "provider_id": provider_id,
                        "display_name": static.display_name or static.id,
                        "capabilities": capabilities,
                        "source_metadata": {
                            **static.source_metadata,
                            "source": "static_config",
                        },
                        "effective_limits": {},
                    }
                )
                seen_model_keys.add(exposed_id)

    # Re-apply current config overrides.
    if models_data and config is not None:
        resolver = ModelLimitResolver(config)
        if collapse_models:
            for m in models_data:
                eff = resolver.resolve(
                    provider_id=None,
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
        else:
            # Group by base model_id and apply conservative merge so OpenCode
            # compacts before any single provider's limit is exceeded.
            grouped: dict[str, list[dict[str, Any]]] = {}
            for m in models_data:
                base = m.get("base_model_id", m["model_id"])
                grouped.setdefault(base, []).append(m)
            for entries in grouped.values():
                limits_list: list[EffectiveModelLimits] = []
                for m in entries:
                    provider_id = m.get("provider_id")
                    eff = resolver.resolve(
                        provider_id=provider_id,
                        model_id=m.get("base_model_id", m["model_id"]),
                        capabilities=m.get("capabilities", {}),
                        source_metadata=m.get("source_metadata", {}),
                    )
                    limits_list.append(
                        EffectiveModelLimits(
                            context_tokens=eff.context_tokens,
                            input_tokens=eff.input_tokens,
                            output_tokens=eff.output_tokens,
                            enforce=eff.enforce,
                            context_source=eff.context_source,
                            input_source=eff.input_source,
                            output_source=eff.output_source,
                        )
                    )
                merged = conservative_limits(limits_list)
                merged_dict = {
                    "context_tokens": merged.context_tokens,
                    "input_tokens": merged.input_tokens,
                    "output_tokens": merged.output_tokens,
                    "enforce": merged.enforce,
                    "context_source": merged.context_source,
                    "input_source": merged.input_source,
                    "output_source": merged.output_source,
                }
                for m in entries:
                    m["effective_limits"] = merged_dict

    config_json = build_opencode_config_json(
        base_url=base_url,
        api_key=key,
        models=models_data,
    )

    # Print the config to stdout (contains the API key) and also try to
    # copy it to the clipboard so the user can paste it directly.
    click.echo(config_json)

    if _copy_to_clipboard(config_json):
        click.echo("Copied config to clipboard.", err=True)
    else:
        click.echo(
            "Could not copy to clipboard. Use the printed config above.",
            err=True,
        )

    if models_data:
        click.echo(f"Generated config with {len(models_data)} models.", err=True)
    else:
        click.echo(
            "Generated provider connection block (no model limits). "
            "Run 'eggpool models refresh' to populate model metadata.",
            err=True,
        )
    click.echo("Paste into ~/.config/opencode/opencode.json.", err=True)
    _print_install_hint()


@configsetup.command("claude-code")
@click.pass_context
def configsetup_claude_code(ctx: click.Context) -> None:
    """Print Claude Code config snippet for connecting to this router."""
    config_path: str = ctx.obj["config_path"]

    # Auto-generate API key if not present. See configsetup opencode
    # above for why the write must succeed before we proceed.
    key = _read_server_api_key(config_path)
    if not key:
        try:
            key = generate_api_key()
            write_server_api_key(config_path, key)
            click.echo("Generated new server API key.", err=True)
        except OSError as exc:
            click.echo(
                f"Error: cannot persist new API key to {config_path}: {exc}. "
                "Refusing to print a key that would not survive a restart.",
                err=True,
            )
            sys.exit(1)

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

    # The snippet contains the API key. Only send it through the
    # clipboard so it never appears in terminal scrollback; report
    # status to stderr and omit the echo line that used to include
    # ``claude --api-key <key>`` in plaintext.
    if _copy_to_clipboard(snippet):
        click.echo("Copied config to clipboard.", err=True)
    else:
        click.echo(
            "Could not copy to clipboard. Use `eggpool getkey` and "
            "pass --api-key to the Claude Code CLI.",
            err=True,
        )

    click.echo(
        "Paste into ~/.claude/settings.json or pass via --api-key "
        "and --base-url to the Claude Code CLI.",
        err=True,
    )


@cli.group()
@click.pass_context
def deploy(ctx: click.Context) -> None:
    """Print deployment snippets (systemd, logrotate, cron).

    Without --install, prints copy-paste instructions for manual setup.
    With --install, writes files and runs setup commands (requires root).
    """


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
@click.option(
    "--install",
    is_flag=True,
    help="Install the systemd unit. Personal mode refuses direct-root "
    "without --as-root; production mode is opt-in via --production.",
)
@click.option(
    "--production",
    is_flag=True,
    help="Production mode: create a dedicated system user and install "
    "to /etc/eggpool and /var/lib/eggpool. Requires root.",
)
@click.option(
    "--as-root",
    "as_root",
    is_flag=True,
    help="Allow personal --install to run as direct root. Refused by "
    "default to prevent an accidental root-owned personal deployment.",
)
@click.pass_context
def deploy_systemd(
    ctx: click.Context, install: bool, production: bool, as_root: bool
) -> None:
    """Print the systemd unit and install instructions.

    With --install, writes the unit file, reloads systemd, and enables
    and starts the service. Default mode targets the *invoking* user
    so the unit never runs as root unless --as-root is passed.

    Production mode (--production) automates the documented system
    layout: dedicated ``eggpool`` user, ``/etc/eggpool`` config dir,
    ``/var/lib/eggpool`` data dir, ``/var/log/eggpool`` log dir,
    ``/var/backups/eggpool`` backup dir, and the hardened
    :data:`eggpool.deploy.SYSTEMD_UNIT` constant.
    """
    from eggpool.deploy import SYSTEMD_UNIT, build_personal_systemd_unit

    config_path: str = ctx.obj["config_path"]
    binary_path = _resolve_eggpool_binary()

    if install and binary_path is None:
        click.echo(
            "Error: cannot locate the eggpool binary on PATH.\n"
            "  Install EggPool first (e.g. `uv tool install eggpool`).",
            err=True,
        )
        sys.exit(1)

    if production:
        _deploy_systemd_production(binary_path=binary_path, install=install)
        return

    # Personal mode path.
    deploy_user = resolve_deploy_user()
    if install and deploy_user.is_root and not as_root:
        click.echo(
            "Error: refusing to install a personal systemd unit as direct root.\n"
            "  Re-run with --as-root for a root-owned personal unit,\n"
            "  or with --production for the dedicated-system layout.\n"
            "  For a sudo-driven install, re-run via:\n"
            '    sudo env "PATH=$PATH" "$(command -v eggpool)" '
            "deploy systemd --install",
            err=True,
        )
        sys.exit(1)

    config_path_resolved = str(Path(config_path).expanduser().resolve())
    data_dir = str(default_data_dir())
    env_path_obj = resolve_env_path(config_path=Path(config_path_resolved))
    env_path = str(env_path_obj) if env_path_obj is not None else None

    user = deploy_user.user
    group = deploy_user.primary_group
    dynamic_unit = build_personal_systemd_unit(
        binary_path=binary_path or "eggpool",
        config_path=config_path_resolved,
        data_dir=data_dir,
        env_path=env_path,
        user=user,
        group=group,
    )

    _print_deploy_snippet(
        title="EggPool systemd unit (personal use)",
        target_path="/etc/systemd/system/eggpool.service",
        snippet=dynamic_unit,
        extra_steps=[
            "sudo systemctl daemon-reload",
            "sudo systemctl enable eggpool",
            "sudo systemctl start eggpool",
            "sudo systemctl status eggpool",
        ],
    )

    click.echo("")
    click.echo("=" * 72)
    click.echo("")
    click.echo("Production snippet (separate eggpool user, security hardening):")
    click.echo("")
    click.echo(SYSTEMD_UNIT)

    click.echo("")
    click.echo(
        "Run 'sudo eggpool deploy systemd --install' (personal) or "
        "'sudo eggpool deploy systemd --install --production' for the "
        "dedicated-system layout."
    )

    if not install:
        return

    _install_personal_systemd(
        deploy_user=deploy_user,
        unit=dynamic_unit,
        binary_path=binary_path or "eggpool",
        config_path=config_path_resolved,
        data_dir=data_dir,
        env_path=env_path,
    )


def _install_personal_systemd(
    *,
    deploy_user: DeployUser,
    unit: str,
    binary_path: str,
    config_path: str,
    data_dir: str,
    env_path: str | None,
) -> None:
    """Run the personal ``deploy systemd --install`` flow.

    Validates config readiness, prepares filesystem state, writes the
    unit, and runs ``systemctl daemon-reload`` / ``enable`` / ``start``.
    Any failure exits non-zero so the caller can surface a clear error.
    """
    if os.geteuid() != 0 and not deploy_user.is_sudo:
        click.echo(
            "Error: --install requires root or sudo.\n"
            "  Re-run via: sudo eggpool deploy systemd --install",
            err=True,
        )
        sys.exit(1)

    _stop_running_server()

    if not _confirm_action(
        f"Install systemd unit to /etc/systemd/system/eggpool.service "
        f"running as {deploy_user.user}?"
    ):
        click.echo("Aborted.")
        return

    _prepare_user_dirs(deploy_user)
    _ensure_personal_config_ready(config_path)

    target = "/etc/systemd/system/eggpool.service"
    _write_file(target, unit)
    _chown_to_user(Path(target), deploy_user)

    try:
        _run_systemctl_required(["daemon-reload"])
        _run_systemctl_required(["enable", "eggpool"])
        _run_systemctl_required(["start", "eggpool"])
    except SystemExit:
        raise

    click.echo("")
    _run_systemctl(["status", "eggpool"])


def _deploy_systemd_production(*, binary_path: str | None, install: bool) -> None:
    """Run the production ``deploy systemd --production`` flow."""
    if install and os.geteuid() != 0:
        click.echo(
            "Error: --production --install requires root privileges.",
            err=True,
        )
        sys.exit(1)

    from eggpool.deploy import SYSTEMD_UNIT

    click.echo("EggPool production systemd layout:")
    click.echo("  config:    /etc/eggpool/config.toml")
    click.echo("  env:       /etc/eggpool/env")
    click.echo("  data:      /var/lib/eggpool")
    click.echo("  log:       /var/log/eggpool")
    click.echo("  backups:   /var/backups/eggpool")
    click.echo("  user:      eggpool (system)")
    click.echo("")
    click.echo(SYSTEMD_UNIT)

    if not install:
        click.echo("")
        click.echo(
            "Run 'sudo eggpool deploy systemd --install --production' to "
            "perform the install automatically."
        )
        return

    if binary_path is None:
        click.echo(
            "Error: cannot locate the eggpool binary on PATH.\n"
            "  The production unit hard-codes the binary path; install "
            "eggpool first (e.g. `uv tool install eggpool`).",
            err=True,
        )
        sys.exit(1)

    _stop_running_server()

    if not _confirm_action(
        "Provision the production layout under /etc/eggpool and "
        "/var/lib/eggpool and start the eggpool service?"
    ):
        click.echo("Aborted.")
        return

    _provision_production_system_user()
    _provision_production_directories()
    _seed_production_config_if_missing()
    _seed_production_env_if_missing()
    _write_file("/etc/systemd/system/eggpool.service", SYSTEMD_UNIT)

    _run_production_validation()

    _run_systemctl_required(["daemon-reload"])
    _run_systemctl_required(["enable", "eggpool"])
    _run_systemctl_required(["start", "eggpool"])

    click.echo("")
    _run_systemctl(["status", "eggpool"])


def _provision_production_system_user() -> None:
    """Create the dedicated ``eggpool`` system user if missing."""
    import pwd  # noqa: PLC0415

    try:
        pwd.getpwnam("eggpool")
        click.echo("  System user 'eggpool' already exists.")
        return
    except KeyError:
        pass

    result = subprocess.run(  # noqa: S603
        [
            "useradd",
            "-r",
            "-s",
            "/usr/sbin/nologin",
            "-d",
            "/var/lib/eggpool",
            "eggpool",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        click.echo(
            f"Error: failed to create system user 'eggpool': {result.stderr.strip()}",
            err=True,
        )
        sys.exit(1)
    click.echo("  Created system user 'eggpool'.")


def _provision_production_directories() -> None:
    """Create and permission the production filesystem layout."""
    dirs: list[tuple[Path, str, str]] = [
        (Path("/var/lib/eggpool"), "eggpool:eggpool", "750"),
        (Path("/var/log/eggpool"), "eggpool:eggpool", "750"),
        (Path("/var/backups/eggpool"), "eggpool:eggpool", "750"),
        (Path("/etc/eggpool"), "root:eggpool", "755"),
    ]
    for path, _owner, mode in dirs:
        if not path.exists():
            path.mkdir(parents=True)
            click.echo(f"  Created {path}")
        _chown(path, "eggpool" if path != Path("/etc/eggpool") else "root")
        _chmod(path, mode)

    for fname, mode in (
        ("config.toml", "640"),
        ("env", "640"),
    ):
        target = Path("/etc/eggpool") / fname
        if target.exists():
            _chown(target, "root")
            _chmod(target, mode)


def _seed_production_config_if_missing() -> None:
    """Copy the bundled config.example.toml to /etc/eggpool/config.toml."""
    from importlib.resources import as_file, files  # noqa: PLC0415

    target = Path("/etc/eggpool/config.toml")
    if target.exists():
        click.echo(f"  {target} already exists; leaving as-is.")
        return

    try:
        ref = files("eggpool._share").joinpath("config.example.toml")
        with as_file(ref) as source:
            if source.exists():
                import shutil  # noqa: PLC0415

                shutil.copy2(source, target)
                _chown(target, "root")
                _chmod(target, "640")
                click.echo(f"  Seeded {target} from bundled template.")
                return
    except (OSError, ModuleNotFoundError):
        pass

    click.echo(
        f"  Warning: {target} not seeded (bundled template unavailable).",
        err=True,
    )


def _seed_production_env_if_missing() -> None:
    """Copy the bundled deploy/env.example to /etc/eggpool/env if missing."""
    target = Path("/etc/eggpool/env")
    if target.exists():
        click.echo(f"  {target} already exists; leaving as-is.")
        return

    source = Path(__file__).resolve().parents[2] / "deploy" / "env.example"
    if not source.exists():
        click.echo(
            f"  Warning: {source} not found; please create {target} manually.",
            err=True,
        )
        return
    import shutil  # noqa: PLC0415

    shutil.copy2(source, target)
    _chown(target, "root")
    _chmod(target, "640")
    click.echo(f"  Seeded {target} from {source}.")


def _run_production_validation() -> None:
    """Validate the seeded config and run migrations as the eggpool user."""
    config_path = "/etc/eggpool/config.toml"
    env_path = "/etc/eggpool/env"

    check_cmd = [
        "sudo",
        "-u",
        "eggpool",
        "env",
        f"EGGPOOL_CONFIG={config_path}",
        f"EGGPOOL_ENV={env_path}",
        binary_path_for_validation(),
        "check-config",
        "--config",
        config_path,
    ]
    migrate_cmd = [
        "sudo",
        "-u",
        "eggpool",
        "env",
        f"EGGPOOL_CONFIG={config_path}",
        f"EGGPOOL_ENV={env_path}",
        binary_path_for_validation(),
        "migrate",
        "--config",
        config_path,
    ]

    click.echo("  Running eggpool check-config as eggpool user...")
    check_result = subprocess.run(  # noqa: S603
        check_cmd, capture_output=True, text=True, check=False
    )
    if check_result.returncode != 0:
        if _looks_like_placeholder_credentials(
            check_result.stdout + check_result.stderr
        ):
            click.echo(
                "Configuration is not ready. Run `eggpool onboard` or "
                "`eggpool connect` first.",
                err=True,
            )
        else:
            click.echo(
                f"Error: check-config failed: {check_result.stderr.strip()}",
                err=True,
            )
        sys.exit(1)

    click.echo("  Running eggpool migrate as eggpool user...")
    migrate_result = subprocess.run(  # noqa: S603
        migrate_cmd, capture_output=True, text=True, check=False
    )
    if migrate_result.returncode != 0:
        click.echo(
            f"Error: migrate failed: {migrate_result.stderr.strip()}",
            err=True,
        )
        sys.exit(1)


def _looks_like_placeholder_credentials(text: str) -> bool:
    """Return True if a check-config failure looks like placeholder credentials."""
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in ("replace-me", "your-", "placeholder", "not set")
    )


def binary_path_for_validation() -> str:
    """Return the absolute eggpool binary path for production validation."""
    import shutil  # noqa: PLC0415

    resolved = shutil.which("eggpool")
    if resolved is None:
        return "eggpool"
    return str(Path(resolved).resolve())


def _prepare_user_dirs(deploy_user: DeployUser) -> None:
    """Create the personal config / data / state directories as the user."""
    target_dirs = [
        default_config_dir(),
        default_data_dir(),
        default_state_dir(),
    ]
    for d in target_dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            click.echo(f"  Prepared {d}")
        except OSError as exc:
            click.echo(f"  Warning: could not create {d}: {exc}", err=True)
        _chown_to_user(d, deploy_user)


def _ensure_personal_config_ready(config_path: str) -> None:
    """Refuse --install unless the config has real credentials."""
    from eggpool.config import ensure_config  # noqa: PLC0415

    ensure_config(config_path)

    try:
        config = AppConfig.from_toml(config_path)
    except AggregatorError as exc:
        click.echo(
            f"Error: could not load {config_path}: {exc}",
            err=True,
        )
        sys.exit(1)

    try:
        config.validate_account_credentials()
    except AggregatorError as exc:
        click.echo(
            f"Error: {exc}\n"
            "  Configuration is not ready. Run `eggpool onboard` or "
            "`eggpool connect` first.",
            err=True,
        )
        sys.exit(1)


def _chown_to_user(path: Path, deploy_user: DeployUser) -> None:
    """Best-effort chown of *path* to the resolved deploy user."""
    if os.geteuid() != 0:
        return
    try:
        os.chown(path, deploy_user.uid, deploy_user.gid)
    except (OSError, PermissionError) as exc:
        click.echo(f"  Warning: chown {path} failed: {exc}", err=True)


def _chown(path: Path, owner: str) -> None:
    """Best-effort chown by ``owner:owner`` (POSIX) or fallback."""
    import pwd  # noqa: PLC0415

    try:
        user_name, _, group_name = owner.partition(":")
        user_pw = pwd.getpwnam(user_name)
        group_pw = pwd.getpwnam(group_name or user_name)
    except KeyError:
        return

    with suppress(OSError, PermissionError):
        os.chown(path, user_pw.pw_uid, group_pw.pw_gid)


def _chmod(path: Path, mode: str) -> None:
    """Best-effort chmod by string mode."""
    try:
        path.chmod(int(mode, 8))
    except (OSError, PermissionError, ValueError) as exc:
        click.echo(f"  Warning: chmod {path} {mode} failed: {exc}", err=True)


def _confirm_action(message: str) -> bool:
    """Ask the user to confirm an action; ``y`` proceeds, anything else aborts."""
    click.echo(message)
    return bool(click.confirm("Proceed?", default=False))


@deploy.command("logrotate")
@click.option(
    "--install", is_flag=True, help="Install the logrotate config (requires root)."
)
@click.pass_context
def deploy_logrotate(ctx: click.Context, install: bool) -> None:
    """Print the logrotate config and install instructions.

    With --install, writes the config to /etc/logrotate.d/eggpool and
    validates it via ``logrotate -d``. Many distributions run
    logrotate from cron or a systemd timer rather than a
    ``logrotate.service``; the new flow no longer attempts to restart
    a possibly-missing service unit.
    """
    from eggpool.deploy import build_personal_logrotate

    dynamic_conf = build_personal_logrotate()

    _print_deploy_snippet(
        title="EggPool logrotate configuration",
        target_path="/etc/logrotate.d/eggpool",
        snippet=dynamic_conf,
        extra_steps=[
            "sudo logrotate -d /etc/logrotate.d/eggpool",
        ],
    )

    click.echo("")
    click.echo(
        "Run 'sudo eggpool deploy logrotate --install' to set this up automatically."
    )

    if not install:
        return

    if os.geteuid() != 0:
        click.echo(
            "Error: --install requires root privileges.\n"
            "  Re-run with: sudo eggpool deploy logrotate --install",
            err=True,
        )
        sys.exit(1)

    if not _confirm_action("Install logrotate config to /etc/logrotate.d/eggpool?"):
        click.echo("Aborted.")
        return

    _write_file("/etc/logrotate.d/eggpool", dynamic_conf)
    _validate_logrotate_config("/etc/logrotate.d/eggpool")


def _validate_logrotate_config(path: str) -> None:
    """Best-effort ``logrotate -d`` syntax check for ``path``.

    Prints a warning (no abort) when ``logrotate`` is not on PATH so
    sandboxed installs still succeed. Exits non-zero only when
    ``logrotate -d`` actually reports a config error.
    """
    import shutil  # noqa: PLC0415

    if shutil.which("logrotate") is None:
        click.echo(
            "Warning: logrotate not found; config written but not validated.",
            err=True,
        )
        return

    click.echo("  Verifying config with `logrotate -d`...")
    result = subprocess.run(  # noqa: S603
        ["logrotate", "-d", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        click.echo(
            f"Error: logrotate -d reported a problem:\n{result.stderr.strip()}",
            err=True,
        )
        sys.exit(1)
    click.echo("  Logrotate config installed.")


@deploy.command("cron")
@click.option(
    "--install",
    is_flag=True,
    help="Install the watchdog cron entries into the user's crontab.",
)
@click.option(
    "--uninstall",
    is_flag=True,
    help="Remove EggPool watchdog cron entries from the user's crontab.",
)
@click.option(
    "--interval",
    "interval_minutes",
    default=5,
    type=click.IntRange(1, 59),
    help="Watchdog poll interval in minutes (1-59, default 5).",
)
@click.option(
    "--user",
    "cron_user",
    default=None,
    help="Target a specific user's crontab (defaults to the invoking user).",
)
@click.pass_context
def deploy_cron(
    ctx: click.Context,
    install: bool,
    uninstall: bool,
    interval_minutes: int,
    cron_user: str | None,
) -> None:
    """Install the EggPool watchdog into the user's crontab.

    The watchdog runs ``eggpool ensure-running`` on every reboot and
    every ``--interval`` minutes so the server stays up on systems
    without systemd. Backups are installed separately via
    ``eggpool deploy backup-cron``.

    With --install the watchdog is appended to the user's crontab.
    Under sudo (``SUDO_USER`` set) the install targets the *invoking*
    user's crontab instead of root's, so the personal deployment does
    not depend on root cron. With --uninstall the marked block is
    stripped from the same crontab.
    """
    from eggpool.deploy import build_personal_watchdog_cron
    from eggpool.runtime_paths import default_log_file

    config_path: str = ctx.obj["config_path"]
    binary_path = _resolve_eggpool_binary()
    if binary_path is None:
        click.echo(
            "Error: cannot locate the eggpool binary on PATH.\n"
            "  Install EggPool first (e.g. `uv tool install eggpool`).",
            err=True,
        )
        sys.exit(1)

    config_path_resolved = str(Path(config_path).expanduser().resolve())
    log_path = str(default_log_file())

    cron_user = _resolve_cron_user(cron_user)
    block = build_personal_watchdog_cron(
        binary_path=binary_path,
        config_path=config_path_resolved,
        log_path=log_path,
        interval_minutes=interval_minutes,
    )

    if install:
        _install_cron_block_for_user(
            block=block,
            cron_user=cron_user,
            description=(
                f"watchdog ({interval_minutes}-minute interval) for {cron_user}"
            ),
        )
        click.echo(f"  Watchdog cron installed for user {cron_user}. Logs: {log_path}")
        return

    if uninstall:
        _remove_cron_block_for_user(cron_user=cron_user, description="watchdog")
        click.echo(f"  Watchdog cron removed for user {cron_user}.")
        return

    click.echo("EggPool watchdog (cron fallback for non-systemd systems)")
    click.echo("")
    click.echo("Generated crontab fragment:")
    click.echo("")
    click.echo(block)
    click.echo("Install with:")
    click.echo("  eggpool deploy cron --install")
    click.echo("")
    click.echo("Remove with:")
    click.echo("  eggpool deploy cron --uninstall")


def _resolve_cron_user(explicit: str | None) -> str:
    """Resolve which user's crontab to target.

    Defaults to the invoking user when no ``--user`` is passed; under
    sudo, defaults to ``SUDO_USER`` so the install does not land in
    root's crontab for a personal deployment.
    """
    if explicit is not None:
        return explicit
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if sudo_user:
        return sudo_user
    import pwd  # noqa: PLC0415

    return pwd.getpwuid(os.getuid()).pw_name


def _install_cron_block_for_user(
    *, block: str, cron_user: str, description: str
) -> None:
    """Install a crontab block for *cron_user*.

    Direct root without a ``SUDO_USER`` is rejected because a root
    crontab does not help a personal deployment. Operators are
    instructed to use ``sudo -u <you>`` instead.
    """
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if os.geteuid() == 0 and not sudo_user and cron_user != "root":
        click.echo(
            "Error: refusing to install a personal cron entry into root's "
            "crontab.\n"
            f"  Re-run via: sudo -u {os.environ.get('USER', '<you>')} "
            "eggpool deploy cron --install",
            err=True,
        )
        sys.exit(1)

    from eggpool.deploy import install_cron_block  # noqa: PLC0415

    install_cron_block(block, user=cron_user)
    click.echo(f"  Installed {description}.")


def _remove_cron_block_for_user(*, cron_user: str, description: str) -> None:
    """Strip EggPool cron blocks from *cron_user*'s crontab."""
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if os.geteuid() == 0 and not sudo_user and cron_user != "root":
        click.echo(
            "Error: refusing to remove cron entries from root's crontab.\n"
            f"  Re-run via: sudo -u {os.environ.get('USER', '<you>')} "
            "eggpool deploy cron --uninstall",
            err=True,
        )
        sys.exit(1)

    from eggpool.deploy import remove_cron_block  # noqa: PLC0415

    remove_cron_block(user=cron_user)
    click.echo(f"  Removed {description} entries from {cron_user}'s crontab.")


@deploy.command("backup-cron")
@click.option(
    "--install",
    is_flag=True,
    help="Install the daily backup script and cron entry.",
)
@click.option(
    "--uninstall",
    is_flag=True,
    help="Remove the daily backup cron entry and script.",
)
@click.option(
    "--production",
    is_flag=True,
    help="Production mode: install /etc/cron.d/eggpool-backup owned by root.",
)
@click.option(
    "--user",
    "cron_user",
    default=None,
    help="Target a specific user's crontab (defaults to the invoking user).",
)
@click.pass_context
def deploy_backup_cron(
    ctx: click.Context,
    install: bool,
    uninstall: bool,
    production: bool,
    cron_user: str | None,
) -> None:
    """Install the EggPool daily backup as a cron job.

    In personal mode the script and a user-crontab entry are added so
    daily snapshots land under ``~/backups/eggpool``. In production
    mode the script lives at ``/usr/local/bin/eggpool-backup`` and the
    cron entry at ``/etc/cron.d/eggpool-backup`` writing to
    ``/var/backups/eggpool``.

    The personal backup script requires ``sqlite3`` on PATH. ``deploy
    backup-cron --install`` warns (does not abort) if the binary is
    missing because some operators may want to schedule the cron entry
    even before ``sqlite3`` is installed.
    """
    from eggpool.deploy import (
        CRON_BACKUP_FILE,
        CRON_BACKUP_SCRIPT,
        build_personal_backup_block,
        build_personal_backup_script,
    )

    config_path: str = ctx.obj["config_path"]
    data_dir = str(_resolve_data_dir())
    db_path = str(Path(data_dir) / "usage.sqlite3")

    dynamic_script = build_personal_backup_script(
        config_path=config_path, db_path=db_path
    )

    if production:
        if install:
            _install_production_backup_cron(dynamic_script=CRON_BACKUP_SCRIPT)
        elif uninstall:
            _uninstall_production_backup_cron()
        else:
            click.echo("EggPool production backup cron:")
            click.echo("")
            click.echo(CRON_BACKUP_FILE)
            click.echo("")
            click.echo(CRON_BACKUP_SCRIPT)
        return

    if install:
        if os.geteuid() != 0 and not os.environ.get("SUDO_USER"):
            click.echo(
                "Error: personal --install requires running under your user.\n"
                "  Re-run without sudo, or via: sudo -u $USER eggpool "
                "deploy backup-cron --install",
                err=True,
            )
            sys.exit(1)
        cron_user = _resolve_cron_user(cron_user)
        _install_personal_backup_cron(
            script=dynamic_script,
            cron_user=cron_user,
        )
        return

    if uninstall:
        cron_user = _resolve_cron_user(cron_user)
        _uninstall_personal_backup_cron(cron_user=cron_user)
        return

    click.echo("EggPool daily backup (personal use)")
    click.echo("")
    click.echo("Backup script:")
    click.echo("")
    click.echo(dynamic_script)
    click.echo("")
    click.echo("User crontab fragment:")
    click.echo("")
    cron_user = _resolve_cron_user(cron_user)
    binary_path = _resolve_eggpool_binary() or "/usr/local/bin/eggpool-backup"
    click.echo(build_personal_backup_block(binary_path))


def _install_personal_backup_cron(*, script: str, cron_user: str) -> None:
    """Install the personal backup script and crontab entry."""
    import shutil  # noqa: PLC0415

    from eggpool.deploy import build_personal_backup_block

    target_script = "/usr/local/bin/eggpool-backup"
    if os.geteuid() != 0 and cron_user != pwd_get_username():
        click.echo(
            "Error: writing /usr/local/bin requires root or running as the "
            "target user.",
            err=True,
        )
        sys.exit(1)

    if shutil.which("sqlite3") is None:
        click.echo(
            "Warning: sqlite3 is not on PATH. The backup script requires it.",
            err=True,
        )

    _write_file(target_script, script)
    subprocess.run(  # noqa: S603
        ["chmod", "+x", target_script],
        check=True,
    )
    block = build_personal_backup_block(target_script)
    from eggpool.deploy import install_cron_block  # noqa: PLC0415

    install_cron_block(block, user=cron_user)
    click.echo(f"  Backup script installed at {target_script}.")
    click.echo(f"  Backup cron entry installed for {cron_user}.")


def _uninstall_personal_backup_cron(*, cron_user: str) -> None:
    """Remove the personal backup script and crontab entry."""
    from eggpool.deploy import remove_cron_block  # noqa: PLC0415

    target_script = Path("/usr/local/bin/eggpool-backup")
    if target_script.exists():
        target_script.unlink()
        click.echo(f"  Removed {target_script}.")
    remove_cron_block(user=cron_user)
    click.echo(f"  Removed backup cron entries from {cron_user}'s crontab.")


def pwd_get_username() -> str:
    """Return the current process username via :mod:`pwd`."""
    import pwd  # noqa: PLC0415

    return pwd.getpwuid(os.getuid()).pw_name


def _install_production_backup_cron(*, dynamic_script: str) -> None:
    """Install the production backup cron at ``/etc/cron.d/eggpool-backup``."""
    if os.geteuid() != 0:
        click.echo(
            "Error: production --install requires root.",
            err=True,
        )
        sys.exit(1)

    target_script = Path("/usr/local/bin/eggpool-backup")
    target_cron = Path("/etc/cron.d/eggpool-backup")

    if not target_script.exists():
        target_script.write_text(dynamic_script, encoding="utf-8")
        target_script.chmod(0o755)
        click.echo(f"  Wrote {target_script}.")

    from eggpool.deploy import CRON_BACKUP_FILE  # noqa: PLC0415

    target_cron.write_text(CRON_BACKUP_FILE, encoding="utf-8")
    target_cron.chmod(0o644)
    click.echo(f"  Wrote {target_cron}.")


def _uninstall_production_backup_cron() -> None:
    """Remove the production backup script and cron.d entry."""
    if os.geteuid() != 0:
        click.echo(
            "Error: production --uninstall requires root.",
            err=True,
        )
        sys.exit(1)

    for path in (
        Path("/etc/cron.d/eggpool-backup"),
        Path("/usr/local/bin/eggpool-backup"),
    ):
        if path.exists():
            path.unlink()
            click.echo(f"  Removed {path}.")


@deploy.command("all")
@click.option(
    "--install", is_flag=True, help="Install all deployment files (requires root)."
)
@click.pass_context
def deploy_all(ctx: click.Context, install: bool) -> None:
    """Print every deployment snippet in sequence.

    With --install, installs systemd unit, logrotate config, and
    watchdog cron entry. Backup cron lives under its own
    ``eggpool deploy backup-cron`` command.
    """
    ctx.invoke(deploy_systemd, install=install)
    click.echo("")
    click.echo("=" * 72)
    click.echo("")
    ctx.invoke(deploy_logrotate, install=install)
    click.echo("")
    click.echo("=" * 72)
    click.echo("")
    ctx.invoke(deploy_cron, install=install)
    click.echo("")
    click.echo("=" * 72)
    click.echo("")
    click.echo(
        "Note: nightly backups are configured separately via "
        "'eggpool deploy backup-cron --install'."
    )


@cli.command()
@click.pass_context
def rehash(ctx: click.Context) -> None:
    """Restart the server to apply configuration changes."""
    ctx.invoke(restart, timeout=10.0)


@cli.group()
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Statistics / observability subcommands."""


@stats.command("recompute-costs")
@click.option(
    "--dry-run/--apply",
    default=True,
    help="Dry-run (default) only reports the changes; --apply writes them.",
)
@click.option(
    "--limit",
    default=None,
    type=int,
    help="Maximum number of historical requests to recompute.",
)
@click.pass_context
def stats_recompute_costs(
    ctx: click.Context,
    dry_run: bool,
    limit: int | None,
) -> None:
    """Recompute cost_microdollars on historical requests.

    Walks the requests table in started_at DESC order, recomputes
    cost from the latest price snapshot per (model_id, provider_id),
    and reports / applies the change. Useful after upgrading the
    pricing resolver to fix inflated totals on cached-token-heavy
    models (e.g. MiMo 2.5).
    """
    import asyncio

    config_path_raw = ctx.obj.get("config_path") if ctx.obj else None
    config = AppConfig.from_toml(config_path_raw) if config_path_raw else None
    if config is None:
        click.echo(
            "No config available; pass --config or set EGGPOOL_CONFIG.", err=True
        )
        sys.exit(2)

    async def _runner() -> int:
        from eggpool.cost_recompute import recompute_request_costs

        db = Database(
            path=config.database.path,
            busy_timeout_ms=config.database.busy_timeout_ms,
            wal=config.database.wal,
            synchronous=config.database.synchronous,
        )
        await db.connect()
        try:
            result = await recompute_request_costs(
                db,
                limit=limit,
                dry_run=dry_run,
            )
        finally:
            await db.disconnect()
        prefix = "DRY-RUN" if dry_run else "APPLY"
        click.echo(
            f"{prefix}: scanned {result.scanned} rows, "
            f"updated {result.updated}, "
            f"skipped {result.skipped_unchanged}, "
            f"unchanged cost total {result.cost_total_microdollars:,} μ$"
        )
        if result.changed_rows:
            click.echo("")
            click.echo(_format_change_rows(result.changed_rows))
        return 0

    sys.exit(asyncio.run(_runner()))


@stats.command("transcoding")
@click.option(
    "--period",
    default="24h",
    help="Time period: 1h, 24h (default), 7d, or 30d.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output as JSON.",
)
@click.pass_context
def stats_transcoding(
    ctx: click.Context,
    period: str,
    as_json: bool,
) -> None:
    """Show protocol transcoding statistics."""
    import asyncio

    config_path_raw = ctx.obj.get("config_path") if ctx.obj else None
    config = AppConfig.from_toml(config_path_raw) if config_path_raw else None
    if config is None:
        click.echo(
            "No config available; pass --config or set EGGPOOL_CONFIG.", err=True
        )
        sys.exit(2)

    async def _runner() -> int:
        from eggpool.db.connection import Database
        from eggpool.stats import StatsService

        db = Database(
            path=config.database.path,
            busy_timeout_ms=config.database.busy_timeout_ms,
            wal=config.database.wal,
            synchronous=config.database.synchronous,
        )
        await db.connect()
        try:
            svc = StatsService(db)
            stats_data = await svc.get_transcoding_stats(period)
        finally:
            await db.disconnect()

        if as_json:
            import json as json_mod

            serializable = dict(stats_data)
            if "per_direction" in serializable:
                serializable["per_direction"] = {
                    f"{k[0]}→{k[1]}": v
                    for k, v in serializable["per_direction"].items()
                }
            click.echo(json_mod.dumps(serializable, indent=2, default=str))
            return 0

        total = stats_data.get("total", 0)
        native_count = stats_data.get("native_count", 0)
        transcoded_count = stats_data.get("transcoded_count", 0)
        per_direction = stats_data.get("per_direction", {})

        click.echo(f"Period: {period}")
        click.echo(f"Total requests: {total:,}")
        click.echo(f"Native (no transcoding): {native_count:,}")
        click.echo(f"Transcoded: {transcoded_count:,}")

        if per_direction:
            click.echo("")
            click.echo(f"{'Direction':<30} {'Count':>10}")
            click.echo(f"{'-' * 30} {'-' * 10}")
            for (client, upstream), count in sorted(
                per_direction.items(), key=lambda x: x[1], reverse=True
            ):
                direction = f"{client}→{upstream}"
                click.echo(f"{direction:<30} {count:>10,}")

        return 0

    sys.exit(asyncio.run(_runner()))


def _format_change_rows(rows: list[dict[str, Any]]) -> str:
    """Format recompute-costs output as a small text table."""
    headers = ("model", "provider", "old_μ$", "new_μ$", "Δ μ$")
    widths = (28, 14, 12, 12, 12)
    parts = [
        "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)),
        "  ".join("-" * w for w in widths),
    ]
    for row in rows:
        cells = (
            str(row.get("model_id", ""))[: widths[0]],
            str(row.get("provider_id", ""))[: widths[1]],
            f"{int(row.get('old_cost_microdollars', 0)):,}",
            f"{int(row.get('new_cost_microdollars', 0)):,}",
            f"{int(row.get('delta_microdollars', 0)):+,}",
        )
        parts.append("  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)))
    return "\n".join(parts)


def _update_server_config(config_path: str, key: str, value: str) -> None:
    """Update a [server] key in the TOML config file."""
    from pathlib import Path

    path = Path(config_path)

    lines = path.read_text(encoding="utf-8").splitlines()
    if key == "port":
        try:
            parsed_port = int(value)
        except ValueError:
            click.echo(f"Invalid port: {value!r} is not an integer.", err=True)
            sys.exit(1)
        if not 0 <= parsed_port <= 65535:
            click.echo("Invalid port: expected a value from 0 to 65535.", err=True)
            sys.exit(1)
        toml_value = str(parsed_port)
    else:
        toml_value = render_toml_string(value)

    result = update_section_value(lines, "server", key, toml_value)
    if not result.key_found:
        click.echo(f"Key '{key}' not found in [server] section.", err=True)
        sys.exit(1)

    path.write_text("\n".join(result.lines) + "\n", encoding="utf-8")


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
    from eggpool.providers.connect import restart_server

    config_path: str = ctx.obj["config_path"]
    _update_server_config(config_path, key, value)
    click.echo(f"Set {key} = {value} in {config_path}.")

    if restart_server(config_path):
        click.echo("Server restarted.")
    else:
        click.echo("Server is not running.")


@cli.group()
def dashboard() -> None:
    """Dashboard configuration commands."""


def _read_dashboard_public(config_path: str) -> bool:
    """Read the current dashboard.public value from config."""
    raw = _load_raw_config(config_path)
    dashboard = _get_section(raw, "dashboard")
    public = dashboard.get("public", True)
    return public if isinstance(public, bool) else True


def _write_dashboard_public(config_path: str, public: bool) -> None:
    """Write the dashboard.public value to config."""
    from pathlib import Path

    path = Path(config_path)

    lines = path.read_text(encoding="utf-8").splitlines()
    result = update_section_value(
        lines,
        "dashboard",
        "public",
        "true" if public else "false",
        insert_missing_key=True,
        append_missing_section=True,
    )
    path.write_text("\n".join(result.lines) + "\n", encoding="utf-8")


@dashboard.command("public")
@click.option(
    "--on/--off",
    "set_public",
    default=None,
    help="Set dashboard public access explicitly (omit to toggle).",
)
@click.pass_context
def dashboard_public(ctx: click.Context, set_public: bool | None) -> None:
    """Toggle dashboard public access.

    Without options, shows the current setting and toggles it.
    With --on/--off, sets the value explicitly.
    """
    from eggpool.providers.connect import restart_server

    config_path: str = ctx.obj["config_path"]
    current = _read_dashboard_public(config_path)

    new_value = (not current) if set_public is None else set_public

    _write_dashboard_public(config_path, new_value)

    if new_value:
        click.echo("Dashboard is now public (no API key required).")
    else:
        click.echo("Dashboard now requires API key authentication.")

    if restart_server(config_path):
        click.echo("Server restarted.")
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

    async def _run_migrations(db: Database) -> None:
        runner = MigrationRunner(db)
        await runner.run()
        click.echo("Migrations completed successfully")

    _run_with_database(config, _run_migrations)


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

    async def _run_models_refresh(db: Database) -> None:
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
        outbound_manager = OutboundClientManager(config=config.network)
        try:
            outbound_client = await outbound_manager.get_client()
            catalog = CatalogService(
                config,
                registry,
                db,
                client_pool,
                outbound_client=outbound_client,
            )
            await catalog.attach_pricing_resolvers()
            await catalog.refresh()
            count = catalog.cache.model_count
            click.echo(f"Refreshed catalog: {count} models found")
        finally:
            await outbound_manager.aclose()
            await client_pool.close()

    _run_with_database(config, _run_models_refresh)


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
        priority = _get_priority_for_account(config, acct.name)
        env_set = "yes" if os.environ.get(acct.api_key_env) else "no"
        click.echo(
            f"  {acct.name}: provider={provider_id}, priority={priority}, "
            f"enabled={acct.enabled}, weight={acct.weight}, "
            f"api_key_env={acct.api_key_env} (set={env_set})"
        )

    click.echo(f"\nTotal accounts: {len(config.all_accounts())}")


@accounts.command("explain")
@click.option(
    "--model",
    "model_id",
    required=True,
    help="Model id to evaluate (e.g. gpt-4, claude-3-5-sonnet).",
)
@click.option(
    "--provider",
    "provider_id",
    default=None,
    help="Restrict the explanation to one provider.",
)
@click.option(
    "--protocol",
    "protocol",
    default=None,
    help=(
        "Restrict the explanation to accounts declaring this protocol "
        "(default: no protocol filter)."
    ),
)
@click.pass_context
def accounts_explain(
    ctx: click.Context,
    model_id: str,
    provider_id: str | None,
    protocol: str | None,
) -> None:
    """Explain why each account is or is not eligible for ``--model``.

    Reads the durable catalog state from SQLite (after running
    migrations so a fresh install still works) and feeds it into the
    same ``Router.explain_account_eligibility`` helper the coordinator
    uses, so the operator sees the real model/account picture instead
    of an empty in-memory cache.
    """
    from eggpool.accounts.registry import AccountRegistry
    from eggpool.catalog.cache import ModelCatalogCache
    from eggpool.models.config import AppConfig
    from eggpool.quota.estimation import QuotaEstimator
    from eggpool.routing.router import Router

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

    async def _run_explain(db: Database) -> None:
        runner = MigrationRunner(db)
        await runner.run()

        registry = AccountRegistry(config)
        cache = ModelCatalogCache()
        cache.set_config(config)
        await cache.hydrate_from_db(db)

        class _CatalogShim:
            """Protocol-compatible wrapper exposing the loaded cache.

            ``Router`` reads ``catalog.cache`` to answer eligibility
            questions; constructing a full ``CatalogService`` here
            would require a provider client pool, outbound manager,
            and live refresh path. The shim keeps ``accounts explain``
            to a single SQLite read so the command remains usable on
            hosts where the outbound network is offline.
            """

            def __init__(self, inner: ModelCatalogCache) -> None:
                self.cache = inner

        catalog = _CatalogShim(cache)
        quota_estimator = QuotaEstimator()
        router = Router(
            registry,
            catalog,  # type: ignore[reportArgumentType]
            quota_estimator=quota_estimator,
        )
        rows = await router.explain_account_eligibility(
            model_id=model_id,
            provider_id=provider_id,
            protocol=protocol,
            transcode_eligibility=None,
        )
        click.echo(f"Account eligibility for model {model_id!r}:")
        if not rows:
            click.echo("  (no configured accounts)")
            return
        name_w = max(len("Account"), *(len(r["account_name"]) for r in rows))
        elig_w = max(len("Eligible"), 3)
        reason_w = max(len("Reason"), *(len(r["reason_code"]) for r in rows))
        header = (
            f"  {'Account':<{name_w}}  {'Eligible':<{elig_w}}  "
            f"{'Reason':<{reason_w}}  Detail"
        )
        click.echo(header)
        click.echo(f"  {'-' * name_w}  {'-' * elig_w}  {'-' * reason_w}  ------")
        for row in rows:
            click.echo(
                f"  {row['account_name']:<{name_w}}  "
                f"{('yes' if row['eligible'] else 'no'):<{elig_w}}  "
                f"{row['reason_code']:<{reason_w}}  {row['reason_detail']}"
            )

    _run_with_database(config, _run_explain)


def _get_provider_for_account(config: AppConfig, account_name: str) -> str:
    """Return the provider ID for an account."""
    for provider_id, provider_cfg in config.providers.items():
        for acct in provider_cfg.accounts:
            if acct.name == account_name:
                return provider_id
    return "unknown"


def _get_priority_for_account(config: AppConfig, account_name: str) -> int:
    """Return the routing priority for an account's provider, or 0."""
    provider_id = _get_provider_for_account(config, account_name)
    if provider_id == "unknown":
        return 0
    provider_cfg = config.providers.get(provider_id)
    if provider_cfg is None:
        return 0
    return int(provider_cfg.routing_priority)


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

    async def _run_vacuum(db: Database) -> None:
        await db.vacuum()
        click.echo("Database vacuum completed successfully")

    _run_with_database(config, _run_vacuum)


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

    from eggpool.providers.connect import restart_server

    config_path: str = ctx.obj["config_path"]
    current_version, latest_version, error = async_check_for_update()
    click.echo(f"Current version: {current_version}")

    if error:
        click.echo(f"Error checking for updates: {error}", err=True)
        sys.exit(1)

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

    # Determine update command based on install method
    method = _detect_install_method()
    repo = "eggstack/eggpool"
    git_target = f"git+https://github.com/{repo}.git@v{latest_version}"

    if from_source:
        # Force update from git source — use the method-appropriate installer
        if method == "source":
            cmd = ["uv", "sync", "--no-dev"]
        elif method == "pipx":
            cmd = ["pipx", "install", git_target]
        elif method == "uv-tool":
            cmd = ["uv", "tool", "install", git_target]
        else:
            cmd = [sys.executable, "-m", "pip", "install", git_target]
    elif method == "source":
        # Source checkout — find repo root and run uv sync
        repo_root = Path(__file__).resolve().parent.parent.parent
        cmd = ["uv", "sync", "--no-dev", "--directory", str(repo_root)]
    elif method == "pipx":
        cmd = ["pipx", "upgrade", "eggpool"]
    elif method == "uv-tool":
        cmd = ["uv", "tool", "install", f"eggpool=={latest_version}"]
    else:
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

    if restart_server(config_path):
        click.echo("Server restarted.")
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
    from eggpool import runtime

    return runtime.read_pid()


def _is_process_running(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    from eggpool import runtime

    return runtime.is_process_running(pid)


def _wait_for_exit(pid: int, timeout: float = 10.0) -> bool:
    """Wait for a process to exit. Returns True if exited, False on timeout."""
    from eggpool import runtime

    return runtime.wait_for_exit(pid, timeout)


@cli.command()
@click.option("--timeout", default=10.0, help="Seconds to wait for shutdown.")
@click.pass_context
def stop(ctx: click.Context, timeout: float) -> None:
    """Stop the running server."""
    from eggpool import runtime

    pid = _read_pid()
    if pid is None:
        click.echo("Server is not running (no PID file found).")
        return

    if not _is_process_running(pid):
        click.echo("Server is not running (stale PID file).")
        runtime.clear_pid_file()
        return

    click.echo(f"Stopping server (PID {pid})...")

    if not runtime.send_sigterm(pid):
        click.echo("Error sending signal.", err=True)
        runtime.clear_pid_file()
        return

    if _wait_for_exit(pid, timeout):
        runtime.clear_pid_file()
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

    Reads the supervisor PID written by ``eggpool serve``, sends
    SIGTERM, waits for clean exit, then spawns a new supervisor. The
    new supervisor writes its own PID to the file before
    ``Granian.serve()`` runs. The legacy ``eggpool rehash`` command
    delegates to this same path.
    """
    from eggpool import runtime

    config_path: str = ctx.obj["config_path"]

    pid = _read_pid()
    if pid is not None and _is_process_running(pid):
        click.echo(f"Stopping server (PID {pid})...")
        if not runtime.send_sigterm(pid):
            click.echo("Error sending signal.", err=True)
            return
        if not _wait_for_exit(pid, timeout):
            click.echo(
                f"Server did not stop within {timeout}s. "
                "Try 'kill -9' or check process.",
                err=True,
            )
            return
        click.echo("Server stopped.")
    else:
        if pid is not None and not _is_process_running(pid):
            runtime.clear_pid_file()

    click.echo("Starting server...")
    runtime.start_server(config_path)
    click.echo("Server started.")


@cli.command("init-config")
@click.argument("target", required=False, type=click.Path())
@click.option("--force", is_flag=True, help="Overwrite existing config file.")
@click.pass_context
def init_config(ctx: click.Context, target: str | None, force: bool) -> None:
    """Write config.example.toml into the current directory (or TARGET).

    For fresh installs, prefer 'eggpool onboard' which handles
    config creation, API key generation, and provider setup.
    """
    from importlib.resources import as_file, files

    ref = files("eggpool._share").joinpath("config.example.toml")
    with as_file(ref) as source_path:
        if not source_path.exists():
            click.echo("Error: bundled config.example.toml not found", err=True)
            sys.exit(1)

        target_path = Path(target) if target else Path("config.toml")

        if target_path.exists() and not force:
            click.echo(
                f"Warning: {target_path} already exists.\n"
                "This will overwrite your configuration.\n"
                "For a fresh config, use --force.\n"
                "For provider setup, use 'eggpool onboard' instead.",
                err=True,
            )
            sys.exit(1)

        import shutil

        shutil.copy2(source_path, target_path)
        click.echo(f"Config written to {target_path}")


@cli.command("help")
@click.pass_context
def help_command(ctx: click.Context) -> None:
    """Show this help message and exit."""
    if ctx.parent is not None:
        click.echo(ctx.parent.get_help())
    else:
        click.echo(ctx.get_help())


@cli.command()
@click.pass_context
def version(ctx: click.Context) -> None:
    """Print the installed version and exit."""
    from importlib.metadata import version as get_version

    try:
        click.echo(get_version("eggpool"))
    except Exception:
        click.echo("unknown")


@cli.command("croncheck")
@click.pass_context
def croncheck(ctx: click.Context) -> None:
    """Lightweight check: exit 0 if server is running, exit 1 if not.

    Fast-path command; normally intercepted by the bootstrap in
    :mod:`eggpool.cli` before Click runs. The full-CLI body here is a
    fallback used when the command is dispatched through
    ``python -m eggpool.cli_full`` directly.

    Designed for cron jobs that should restart a stopped server.
    Uses only the PID file and a kill probe — no network I/O.
    """
    from eggpool.runtime_paths import default_pid_file, read_pid_file

    pid_file = default_pid_file()
    if not pid_file.exists():
        sys.exit(1)

    pid = read_pid_file(pid_file)
    if pid is None:
        sys.exit(1)

    if _is_process_running(pid):
        sys.exit(0)
    sys.exit(1)


@cli.command("ensure-running")
@click.pass_context
def ensure_running(ctx: click.Context) -> None:
    """Start the server if it is not already running.

    Fast-path command; normally intercepted by the bootstrap in
    :mod:`eggpool.cli` before Click runs. The full-CLI body here is a
    fallback used when the command is dispatched through
    ``python -m eggpool.cli_full`` directly.
    """
    from eggpool import fastcli

    config_path: str = ctx.obj["config_path"]
    sys.exit(fastcli._run_ensure_running(config_path))  # type: ignore[reportPrivateUsage] - fallback path


@cli.command("runtime-status")
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output raw JSON instead of a formatted summary.",
)
@click.pass_context
def runtime_status(ctx: click.Context, output_json: bool) -> None:
    """Print a compact runtime health summary from the running server.

    Calls the local ``/api/stats/runtime`` endpoint and displays
    process, memory, background-task, and database status.  Requires
    the server to be running; exits non-zero otherwise.

    Use ``--json`` for machine-readable output.
    """
    import json
    import urllib.error
    import urllib.request

    from eggpool.deploy_user import resolve_config_path

    config_path: str = ctx.obj["config_path"]
    resolved = resolve_config_path(cli_value=config_path)
    from eggpool.models.config import AppConfig

    config = AppConfig.from_toml(str(resolved))

    host = config.server.host
    port = config.server.port
    api_key = config.server.resolved_api_key

    # Normalize bind address for localhost probe
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"

    url = f"http://{host}:{port}/api/stats/runtime"
    req = urllib.request.Request(url, method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        click.echo(f"Server returned HTTP {exc.code}", err=True)
        sys.exit(1)
    except (urllib.error.URLError, OSError) as exc:
        click.echo(f"Cannot reach server at {url}: {exc}", err=True)
        sys.exit(1)

    if output_json:
        click.echo(json.dumps(data, indent=2))
    else:
        _print_runtime_status(data)


def _print_runtime_status(data: dict[str, Any]) -> None:
    """Format and print a compact runtime status summary."""
    server = cast("dict[str, Any]", data.get("server", {}))
    memory = cast("dict[str, Any]", data.get("memory", {}))
    processes = cast("dict[str, Any]", data.get("processes", {}))
    db_info = cast("dict[str, Any]", data.get("db", {}))
    routing = cast("dict[str, Any]", data.get("routing_runtime", {}))
    bg_tasks = cast("list[dict[str, Any]]", data.get("background_tasks", []))
    probe_errors = cast("list[str]", data.get("probe_errors", []))

    click.echo("=== EggPool Runtime Status ===")
    click.echo()

    # Server
    pid = server.get("pid", "?")
    uptime = server.get("uptime_seconds", "?")
    threads = server.get("configured_server_threads", "?")
    pyver = server.get("python_version", "?")
    click.echo(f"  PID:            {pid}")
    click.echo(f"  Uptime:         {_format_duration(uptime)}")
    click.echo(f"  Server Threads: {threads}")
    click.echo(f"  Python:         {pyver}")

    # Memory
    click.echo()
    rss = memory.get("rss_bytes")
    vms = memory.get("vms_bytes")
    fds = memory.get("open_fd_count")
    thr = memory.get("thread_count")
    click.echo(f"  RSS:            {_format_bytes(rss)}")
    click.echo(f"  VMS:            {_format_bytes(vms)}")
    click.echo(f"  Open FDs:       {fds if fds is not None else 'N/A'}")
    click.echo(f"  Threads:        {thr if thr is not None else 'N/A'}")

    # Process count
    click.echo()
    observed = processes.get("eggpool_process_count")
    expected = processes.get("expected_worker_process_count")
    warning = processes.get("process_count_warning", False)
    click.echo(f"  EggPool Processes: {observed} (expected: {expected})")
    if warning:
        click.echo("  *** WARNING: Process count exceeds expected ***")

    # Load average
    load = cast("dict[str, Any]", data.get("load", {}))
    if load:
        click.echo()
        load_available = load.get("available", False)
        load_1m = load.get("load_1m")
        load_5m = load.get("load_5m")
        load_15m = load.get("load_15m")
        norm_1m = load.get("normalized_1m")
        cpu_count = load.get("cpu_count")
        if (
            load_available
            and load_1m is not None
            and load_5m is not None
            and load_15m is not None
        ):
            load_str = f"{float(load_1m):.2f}"
            if norm_1m is not None and cpu_count:
                load_str += f" ({float(norm_1m):.2f}/core, {cpu_count} CPUs)"
            else:
                load_str += f" (5m {float(load_5m):.2f}, 15m {float(load_15m):.2f})"
            click.echo(f"  Load (1m):       {load_str}")
        else:
            click.echo("  Load (1m):       N/A")

    # Dispatch overhead
    dispatch = cast("dict[str, Any]", data.get("dispatch_overhead", {}))
    if dispatch:
        click.echo()
        avg_ms = dispatch.get("avg_ms")
        p95_ms = dispatch.get("p95_ms")
        p99_ms = dispatch.get("p99_ms")
        max_ms = dispatch.get("max_ms")
        sample_count = dispatch.get("sample_count", 0)
        window_size = dispatch.get("window_size", 100)
        if avg_ms is None:
            click.echo(
                f"  Dispatch Overhead: N/A ({sample_count}/{window_size} attempts)"
            )
        else:
            click.echo(
                f"  Dispatch Overhead: avg {_format_ms(avg_ms)} ms "
                f"(p95 {_format_ms(p95_ms)}, p99 {_format_ms(p99_ms)}, "
                f"max {_format_ms(max_ms)}, n={sample_count})"
            )

    # Background tasks
    click.echo()
    click.echo("  Background Tasks:")
    if not bg_tasks:
        click.echo("    (none)")
    for task in bg_tasks:
        name = task.get("name", "?")
        running = task.get("running", False)
        restarts = task.get("restart_count", 0)
        status = "running" if running else "STOPPED"
        line = f"    {name}: {status}"
        if restarts > 0:
            line += f" (restarts: {restarts})"
        click.echo(line)

    # Database
    click.echo()
    db_path = db_info.get("path", ":memory:")
    wal_size = db_info.get("wal_size_bytes")
    file_size = db_info.get("file_size_bytes")
    click.echo(f"  DB Path:        {db_path or ':memory:'}")
    click.echo(f"  DB Size:        {_format_bytes(file_size)}")
    click.echo(f"  WAL Size:       {_format_bytes(wal_size)}")

    # Routing
    click.echo()
    pending = routing.get("pending_count")
    reservations = routing.get("active_reservations_count")
    reserved = routing.get("reserved_microdollars")
    click.echo(f"  Pending Requests:   {pending if pending is not None else 'N/A'}")
    click.echo(
        f"  Active Reservations: {reservations if reservations is not None else 'N/A'}"
    )
    click.echo(f"  Reserved (μ$):      {reserved if reserved is not None else 'N/A'}")

    # Network diagnostics
    outbound = cast("dict[str, Any]", data.get("outbound_client", {}))
    provider_pool = cast("dict[str, Any]", data.get("provider_client_pool", {}))
    dns = cast("dict[str, Any]", data.get("dns_cache", {}))
    click.echo()
    click.echo("  Network:")
    dns_enabled = dns.get("enabled", False)
    dns_size = dns.get("size", 0)
    dns_max = dns.get("max_entries")
    dns_hits = dns.get("hits", 0)
    dns_misses = dns.get("misses", 0)
    dns_suppression_rate = dns.get("dns_suppression_rate", 0.0)
    dns_suppression_pct = (
        f"{dns_suppression_rate * 100:.1f}%" if dns_suppression_rate else "—"
    )
    dns_resolver_calls = dns.get("resolver_calls_total", 0)
    dns_resolver_errors = dns.get("resolver_errors_total", 0)
    ob_builds = outbound.get("build_count", 0)
    ob_requests = outbound.get("request_count", 0)
    ob_errors = outbound.get("error_count", 0)
    provider_builds = provider_pool.get("build_count", 0)
    provider_list = provider_pool.get("providers", {})
    click.echo(f"    DNS cache:         {'enabled' if dns_enabled else 'disabled'}")
    dns_entries_str = f"{dns_size}" + (f" / {dns_max}" if dns_max is not None else "")
    click.echo(f"    DNS entries:       {dns_entries_str}")
    click.echo(f"    DNS suppression:   {dns_suppression_pct}")
    click.echo(f"    Resolver calls:    {dns_resolver_calls}")
    click.echo(f"    Cache hits:        {dns_hits}")
    click.echo(f"    Owner misses:      {dns_misses}")
    click.echo(f"    DNS errors:        {dns_resolver_errors}")
    click.echo(f"    Outbound builds:   {ob_builds}")
    click.echo(f"    Outbound requests: {ob_requests}")
    click.echo(f"    Outbound errors:   {ob_errors}")
    click.echo(f"    Provider clients:  {provider_builds}")
    if provider_list:
        for pid in sorted(provider_list):
            click.echo(f"      {pid}: {provider_list[pid]}")

    # DNS cache hosts (if any)
    hosts = dns.get("hosts", [])
    if hosts:
        click.echo("    DNS cache entries:")
        for entry in hosts:
            host = entry.get("host", "?")
            family = entry.get("family", "?")
            state = entry.get("state", "?")
            expires = entry.get("expires_in_seconds", 0)
            stale = entry.get("stale_available", False)
            err_kind = entry.get("last_error_kind")
            parts = [f"{host} ({family}) state={state}"]
            parts.append(f"expires={expires:.0f}s")
            if stale:
                parts.append("stale_ok")
            if err_kind:
                parts.append(f"error={err_kind}")
            click.echo(f"      {' '.join(parts)}")

    # Probe errors
    if probe_errors:
        click.echo()
        click.echo("  Probe Errors:")
        for err in probe_errors:
            click.echo(f"    - {err}")

    click.echo()


def _format_duration(seconds: object) -> str:
    """Format seconds into a human-readable duration string."""
    if not isinstance(seconds, (int, float)):
        return str(seconds)
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m"


def _format_ms(value: object) -> str:
    """Format a millisecond value with sub-ms precision for small numbers."""
    if value is None:
        return "—"
    if not isinstance(value, (int, float)):
        return str(value)
    number = float(value)
    if number < 1:
        return f"{number:.2f}"
    if number < 10:
        return f"{number:.1f}"
    return f"{number:.0f}"


def _format_bytes(value: object) -> str:
    """Format bytes into a human-readable size string."""
    if value is None:
        return "N/A"
    if not isinstance(value, (int, float)):
        return str(value)
    b = int(value)
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    if b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    return f"{b / (1024 * 1024 * 1024):.2f} GB"


@cli.command()
@click.option(
    "--output-dir",
    "output_dir",
    default=None,
    help="Override the default backup directory.",
    type=click.Path(file_okay=False, dir_okay=True),
)
@click.pass_context
def backup(ctx: click.Context, output_dir: str | None) -> None:
    """Create a timestamped zip backup of the config and database.

    The backup includes the live config, the optional ``.env`` file next
    to it, the SQLite database (with its WAL and SHM sidecars when
    present), and a META block describing the archive's contents.

    When the database is a regular file (not ``:memory:``), an
    in-process SQLite snapshot is used for a consistent backup even if
    the server is running.

    Backups are stored under ``$XDG_BACKUP_HOME/eggpool`` or, when that
    variable is unset, ``$HOME/backups/eggpool``. The filename follows
    the pattern ``eggpool-backup-YYYYMMDD-HHMMSS.zip`` so that
    ``eggpool recover`` can list and select from them.
    """
    config_path: str = ctx.obj["config_path"]

    try:
        config = AppConfig.from_toml(config_path)
    except AggregatorError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    target_dir = Path(output_dir) if output_dir else default_backup_dir()
    db_path = Path(config.database.path).expanduser().resolve()
    resolved_config_path = Path(config_path).resolve()
    env_path = resolved_config_path.parent / ".env"

    # Use SQLite backup API for file-backed databases (consistent
    # snapshot even while the server is running).  Fall back to raw
    # copy for :memory:, missing, or corrupt databases.
    archive: Path | None = None
    if config.database.path != ":memory:" and db_path.exists():

        async def _runtime_backup() -> Path:
            return await create_runtime_backup(
                db_path=db_path,
                config_path=resolved_config_path,
                env_path=env_path if env_path.exists() else None,
                output_dir=target_dir,
                install_method=_detect_install_method(),
                include_env=True,
            )

        # Snapshot failed (corrupt DB, not a real SQLite file, etc.);
        # fall back to raw file copy.
        with suppress(Exception):
            archive = asyncio.run(_runtime_backup())

    if archive is None:
        contents = BackupContents(
            config_path=resolved_config_path,
            db_path=db_path,
            env_path=env_path if env_path.exists() else None,
            install_method=_detect_install_method(),
        )
        try:
            archive = create_backup(contents, output_dir=target_dir)
        except OSError as exc:
            click.echo(f"Error: could not write backup: {exc}", err=True)
            sys.exit(1)

    click.echo(f"Wrote backup: {archive}")
    if env_path.exists():
        click.echo(f"  included: env ({env_path})")
    with zipfile.ZipFile(archive) as zf:
        for member in sorted(zf.namelist()):
            if member == ".env":
                continue
            click.echo(f"  included: {member}")


@cli.group(invoke_without_command=True)
@click.argument("source", required=False, type=click.Path())
@click.pass_context
def recover(ctx: click.Context, source: str | None) -> None:
    """Restore a backup taken with ``eggpool backup``.

    Without SOURCE, the command lists existing backups (newest first)
    and lets the operator choose one through the same selector used by
    ``eggpool connect`` / ``eggpool logout``.

    With SOURCE, the command restores from the supplied path instead
    of prompting. SOURCE can be either a full archive path or a
    relative name resolved against the default backup directory.
    """
    archive: Path | None = None
    if source is not None:
        candidate = Path(source).expanduser()
        if not candidate.is_absolute():
            candidate = default_backup_dir() / candidate
        if not candidate.exists():
            click.echo(f"Error: backup not found: {candidate}", err=True)
            sys.exit(1)
        archive = candidate
    else:
        backups = list_backups()
        if not backups:
            click.echo("No backups found in the default backup directory.")
            click.echo(f"  Default location: {Path.home() / 'backups' / 'eggpool'}")
            click.echo("  Pass an explicit path: eggpool recover <path>")
            return
        try:
            chosen = select_backup(backups)
        except KeyboardInterrupt:
            return
        if chosen is None:
            return
        archive = chosen.path

    try:
        contents = read_backup_contents(archive)
    except (OSError, ValueError) as exc:
        click.echo(f"Error: could not read backup {archive}: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Restoring from: {archive}")
    click.echo(f"  config: {contents.config_path}")
    if contents.env_path is not None:
        click.echo(f"  env:    {contents.env_path}")
    click.echo(f"  db:     {contents.db_path}")

    if not click.confirm("Overwrite current configuration and database?"):
        click.echo("Aborted.")
        return

    # Stop the running server before swapping files out from under it.
    _stop_running_server()

    try:
        restore_backup(archive, contents=contents)
    except (OSError, RuntimeError, ValueError) as exc:
        click.echo(f"Error: restore failed: {exc}", err=True)
        sys.exit(1)

    click.echo("Restore complete. Restart the server to load the new config.")
    _print_install_hint()


def _prompt_yes_no(message: str, *, default: bool = False) -> bool:
    """Prompt the user with a y/N question.

    Thin wrapper around :func:`click.confirm` so call sites can pass an
    explicit ``default`` (e.g. the uninstall flow wants a ``False``
    default for safety). Click handles the controlling-terminal checks.
    """
    return bool(click.confirm(message, default=default))


@cli.command()
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    help="Skip the interactive confirmation prompts.",
)
@click.option(
    "--keep-data",
    is_flag=True,
    help="Keep the XDG data directory (SQLite database, log files).",
)
@click.option(
    "--keep-config",
    is_flag=True,
    help="Keep the configuration file (and adjacent .env if present).",
)
@click.option(
    "--keep-path",
    is_flag=True,
    help="Skip PATH / shell-rc cleanup.",
)
@click.option(
    "--deploy-artifacts",
    is_flag=True,
    help="Also remove system-level deploy artifacts: systemd unit, "
    "logrotate config, cron entries, backup script. Requires sudo for "
    "files under /etc and /usr/local/bin.",
)
@click.pass_context
def uninstall(
    ctx: click.Context,
    assume_yes: bool,
    keep_data: bool,
    keep_config: bool,
    keep_path: bool,
    deploy_artifacts: bool,
) -> None:
    """Uninstall EggPool from this machine.

    The command detects the install method (pipx / uv tool / source /
    manual), asks for confirmation, then reverses the install: it stops
    any running server, removes the binary via the matching installer
    (or directly, for source installs), deletes the configuration and
    SQLite database, and scrubs eggpool-related PATH entries from the
    user's shell rc files.

    Pass --deploy-artifacts to also remove the system-level deploy
    artifacts that ``eggpool deploy`` created: the systemd unit, the
    logrotate config, the watchdog and backup crontab blocks, and the
    backup script. Files under ``/etc`` and ``/usr/local/bin`` are
    removed via ``sudo``.
    """
    config_path: str = ctx.obj["config_path"]

    paths = resolve_uninstall_paths(Path(config_path))

    click.echo("EggPool uninstall plan:")
    click.echo(f"  install method: {paths.install_method.value}")
    click.echo(f"  config:         {paths.config_path}")
    if paths.env_path is not None:
        click.echo(f"  env:            {paths.env_path}")
    click.echo(f"  database:       {paths.db_path}")
    click.echo(f"  data dir:       {paths.data_dir}")
    if paths.binary_path is not None:
        click.echo(f"  binary:         {paths.binary_path}")
    if paths.eggpool_dir is not None:
        click.echo(f"  source dir:     {paths.eggpool_dir}")

    if paths.install_method is InstallMethod.MANUAL and paths.binary_path is None:
        click.echo(
            "Error: cannot locate the eggpool binary on PATH, and the "
            "install method is 'manual'. Run from inside a verified "
            "eggpool checkout, or remove the binary by hand before "
            "re-running.",
            err=True,
        )
        sys.exit(1)
    if paths.install_method is InstallMethod.SOURCE and paths.eggpool_dir is None:
        click.echo(
            "Error: source install detected, but the eggpool project root "
            "could not be verified. Re-run from inside the cloned "
            "checkout so verification can succeed.",
            err=True,
        )
        sys.exit(1)

    click.echo("")
    click.echo(
        "This will remove the eggpool binary, the configuration, "
        "and the SQLite database."
    )
    click.echo("Existing backups under ~/backups/eggpool are NOT removed.")

    if not assume_yes and not _prompt_yes_no(
        "Continue with uninstall?",
        default=False,
    ):
        click.echo("Aborted.")
        return

    def _auto_confirm(_msg: str) -> bool:
        return True

    confirm: Callable[[str], bool] = _auto_confirm if assume_yes else _prompt_yes_no

    try:
        do_uninstall(
            paths=paths,
            confirm=confirm,
            cleanup_data=not keep_data,
            cleanup_config=not keep_config,
            cleanup_path=not keep_path,
        )
    except RuntimeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    # Verify the binary is actually gone. Pipx and uv tool usually
    # remove the symlink, but verify in case they fail silently.
    leftover = verify_binary_removed()
    if leftover:
        click.echo(
            "Warning: eggpool binary still reachable on PATH:",
            err=True,
        )
        for path in leftover:
            click.echo(f"  {path}", err=True)
        click.echo(
            "Remove it manually, then re-run with --yes.",
            err=True,
        )
    else:
        click.echo("Verified: eggpool binary is no longer on PATH.")

    if deploy_artifacts:
        _remove_deploy_artifacts(assume_yes=assume_yes, confirm=confirm)
    else:
        click.echo("")
        click.echo(
            "To finish removing system-level deploy artifacts, run "
            "`eggpool uninstall --deploy-artifacts` or:"
        )
        click.echo("  sudo systemctl disable --now eggpool 2>/dev/null || true")
        click.echo("  sudo rm -f /etc/systemd/system/eggpool.service")
        click.echo("  sudo rm -f /etc/logrotate.d/eggpool")
        click.echo("  eggpool deploy cron --uninstall")
        click.echo("  eggpool deploy backup-cron --uninstall")
        click.echo("Existing backups under ~/backups/eggpool were preserved.")


def _remove_deploy_artifacts(
    *, assume_yes: bool, confirm: Callable[[str], bool]
) -> None:
    """Remove systemd unit, logrotate config, cron blocks, backup script."""
    click.echo("")
    click.echo("Removing system-level deploy artifacts...")

    if confirm("Remove the systemd unit and reload systemd?"):
        _remove_systemd_unit()

    if confirm("Remove the logrotate config?"):
        _remove_logrotate_config()

    if confirm("Remove EggPool watchdog cron entries from your crontab?"):
        _remove_watchdog_cron_blocks()

    if confirm("Remove EggPool backup cron entries and backup script?"):
        _remove_backup_cron_artifacts()

    if not assume_yes:
        click.echo("System-level deploy artifacts cleanup finished.")


def _remove_systemd_unit() -> None:
    """Best-effort removal of the systemd unit."""
    target = Path("/etc/systemd/system/eggpool.service")
    if not target.exists():
        click.echo("  No systemd unit found.")
        return
    _sudo_unlink(target)
    if shutil.which("systemctl") is not None:
        subprocess.run(  # noqa: S603
            ["sudo", "systemctl", "daemon-reload"],
            capture_output=True,
            check=False,
        )
    click.echo(f"  Removed {target}.")


def _remove_logrotate_config() -> None:
    """Best-effort removal of the logrotate config."""
    target = Path("/etc/logrotate.d/eggpool")
    if not target.exists():
        click.echo("  No logrotate config found.")
        return
    _sudo_unlink(target)
    click.echo(f"  Removed {target}.")


def _remove_watchdog_cron_blocks() -> None:
    """Strip every EggPool block from the invoking user's crontab."""
    from eggpool.deploy import remove_cron_block  # noqa: PLC0415

    cron_user = _resolve_cron_user(None)
    try:
        remove_cron_block(user=cron_user)
        click.echo(f"  Stripped EggPool cron blocks from {cron_user}'s crontab.")
    except (OSError, subprocess.SubprocessError) as exc:
        click.echo(f"  Warning: could not update crontab: {exc}", err=True)


def _remove_backup_cron_artifacts() -> None:
    """Remove backup cron block and the personal backup script."""
    from eggpool.deploy import remove_cron_block  # noqa: PLC0415

    cron_user = _resolve_cron_user(None)
    try:
        remove_cron_block(user=cron_user)
    except (OSError, subprocess.SubprocessError) as exc:
        click.echo(f"  Warning: could not update crontab: {exc}", err=True)

    target = Path("/usr/local/bin/eggpool-backup")
    if target.exists():
        _sudo_unlink(target)
        click.echo(f"  Removed {target}.")
    else:
        click.echo("  No backup script found.")


def _sudo_unlink(path: Path) -> None:
    """Remove *path* via sudo when running as a non-root user."""
    try:
        path.unlink()
        return
    except PermissionError:
        pass
    except FileNotFoundError:
        return
    result = subprocess.run(  # noqa: S603
        ["sudo", "rm", "-f", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        click.echo(
            f"  Warning: could not remove {path}: {result.stderr.strip()}",
            err=True,
        )
