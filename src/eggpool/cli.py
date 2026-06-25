"""CLI entry point for the aggregator."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from eggpool.db.connection import Database

import click

from eggpool.accounts.registry import account_config_rows
from eggpool.auth import require_auth_at_startup
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import AccountRepository, ProviderRepository
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
from eggpool.lifecycle.backup import BackupContents, create_backup
from eggpool.logging import configure_logging
from eggpool.models.config import AppConfig
from eggpool.providers.client_pool import ProviderClientPool
from eggpool.providers.contract import PROVIDER_STATUS_SYMBOLS, compose_provider_url
from eggpool.toml_edit import (
    render_toml_string,
    section_has_key,
    update_section_value,
)


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
    default="config.toml",
    help="Path to the TOML configuration file.",
    type=click.Path(),
)
@click.pass_context
def cli(ctx: click.Context, config_path: str) -> None:
    """EggPool - aggregate OpenCode Go subscriptions."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = os.path.abspath(config_path)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        return

    _skip_ensure_config = {"help", "version", "init-config"}
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
@click.pass_context
def serve(ctx: click.Context) -> None:
    """Start the aggregation proxy server.

    This process is the Granian supervisor. Granian keeps ``workers=1``
    so the total process count is two (supervisor + one worker) plus
    a small thread pool sized by ``[server].threads``. The supervisor
    owns the PID file: it writes ``os.getpid()`` before
    ``Granian.serve()`` and clears the file when Granian returns. The
    ASGI worker is a child of the supervisor and never touches the
    PID file, so ``eggpool stop`` always signals the right process.
    """
    from granian import Granian  # type: ignore[import-untyped]

    from eggpool import runtime

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
        # Check for uv tool install: executable lives under uv tool directories
        exe = Path(sys.executable).resolve()
        parts = exe.parts
        if "uv" in parts and "tools" in parts:
            return "uv-tool"

        # Check for pipx: pipx is available and executable is in a pipx-managed venv
        import shutil

        if shutil.which("pipx") is not None:
            return "pipx"

        # Generic venv (manual or unknown tool) — default to pip for upgrade
        return "pip"

    # Source checkout (pyproject.toml nearby)
    cli_path = Path(__file__).resolve()
    if (cli_path.parent.parent.parent / "pyproject.toml").exists():
        return "source"

    return "pip"


def _resolve_eggpool_binary() -> str:
    """Resolve the path to the eggpool binary for systemd ExecStart."""
    import shutil

    which = shutil.which("eggpool")
    if which is not None:
        return str(Path(which).resolve())

    # Fallback: use sys.executable with -m eggpool (works for source installs)
    return f"{sys.executable} -m eggpool"


def _resolve_data_dir() -> Path:
    """Resolve the data directory for the current user."""
    from eggpool.constants import DEFAULT_DATABASE_PATH

    return Path(DEFAULT_DATABASE_PATH).parent


def _resolve_env_path() -> str | None:
    """Find a .env file for the current user/installation.

    Checks CWD, home directory, and config-referenced env vars.
    Returns the path if found, None otherwise.
    """
    candidates = [
        Path.cwd() / ".env",
        Path.home() / ".env",
    ]
    for p in candidates:
        if p.exists():
            return str(p.resolve())
    return None


def _require_root() -> None:
    """Exit with an error if not running as root."""
    import os

    if os.geteuid() != 0:
        click.echo(
            "Error: --install requires root privileges.\n"
            "Re-run with: sudo eggpool deploy <command> --install",
            err=True,
        )
        sys.exit(1)


def _confirm_install(component: str, path: str) -> None:
    """Prompt the user to confirm a system-level installation action."""
    click.echo(f"\nThis will write {path} and may restart services.")
    if not click.confirm("Proceed?"):
        click.echo("Aborted.")
        sys.exit(0)


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


