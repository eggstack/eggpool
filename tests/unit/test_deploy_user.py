"""Tests for :mod:`eggpool.deploy_user`.

Covers:

- XDG-aware default path resolution
- ``DeployUser`` dataclass invariants
- ``resolve_deploy_user`` under sudo / root / normal euid
- ``resolve_config_path`` precedence: CLI flag > $EGGPOOL_CONFIG > XDG > CWD
- ``resolve_env_path`` search order
- ``config_path_diagnostics`` formatting

The pwd lookups in :func:`resolve_deploy_user` are mocked so the tests
are hermetic and do not depend on the host's passwd database.
"""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from eggpool.deploy_user import (
    DeployUser,
    config_path_diagnostics,
    default_config_dir,
    default_config_path,
    default_data_dir,
    default_env_path,
    default_state_dir,
    resolve_config_path,
    resolve_deploy_user,
    resolve_env_path,
)


def _passwd_entry(
    name: str = "alice", uid: int = 1000, gid: int = 1000, home: str = "/home/alice"
) -> Any:
    """Build a pwd.struct_passwd-like object for tests."""
    entry = pytest.importorskip("pwd").struct_passwd(
        (name, "x", uid, gid, name, home, "/bin/bash")
    )
    return entry


# ---------------------------------------------------------------------------
# XDG default path helpers
# ---------------------------------------------------------------------------


class TestDefaultPaths:
    def test_default_config_dir_uses_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: Path("/home/alice"))
        assert default_config_dir() == Path("/home/alice/.config/eggpool")

    def test_default_config_dir_honors_xdg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "/srv/xdg/cfg")
        assert default_config_dir() == Path("/srv/xdg/cfg/eggpool")

    def test_default_config_dir_ignores_blank_xdg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "   ")
        monkeypatch.setattr(Path, "home", lambda: Path("/home/alice"))
        assert default_config_dir() == Path("/home/alice/.config/eggpool")

    def test_default_data_dir_uses_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: Path("/home/alice"))
        assert default_data_dir() == Path("/home/alice/.local/share/eggpool")

    def test_default_data_dir_honors_xdg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", "/srv/xdg/share")
        assert default_data_dir() == Path("/srv/xdg/share/eggpool")

    def test_default_state_dir_uses_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: Path("/home/alice"))
        assert default_state_dir() == Path("/home/alice/.local/state/eggpool")

    def test_default_state_dir_honors_xdg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", "/srv/xdg/state")
        assert default_state_dir() == Path("/srv/xdg/state/eggpool")

    def test_default_config_path_is_config_dir_join(self) -> None:
        assert default_config_path() == default_config_dir() / "config.toml"

    def test_default_env_path_is_config_dir_join(self) -> None:
        assert default_env_path() == default_config_dir() / ".env"


# ---------------------------------------------------------------------------
# DeployUser dataclass
# ---------------------------------------------------------------------------


class TestDeployUserDataclass:
    def test_primary_group_returns_passwd_name(self) -> None:
        import pwd

        user = DeployUser(
            user="alice",
            uid=1000,
            gid=1000,
            home=Path("/home/alice"),
            is_root=False,
            is_sudo=False,
        )
        with patch.object(pwd, "getpwuid", return_value=_passwd_entry(name="alice")):
            assert user.primary_group == "alice"
            assert user.primary_group_name == "alice"

    def test_primary_group_falls_back_when_lookup_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pwd

        def _raise(_uid: int) -> None:
            raise KeyError(_uid)

        monkeypatch.setattr(pwd, "getpwuid", _raise)
        user = DeployUser(
            user="1000",
            uid=1000,
            gid=1234,
            home=Path("/home/alice"),
            is_root=False,
            is_sudo=False,
        )
        assert user.primary_group == "1234"

    def test_dataclass_is_frozen(self) -> None:
        user = DeployUser(
            user="alice",
            uid=1000,
            gid=1000,
            home=Path("/home/alice"),
            is_root=False,
            is_sudo=False,
        )
        with pytest.raises(FrozenInstanceError):
            user.user = "bob"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# resolve_deploy_user
# ---------------------------------------------------------------------------


