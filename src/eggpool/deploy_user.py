"""Stable user and path resolution for ``eggpool deploy``.

The :func:`resolve_deploy_user` function answers the question "as which
user should the systemd unit / crontab entry run?" in a way that is
robust to sudo invocation, root invocation, and source-checkout runs.

The :func:`resolve_config_path` and :func:`resolve_env_path` helpers
implement the CLI-wide config-path precedence:

    --config PATH    (CLI flag, highest)
    $EGGPOOL_CONFIG  (environment variable)
    ~/.config/eggpool/config.toml  (XDG default for installed copies)
    config.toml      (CWD default for source checkouts)

Default filesystem locations live next to this module so the rest of
the CLI does not need to know the layout.
"""

from __future__ import annotations

import getpass
import os
import pwd
from dataclasses import dataclass
from pathlib import Path


def default_config_dir() -> Path:
    """Return ``~/.config/eggpool`` for the current user.

    Honors ``$XDG_CONFIG_HOME`` when set, matching the XDG Base Directory
    specification. The directory is returned whether or not it exists;
    callers that need it created should call ``mkdir(parents=True, exist_ok=True)``.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "eggpool"
    return Path.home() / ".config" / "eggpool"


def default_data_dir() -> Path:
    """Return ``~/.local/share/eggpool`` for the current user.

    Honors ``$XDG_DATA_HOME`` when set. See :func:`default_config_dir`
    for the XDG precedent.
    """
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg) / "eggpool"
    return Path.home() / ".local" / "share" / "eggpool"


def default_state_dir() -> Path:
    """Return ``~/.local/state/eggpool`` for the current user.

    Honors ``$XDG_STATE_HOME`` when set. Falls back to the XDG default
    of ``~/.local/state`` (not ``~/.var/...``) to match the existing
    :mod:`eggpool.runtime_paths` behavior.
    """
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg) / "eggpool"
    return Path.home() / ".local" / "state" / "eggpool"


def default_config_path() -> Path:
    """Return the default installed config path: ``~/.config/eggpool/config.toml``."""
    return default_config_dir() / "config.toml"


def default_env_path() -> Path:
    """Return the default installed env file path: ``~/.config/eggpool/.env``."""
    return default_config_dir() / ".env"


@dataclass(frozen=True, slots=True)
class DeployUser:
    """Resolved deployment-user identity for ``eggpool deploy``.

    When ``eggpool`` is invoked under sudo, ``user`` and ``home`` are
    those of the *invoking* user (via ``SUDO_USER``/``SUDO_UID``/``SUDO_GID``),
    so generated artifacts land in the correct place. When invoked as
    a normal user, the values match the running process. When invoked
    as direct root with no sudo context, ``is_root`` is ``True`` and
    ``user`` is the literal ``root`` string.

    Attributes
    ----------
    user:
        Username to deploy as. Always a non-empty string.
    uid:
        Numeric UID for the user.
    gid:
        Primary GID for the user.
    home:
        Home directory for the user. Resolved via :func:`pwd.getpwnam`
        or :func:`pwd.getpwuid` so it is always consistent with the
        platform's passwd database.
    is_root:
        ``True`` when running as direct root without sudo context. The
        CLI refuses to perform a personal deployment in that state.
    is_sudo:
        ``True`` when :envvar:`SUDO_USER` was set during invocation.
    """

    user: str
    uid: int
    gid: int
    home: Path
    is_root: bool
    is_sudo: bool

    @property
    def primary_group(self) -> str:
        """Best-effort primary group name; falls back to str(gid) if not resolvable."""
        try:
            entry = pwd.getpwuid(self.uid)
            return entry.pw_name if entry.pw_name else str(self.uid)
        except KeyError:
            return str(self.gid)

    @property
    def primary_group_name(self) -> str:
        """Alias for :attr:`primary_group` matching the systemd field name."""
        return self.primary_group


def _lookup_user_by_name(name: str) -> pwd.struct_passwd | None:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def _lookup_user_by_uid(uid: int) -> pwd.struct_passwd | None:
    try:
        return pwd.getpwuid(uid)
    except KeyError:
        return None


def resolve_deploy_user(*, env: dict[str, str] | None = None) -> DeployUser:
    """Resolve the deployment user for the current invocation.

    Resolution rules:

    1. If :envvar:`SUDO_USER` is set and points at a real user, use that
       user's name/UID/GID/home. This is the common "ran under sudo"
       case for `eggpool deploy systemd --install`.
    2. Otherwise, if :envvar:`SUDO_UID` and :envvar:`SUDO_GID` are set,
       use them (rare but matches what ``sudo`` exports to some helper
       environments).
    3. Otherwise, fall back to the current process's effective UID.
       ``getpass.getuser()`` returns the right name on POSIX systems;
       for the root case, ``user`` is the literal ``"root"`` string.

    Direct root invocation (UID 0 with no SUDO context) sets ``is_root``
    so the CLI can refuse a personal deployment.
    """
    env_map = dict(os.environ if env is None else env)

    sudo_user = env_map.get("SUDO_USER", "").strip()
    if sudo_user:
        entry = _lookup_user_by_name(sudo_user)
        if entry is not None:
            return DeployUser(
                user=entry.pw_name,
                uid=entry.pw_uid,
                gid=entry.pw_gid,
                home=Path(entry.pw_dir),
                is_root=False,
                is_sudo=True,
            )

    sudo_uid_raw = env_map.get("SUDO_UID", "").strip()
    sudo_gid_raw = env_map.get("SUDO_GID", "").strip()
    if sudo_uid_raw and sudo_gid_raw:
        try:
            sudo_uid = int(sudo_uid_raw)
            sudo_gid = int(sudo_gid_raw)
        except ValueError:
            sudo_uid = 0
            sudo_gid = 0
        if sudo_uid > 0:
            entry = _lookup_user_by_uid(sudo_uid)
            if entry is not None:
                return DeployUser(
                    user=entry.pw_name,
                    uid=entry.pw_uid,
                    gid=entry.pw_gid,
                    home=Path(entry.pw_dir),
                    is_root=False,
                    is_sudo=True,
                )
            return DeployUser(
                user=str(sudo_uid),
                uid=sudo_uid,
                gid=sudo_gid,
                home=Path("/") / "var" / "empty",
                is_root=False,
                is_sudo=True,
            )

    euid = os.geteuid()
    if euid == 0:
        return DeployUser(
            user="root",
            uid=0,
            gid=0,
            home=Path("/root"),
            is_root=True,
            is_sudo=False,
        )

    entry = _lookup_user_by_uid(euid)
    if entry is not None:
        return DeployUser(
            user=entry.pw_name,
            uid=entry.pw_uid,
            gid=entry.pw_gid,
            home=Path(entry.pw_dir),
            is_root=False,
            is_sudo=False,
        )

    name = getpass.getuser() or str(euid)
    return DeployUser(
        user=name,
        uid=euid,
        gid=euid,
        home=Path.home(),
        is_root=False,
        is_sudo=False,
    )


def resolve_config_path(
    *,
    cli_value: str | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    """Apply the documented config-path precedence.

    Order:

    1. ``cli_value`` (the ``--config`` flag, if non-empty).
    2. ``$EGGPOOL_CONFIG`` (environment variable).
    3. ``~/.config/eggpool/config.toml`` (XDG default for installed
       copies — used when the file actually exists).
    4. ``config.toml`` in the current working directory (preserves the
       historical source-checkout default).

    The returned path is always absolute, resolving symlinks so systemd
    and crontab commands embed stable paths even if ``config.toml`` is
    a symlink.
    """
    if cli_value and cli_value.strip():
        return Path(cli_value).expanduser().resolve()

    env_map = dict(os.environ if env is None else env)
    env_value = env_map.get("EGGPOOL_CONFIG", "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()

    xdg_default = default_config_path()
    if xdg_default.exists():
        return xdg_default.resolve()

    cwd_default = Path.cwd() / "config.toml"
    return cwd_default.resolve()


def resolve_env_path(
    *,
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> Path | None:
    """Locate the env file for an install, or ``None`` if not present.

    Search order:

    1. ``$EGGPOOL_ENV`` (explicit override).
    2. ``<config-dir>/.env`` when ``config_path`` is supplied.
    3. The XDG default env path.

    Returns ``None`` when none of those candidates exist; the caller
    can then decide whether to skip the env file in the rendered unit.
    """
    env_map = dict(os.environ if env is None else env)
    explicit = env_map.get("EGGPOOL_ENV", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        return candidate if candidate.exists() else None

    if config_path is not None:
        candidate = config_path.parent / ".env"
        if candidate.exists():
            return candidate.resolve()

    candidate = default_env_path()
    return candidate.resolve() if candidate.exists() else None


def config_path_diagnostics(config_path: Path) -> str:
    """Render a short "where is the config" line for CLI output.

    Used by ``deploy systemd --install`` so the operator can confirm
    which config the new unit is about to point at.
    """
    return f"  config: {config_path}"


__all__ = [
    "DeployUser",
    "default_config_dir",
    "default_config_path",
    "default_data_dir",
    "default_env_path",
    "default_state_dir",
    "resolve_config_path",
    "resolve_deploy_user",
    "resolve_env_path",
]