def _run_systemctl(args: list[str], quiet: bool = False) -> bool:
    """Run a systemctl command. Returns True on success."""
    import subprocess

    cmd = ["systemctl", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    if result.returncode != 0:
        click.echo(
            f"  Warning: {' '.join(cmd)} failed: {result.stderr.strip()}", err=True
        )
        return False
    if not quiet and result.stdout.strip():
        click.echo(result.stdout.strip())
    return True


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
                    "SELECT model_id, provider_id, display_name, "
                    "capabilities, source_metadata FROM provider_model_metadata"
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

        # Re-apply current config overrides
        if models_data:
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
                # Group by base model_id and apply conservative merge
                # so OpenCode compacts before any single provider's
                # limit is exceeded.
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
    except Exception:
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
    "--install", is_flag=True, help="Install the systemd unit (requires root)."
)
@click.pass_context
def deploy_systemd(ctx: click.Context, install: bool) -> None:
    """Print the systemd unit and install instructions.

    With --install, writes the unit file, reloads systemd, and
    enables/starts the service. Not intended for public-facing
    deployments — use a dedicated eggpool user for production.
    """
    from eggpool.deploy import SYSTEMD_UNIT, build_personal_systemd_unit

    config_path: str = ctx.obj["config_path"]
    binary_path = _resolve_eggpool_binary()
    data_dir = str(_resolve_data_dir())
    env_path = _resolve_env_path()

    # Generate dynamic snippet for personal use
    dynamic_unit = build_personal_systemd_unit(
        binary_path=binary_path,
        config_path=config_path,
        data_dir=data_dir,
        env_path=env_path,
    )

    # Always print the snippet + instructions
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

    # Also show the production snippet for reference
    click.echo("")
    click.echo("=" * 72)
    click.echo("")
    click.echo("Production snippet (separate eggpool user, security hardening):")
    click.echo("")
    click.echo(SYSTEMD_UNIT)

    # Auto-install hint
    click.echo("")
    click.echo(
        "Run 'sudo eggpool deploy systemd --install' to set this up automatically."
    )

    if install:
        _require_root()
        _stop_running_server()
        _confirm_install("systemd unit", "/etc/systemd/system/eggpool.service")
        _write_file("/etc/systemd/system/eggpool.service", dynamic_unit)
        _run_systemctl(["daemon-reload"])
        _run_systemctl(["enable", "eggpool"])
        _run_systemctl(["start", "eggpool"])
        click.echo("")
        _run_systemctl(["status", "eggpool"])


@deploy.command("logrotate")
@click.option(
    "--install", is_flag=True, help="Install the logrotate config (requires root)."
)
@click.pass_context
def deploy_logrotate(ctx: click.Context, install: bool) -> None:
    """Print the logrotate config and install instructions.

    With --install, writes the config to /etc/logrotate.d/eggpool.
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

    if install:
        _require_root()
        _confirm_install("logrotate config", "/etc/logrotate.d/eggpool")
        _write_file("/etc/logrotate.d/eggpool", dynamic_conf)
        click.echo("  Verifying config...")
        _run_systemctl(["restart", "logrotate"], quiet=True)
        click.echo("  Logrotate config installed.")


@deploy.command("cron")
@click.option("--install", is_flag=True, help="Install the cron entry (requires root).")
@click.pass_context
def deploy_cron(ctx: click.Context, install: bool) -> None:
    """Print a cron entry for starting eggpool on boot and on crash.

    This is an alternative to systemd for systems without systemd
    support. For production use, prefer systemd with a dedicated
    eggpool user.

    With --install, writes the backup script and cron entry.
    """
    from eggpool.deploy import (
        CRON_BACKUP_FILE,
        CRON_BACKUP_SCRIPT,
        build_personal_backup_cron,
        build_personal_backup_script,
    )

    config_path: str = ctx.obj["config_path"]
    data_dir = str(_resolve_data_dir())
    db_path = str(Path(data_dir) / "usage.sqlite3")

    # Generate dynamic snippets for personal use
    dynamic_script = build_personal_backup_script(
        config_path=config_path, db_path=db_path
    )
    dynamic_cron = build_personal_backup_cron()

    click.echo("EggPool cron setup (personal use)")
    click.echo("")
    click.echo(
        "This sets up a daily 02:00 backup of the configuration and "
        "SQLite database under ~/backups/eggpool."
    )
    click.echo("")
    click.echo("Install the backup script:")
    click.echo("")
    click.echo("  sudo tee /usr/local/bin/eggpool-backup > /dev/null << 'EGGPOOL_EOF'")
    for line in dynamic_script.splitlines():
        click.echo(f"{line}")
    click.echo("EGGPOOL_EOF")
    click.echo("  sudo chmod +x /usr/local/bin/eggpool-backup")
    click.echo("")
    click.echo("Install the cron entry (user cron):")
    click.echo("")
    click.echo(
        "  crontab -l 2>/dev/null | { cat; echo ''; echo '"
        + dynamic_cron.strip()
        + "'; } | crontab -"
    )
    click.echo("")
    click.echo("Snippet:")
    click.echo("")
    click.echo(dynamic_cron)

    # Also show the production snippet for reference
    click.echo("")
    click.echo("=" * 72)
    click.echo("")
    click.echo("Production snippet (system cron with root user):")
    click.echo("")
    click.echo(CRON_BACKUP_FILE)
    click.echo("")
    click.echo(CRON_BACKUP_SCRIPT)

    if _copy_to_clipboard(dynamic_script + dynamic_cron):
        click.echo("")
        click.echo("Copied script + cron entry to clipboard.")

    click.echo("")
    click.echo("Run 'sudo eggpool deploy cron --install' to set this up automatically.")

    if install:
        _require_root()
        _confirm_install("cron backup script + entry", "/usr/local/bin/eggpool-backup")
        _write_file("/usr/local/bin/eggpool-backup", dynamic_script)
        import subprocess

        subprocess.run(  # noqa: S603
            ["chmod", "+x", "/usr/local/bin/eggpool-backup"],
            check=True,
        )
        click.echo("  Installed /usr/local/bin/eggpool-backup")

        # Install user cron entry
        cron_line = "0 2 * * * /usr/local/bin/eggpool-backup"
        result = subprocess.run(  # noqa: S603
            ["crontab", "-l"],
            capture_output=True,
            text=True,
        )
        existing = result.stdout if result.returncode == 0 else ""
        # Match the full schedule + command so unrelated cron entries that
        # merely reference the same binary (e.g. a monitoring probe) do
        # not falsely satisfy this check.
        existing_lines = {line.strip() for line in existing.splitlines()}
        if cron_line in existing_lines:
            click.echo("  Cron entry already present — skipping.")
        else:
            cron_entry = f"\n# EggPool daily backup (personal use)\n{cron_line}\n"
            new_cron = existing.rstrip() + cron_entry
            subprocess.run(  # noqa: S603
                ["crontab", "-"],
                input=new_cron,
                check=True,
                text=True,
            )
            click.echo("  Installed user cron entry (02:00 daily backup).")


@deploy.command("all")
@click.option(
    "--install", is_flag=True, help="Install all deployment files (requires root)."
)
@click.pass_context
def deploy_all(ctx: click.Context, install: bool) -> None:
    """Print every deployment snippet in sequence.

    With --install, installs systemd unit, logrotate, and cron entry.
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