class TestResolveDeployUser:
    def test_sudo_user_takes_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = {"SUDO_USER": "alice", "SUDO_UID": "9999", "SUDO_GID": "9999"}
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        with patch("eggpool.deploy_user._lookup_user_by_name") as lookup_name:
            lookup_name.return_value = _passwd_entry()
            user = resolve_deploy_user(env=env)
        assert user.is_sudo is True
        assert user.is_root is False
        assert user.user == "alice"
        assert user.uid == 1000
        assert user.gid == 1000
        assert user.home == Path("/home/alice")

    def test_sudo_user_unknown_name_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = {"SUDO_USER": "ghost-user-does-not-exist"}
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        with (
            patch("eggpool.deploy_user._lookup_user_by_name") as lookup_name,
            patch("eggpool.deploy_user._lookup_user_by_uid") as lookup_uid,
        ):
            lookup_name.return_value = None
            lookup_uid.return_value = _passwd_entry()
            user = resolve_deploy_user(env=env)
        assert user.is_sudo is False
        assert user.user == "alice"

    def test_sudo_uid_used_when_no_sudo_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = {"SUDO_UID": "1000", "SUDO_GID": "1000"}
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        with patch("eggpool.deploy_user._lookup_user_by_uid") as lookup_uid:
            lookup_uid.return_value = _passwd_entry()
            user = resolve_deploy_user(env=env)
        assert user.is_sudo is True
        assert user.is_root is False
        assert user.user == "alice"
        assert user.uid == 1000
        assert user.home == Path("/home/alice")

    def test_sudo_uid_unknown_uses_fake_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = {"SUDO_UID": "4242", "SUDO_GID": "4242"}
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        with patch("eggpool.deploy_user._lookup_user_by_uid") as lookup_uid:
            lookup_uid.return_value = None
            user = resolve_deploy_user(env=env)
        assert user.user == "4242"
        assert user.uid == 4242
        assert user.gid == 4242
        assert user.home == Path("/var/empty")

    def test_sudo_uid_zero_is_treated_as_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = {"SUDO_UID": "0", "SUDO_GID": "0"}
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        with patch("eggpool.deploy_user._lookup_user_by_uid") as lookup_uid:
            lookup_uid.assert_not_called()
            user = resolve_deploy_user(env=env)
        assert user.is_root is True
        assert user.user == "root"

    def test_sudo_uid_unparseable_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = {"SUDO_UID": "notanumber", "SUDO_GID": "notanumber"}
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        with patch("eggpool.deploy_user._lookup_user_by_uid") as lookup_uid:
            lookup_uid.assert_not_called()
            user = resolve_deploy_user(env=env)
        assert user.is_root is True

    def test_direct_root_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.delenv("SUDO_UID", raising=False)
        monkeypatch.delenv("SUDO_GID", raising=False)
        user = resolve_deploy_user()
        assert user.is_root is True
        assert user.user == "root"
        assert user.uid == 0
        assert user.home == Path("/root")

    def test_normal_user_uses_passwd_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.delenv("SUDO_UID", raising=False)
        monkeypatch.delenv("SUDO_GID", raising=False)
        with patch("eggpool.deploy_user._lookup_user_by_uid") as lookup_uid:
            lookup_uid.return_value = _passwd_entry()
            user = resolve_deploy_user()
        assert user.is_root is False
        assert user.is_sudo is False
        assert user.user == "alice"
        assert user.home == Path("/home/alice")

    def test_blank_sudo_user_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = {"SUDO_USER": "   "}
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        with (
            patch("eggpool.deploy_user._lookup_user_by_name") as lookup_name,
            patch("eggpool.deploy_user._lookup_user_by_uid") as lookup_uid,
        ):
            lookup_name.return_value = None
            lookup_uid.return_value = _passwd_entry()
            user = resolve_deploy_user(env=env)
        assert user.is_sudo is False
        assert user.user == "alice"

    def test_normal_user_uses_getuser_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(os, "geteuid", lambda: 4242)
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.delenv("SUDO_UID", raising=False)
        monkeypatch.delenv("SUDO_GID", raising=False)
        with (
            patch("eggpool.deploy_user._lookup_user_by_uid") as lookup_uid,
            patch("eggpool.deploy_user.getpass.getuser", return_value="") as getuser,
        ):
            lookup_uid.return_value = None
            getuser.return_value = ""
            user = resolve_deploy_user()
        assert user.user == "4242"
        assert user.uid == 4242
        assert user.gid == 4242


# ---------------------------------------------------------------------------
# resolve_config_path
# ---------------------------------------------------------------------------


class TestResolveConfigPath:
    def test_cli_value_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "explicit.toml"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setenv("EGGPOOL_CONFIG", str(tmp_path / "env.toml"))
        result = resolve_config_path(cli_value=str(target))
        assert result == target.resolve()

    def test_cli_value_supports_tilde(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "tilde.toml"
        target.write_text("x", encoding="utf-8")
        result = resolve_config_path(cli_value="~/tilde.toml")
        assert result == target.resolve()

    def test_env_var_used_when_no_cli_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "from-env.toml"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setenv("EGGPOOL_CONFIG", str(target))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-home")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-home" / "cfg"))
        result = resolve_config_path()
        assert result == target.resolve()

    def test_xdg_default_used_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        xdg_cfg = tmp_path / "cfg" / "eggpool"
        xdg_cfg.mkdir(parents=True)
        xdg_config = xdg_cfg / "config.toml"
        xdg_config.write_text("x", encoding="utf-8")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.delenv("EGGPOOL_CONFIG", raising=False)
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        result = resolve_config_path()
        assert result == xdg_config.resolve()

    def test_cwd_default_used_when_xdg_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "absent" / "cfg"))
        monkeypatch.delenv("EGGPOOL_CONFIG", raising=False)
        cwd = tmp_path / "src-checkout"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        result = resolve_config_path()
        assert result == (cwd / "config.toml").resolve()

    def test_blank_cli_value_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "absent"))
        monkeypatch.delenv("EGGPOOL_CONFIG", raising=False)
        cwd = tmp_path / "src"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        assert resolve_config_path(cli_value="").name == "config.toml"

    def test_uses_real_os_environ_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EGGPOOL_CONFIG", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        cwd = Path("/tmp/nope")
        cwd.mkdir(exist_ok=True)
        monkeypatch.chdir(cwd)
        result = resolve_config_path()
        assert result == (cwd / "config.toml").resolve()


# ---------------------------------------------------------------------------
# resolve_env_path
# ---------------------------------------------------------------------------


class TestResolveEnvPath:
    def test_explicit_eggpool_env_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "custom.env"
        target.write_text("X=Y", encoding="utf-8")
        monkeypatch.setenv("EGGPOOL_ENV", str(target))
        assert resolve_env_path() == target.resolve()

    def test_explicit_eggpool_env_missing_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("EGGPOOL_ENV", str(tmp_path / "ghost.env"))
        assert resolve_env_path() is None

    def test_sibling_env_used_when_config_path_supplied(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config = config_dir / "config.toml"
        config.write_text("x", encoding="utf-8")
        sibling = config_dir / ".env"
        sibling.write_text("X=Y", encoding="utf-8")
        monkeypatch.delenv("EGGPOOL_ENV", raising=False)
        assert resolve_env_path(config_path=config) == sibling.resolve()

    def test_sibling_env_missing_falls_through_to_xdg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config = config_dir / "config.toml"
        config.write_text("x", encoding="utf-8")
        monkeypatch.delenv("EGGPOOL_ENV", raising=False)
        xdg_default = tmp_path / "xdg" / "eggpool"
        xdg_default.mkdir(parents=True)
        env_file = xdg_default / ".env"
        env_file.write_text("X=Y", encoding="utf-8")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert resolve_env_path(config_path=config) == env_file.resolve()

    def test_returns_none_when_no_env_anywhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("EGGPOOL_ENV", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
        assert resolve_env_path() is None


# ---------------------------------------------------------------------------
# config_path_diagnostics
# ---------------------------------------------------------------------------


class TestConfigPathDiagnostics:
    def test_renders_short_label(self, tmp_path: Path) -> None:
        target = tmp_path / "config.toml"
        line = config_path_diagnostics(target)
        assert line.startswith("  config:")
        assert str(target) in line