@cli.command()
@click.pass_context
def rehash(ctx: click.Context) -> None:
    """Restart the server to apply configuration changes."""
    ctx.invoke(restart, timeout=10.0)


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
        try:
            catalog = CatalogService(config, registry, db, client_pool)
            await catalog.refresh()
            count = catalog.cache.model_count
            click.echo(f"Refreshed catalog: {count} models found")
        finally:
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

    import httpx

    from eggpool.providers.connect import restart_server

    config_path: str = ctx.obj["config_path"]
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

    Designed for cron jobs that should restart a stopped server.
    Uses only the PID file and a kill probe — no network I/O.
    """
    from eggpool.constants import PID_FILE

    if not PID_FILE.exists():
        sys.exit(1)

    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        sys.exit(1)

    if _is_process_running(pid):
        sys.exit(0)
    else:
        sys.exit(1)


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

    env_path = Path(config_path).parent / ".env"
    contents = BackupContents(
        config_path=Path(config_path).resolve(),
        db_path=Path(config.database.path).expanduser().resolve(),
        env_path=env_path if env_path.exists() else None,
        install_method=_detect_install_method(),
    )

    target_dir = Path(output_dir) if output_dir else default_backup_dir()

    try:
        archive = create_backup(contents, output_dir=target_dir)
    except OSError as exc:
        click.echo(f"Error: could not write backup: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Wrote backup: {archive}")
    if contents.env_path is not None:
        click.echo(f"  included: env ({contents.env_path})")
    for member in sorted(contents.member_names()):
        if member == "env":
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
@click.pass_context
def uninstall(
    ctx: click.Context,
    assume_yes: bool,
    keep_data: bool,
    keep_config: bool,
    keep_path: bool,
) -> None:
    """Uninstall EggPool from this machine.

    The command detects the install method (pipx / uv tool / source /
    manual), asks for confirmation, then reverses the install: it stops
    any running server, removes the binary via the matching installer
    (or directly, for source installs), deletes the configuration and
    SQLite database, and scrubs eggpool-related PATH entries from the
    user's shell rc files.

    System-level deployment artifacts (systemd unit, logrotate config,
    cron entry) are not touched automatically; the command prints the
    manual commands for those at the end.
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
            "  Remove it manually if it was not created by pipx/uv tool.",
            err=True,
        )
    else:
        click.echo("Verified: eggpool binary is no longer on PATH.")

    # Print cleanup instructions for system-level deploy artifacts.
    click.echo("")
    click.echo("To finish removing system-level deploy artifacts, run:")
    click.echo("  sudo systemctl disable --now eggpool 2>/dev/null || true")
    click.echo("  sudo rm -f /etc/systemd/system/eggpool.service")
    click.echo("  sudo rm -f /etc/logrotate.d/eggpool")
    click.echo("  crontab -l 2>/dev/null | grep -v 'eggpool' | crontab -")
    click.echo("Existing backups under ~/backups/eggpool were preserved.")


def main() -> NoReturn:
    """Main entry point."""
    cli(obj={})  # type: ignore[call-arg]
    sys.exit(0)
