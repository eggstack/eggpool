#!/usr/bin/env python3
"""Qualify the binary-first quick installer in disposable environments.

Deterministic harness: never touches a real package manager, network, or user
state. Fresh native paths use file:// release fixtures with
EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_ORIGIN=1. Real wheel/index qualification
remains owned by the release tests.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install.sh"


class QualificationError(RuntimeError):
    """A quick-installer contract failed."""


def _exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_command(
    path: Path, *, kind: str, version: str, native: bool, update_log: str = ""
) -> None:
    # Native fake supports `update [VERSION]` by rewriting its own version
    # strings, simulating the Rust updater's owner-preserving transition.
    # Legacy python-fallback mode is handled separately in _run.
    native_text = str(native).lower()
    _exe(
        path,
        f"""#!{sys.executable}
import os
import pathlib
import sys

VERSION = "{version}"
KIND = "{kind}"
NATIVE = "{native_text}"
UPDATE_LOG = {update_log!r}

def _report():
    print("kind\\t" + KIND)
    print("version\\t" + VERSION)
    print("native\\t" + NATIVE)

if sys.argv[1:] == ["install-provenance", "--shell"]:
    _report()
elif sys.argv[1:] == ["version"]:
    print(VERSION)
elif sys.argv[1:2] == ["init-config"]:
    target = pathlib.Path(sys.argv[2])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[server]\\nport = 11300\\n", encoding="utf-8")
elif sys.argv[1:2] == ["runtime-status"]:
    raise SystemExit(1)
elif sys.argv[1:2] in (["stop"], ["restart"]):
    pass
elif sys.argv[1:2] == ["update"]:
    if UPDATE_LOG:
        pathlib.Path(os.environ.get("FAKE_UPDATE_LOG", UPDATE_LOG)).open(
            "a", encoding="utf-8"
        ).write(" ".join(sys.argv[1:]) + "\\n")
    # Simulate owner-preserving update: rewrite own version strings.
    if len(sys.argv) > 2:
        new_version = sys.argv[2].lstrip("vV")
        try:
            me = pathlib.Path(__file__)
            text = me.read_text(encoding="utf-8")
            old_marker = 'VERSION = "' + VERSION + '"'
            new_marker = 'VERSION = "' + new_version + '"'
            text = text.replace(old_marker, new_marker)
            me.write_text(text, encoding="utf-8")
            VERSION = new_version
        except OSError:
            raise SystemExit(1)
else:
    raise SystemExit(2)
""",
    )


def _fake_raw_binary(
    path: Path, *, version: str, kind: str = "standalone", native: bool = True
) -> None:
    full_kind = "standalone-rust" if kind == "standalone" else kind
    _fake_command(path, kind=full_kind, version=version, native=native)


def _fake_raw_binary_failing_init(path: Path, *, version: str) -> None:
    # Candidate passes release/provenance/version checks but fails
    # `init-config` after writing a partial file. Used to prove fresh-install
    # rollback removes the committed executable and the partial config.
    _exe(
        path,
        f"""#!{sys.executable}
import pathlib
import sys

VERSION = "{version}"

def _report():
    print("kind\\tstandalone-rust")
    print("version\\t" + VERSION)
    print("native\\ttrue")

if sys.argv[1:] == ["install-provenance", "--shell"]:
    _report()
elif sys.argv[1:] == ["version"]:
    print(VERSION)
elif sys.argv[1:2] == ["init-config"]:
    target = pathlib.Path(sys.argv[2])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[server\\npartial = true\\n", encoding="utf-8")
    raise SystemExit(3)
elif sys.argv[1:2] == ["runtime-status"]:
    raise SystemExit(1)
elif sys.argv[1:2] in (["stop"], ["restart"]):
    pass
else:
    raise SystemExit(2)
""",
    )


def _fake_raw_binary_race_on_config(path: Path, *, version: str) -> None:
    # Candidate passes verification but its `init-config` replaces the
    # destination executable with unrelated bytes before failing. Rollback
    # must detect the identity change and refuse to delete the replacement.
    _exe(
        path,
        f"""#!{sys.executable}
import os
import pathlib
import sys

VERSION = "{version}"

def _report():
    print("kind\\tstandalone-rust")
    print("version\\t" + VERSION)
    print("native\\ttrue")

if sys.argv[1:] == ["install-provenance", "--shell"]:
    _report()
elif sys.argv[1:] == ["version"]:
    print(VERSION)
elif sys.argv[1:2] == ["init-config"]:
    home = pathlib.Path(os.environ.get("HOME", str(pathlib.Path.home())))
    default_bin = str(home / ".local/bin")
    bin_dir = pathlib.Path(os.environ.get("EGGPOOL_INSTALL_BIN_DIR", default_bin))
    dest = bin_dir / "eggpool"
    try:
        dest.write_bytes(b"racing\\n")
        dest.chmod(0o755)
    except OSError:
        pass
    raise SystemExit(3)
elif sys.argv[1:2] == ["runtime-status"]:
    raise SystemExit(1)
elif sys.argv[1:2] in (["stop"], ["restart"]):
    pass
else:
    raise SystemExit(2)
""",
    )


def _final_config_for_env() -> str:
    # Python snippet resolving the installer's final config path from env.
    # Indented 4 spaces for insertion inside the init-config branch.
    return (
        "    eggpool_config = os.environ.get('EGGPOOL_CONFIG')\n"
        "    if eggpool_config:\n"
        "        final = pathlib.Path(eggpool_config)\n"
        "    else:\n"
        "        xdg = os.environ.get('XDG_CONFIG_HOME')\n"
        "        if not xdg:\n"
        "            home = os.environ.get('HOME', str(pathlib.Path.home()))\n"
        "            xdg = str(pathlib.Path(home) / '.config')\n"
        "        final = pathlib.Path(xdg) / 'eggpool/config.toml'\n"
    )


def _fake_raw_binary_config_concurrent_writer(path: Path, *, version: str) -> None:
    # M003 race: concurrent operator creates FINAL while generation runs.
    # New code calls init-config STAGING; this fake writes operator bytes to
    # FINAL, writes a partial to STAGING, then fails. Old direct-final code
    # calls init-config FINAL; the fake writes operator bytes to FINAL then
    # fails, and old rollback deletes FINAL (the bug). New code must preserve
    # FINAL, clean staging, and roll back DEST.
    _exe(
        path,
        f"""#!{sys.executable}
import os
import pathlib
import sys

VERSION = "{version}"

def _report():
    print("kind\\tstandalone-rust")
    print("version\\t" + VERSION)
    print("native\\ttrue")

if sys.argv[1:] == ["install-provenance", "--shell"]:
    _report()
elif sys.argv[1:] == ["version"]:
    print(VERSION)
elif sys.argv[1:2] == ["init-config"]:
    target = pathlib.Path(sys.argv[2])
{_final_config_for_env()}
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        final.write_bytes(b"operator-config-concurrent\\n")
    except OSError:
        pass
    if target != final:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("[server\\npartial = true\\n", encoding="utf-8")
        except OSError:
            pass
    raise SystemExit(3)
elif sys.argv[1:2] == ["runtime-status"]:
    raise SystemExit(1)
elif sys.argv[1:2] in (["stop"], ["restart"]):
    pass
else:
    raise SystemExit(2)
""",
    )


def _fake_raw_binary_config_publish_race(path: Path, *, version: str) -> None:
    # M003 publish race: generation succeeds but another actor wins FINAL
    # before no-clobber publish. New code (staging) writes staged bytes to
    # STAGING and winner bytes to FINAL, exits 0; installer must preserve
    # winner via failed ln. Old direct-final code is called with FINAL: the
    # fake first records winner then overwrites with staged bytes, proving
    # overwrite; the test expecting winner fails on baseline.
    _exe(
        path,
        f"""#!{sys.executable}
import os
import pathlib
import sys

VERSION = "{version}"

def _report():
    print("kind\\tstandalone-rust")
    print("version\\t" + VERSION)
    print("native\\ttrue")

if sys.argv[1:] == ["install-provenance", "--shell"]:
    _report()
elif sys.argv[1:] == ["version"]:
    print(VERSION)
elif sys.argv[1:2] == ["init-config"]:
    target = pathlib.Path(sys.argv[2])
{_final_config_for_env()}
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if target == final:
        try:
            final.write_bytes(b"concurrent-winner\\n")
        except OSError:
            pass
        try:
            final.write_text("[server]\\nport = 11300\\n", encoding="utf-8")
        except OSError:
            pass
    else:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("[server]\\nport = 11300\\n", encoding="utf-8")
        except OSError:
            pass
        try:
            final.write_bytes(b"concurrent-winner\\n")
        except OSError:
            pass
elif sys.argv[1:2] == ["runtime-status"]:
    raise SystemExit(1)
elif sys.argv[1:2] in (["stop"], ["restart"]):
    pass
else:
    raise SystemExit(2)
""",
    )


def _fake_raw_binary_blocking_init(path: Path, *, version: str) -> None:
    # M003 signal fixture: init-config signals start via marker then blocks
    # so the harness can SIGTERM the installer mid-generation. Works for both
    # staging (new) and direct-final (old) invocation shapes.
    _exe(
        path,
        f"""#!{sys.executable}
import os
import pathlib
import sys
import time

VERSION = "{version}"

def _report():
    print("kind\\tstandalone-rust")
    print("version\\t" + VERSION)
    print("native\\ttrue")

if sys.argv[1:] == ["install-provenance", "--shell"]:
    _report()
elif sys.argv[1:] == ["version"]:
    print(VERSION)
elif sys.argv[1:2] == ["init-config"]:
    target = pathlib.Path(sys.argv[2])
    try:
        home = pathlib.Path(os.environ.get("HOME", str(pathlib.Path.home())))
        (home / "init-started").write_text("started", encoding="utf-8")
    except OSError:
        pass
    time.sleep(30)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[server]\\nport = 11300\\n", encoding="utf-8")
    except OSError:
        pass
elif sys.argv[1:2] == ["runtime-status"]:
    raise SystemExit(1)
elif sys.argv[1:2] in (["stop"], ["restart"]):
    pass
else:
    raise SystemExit(2)
""",
    )


def _fake_manager(path: Path, *, kind: str) -> None:
    bin_variable = "UV_TOOL_BIN_DIR" if kind == "uv-tool" else "PIPX_BIN_DIR"
    _exe(
        path,
        f"""#!{sys.executable}
import os
import pathlib
import sys

pathlib.Path(os.environ["FAKE_MANAGER_LOG"]).open("a", encoding="utf-8").write(
    "\\n".join(sys.argv[1:]) + "\\n"
)
if os.environ.get("FAKE_MANAGER_FAIL") == "1":
    raise SystemExit(17)
version = next(
    (arg.split("==", 1)[1] for arg in sys.argv[1:] if arg.startswith("eggpool==")),
    "0.8.0",
)
bin_dir = pathlib.Path(
    os.environ.get("{bin_variable}", str(pathlib.Path.home() / ".local/bin"))
)
bin_dir.mkdir(parents=True, exist_ok=True)
command = bin_dir / "eggpool"
command.write_text(f'''#!{sys.executable}
import pathlib
import sys

if sys.argv[1:] == ["install-provenance", "--shell"]:
    print("kind\\t{kind}")
    print("version\\t{{version}}")
    print("native\\ttrue")
elif sys.argv[1:] == ["version"]:
    print("{{version}}")
elif sys.argv[1:2] == ["init-config"]:
    target = pathlib.Path(sys.argv[2])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[server]\\\\nport = 11300\\\\n", encoding="utf-8")
elif sys.argv[1:2] == ["runtime-status"]:
    raise SystemExit(1)
elif sys.argv[1:2] in (["stop"], ["restart"]):
    pass
else:
    raise SystemExit(2)
'''.replace("{{version}}", version), encoding="utf-8")
command.chmod(0o755)
""",
    )


def _env(root: Path, fake_bin: Path) -> dict[str, str]:
    return {
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config-home"),
        "XDG_DATA_HOME": str(root / "data-home"),
        "XDG_STATE_HOME": str(root / "state-home"),
        "PATH": os.pathsep.join((str(fake_bin), "/usr/bin", "/bin")),
        "UV_NO_CONFIG": "1",
        "FAKE_MANAGER_LOG": str(root / "manager.log"),
        "FAKE_UPDATE_LOG": str(root / "update.log"),
        "FAKE_MANAGER_FAIL": "0",
        "UV_TOOL_BIN_DIR": str(root / "manager-bin"),
        "PIPX_BIN_DIR": str(root / "manager-bin"),
    }


def _run(
    root: Path,
    *,
    manager: str | None = None,
    manager_kind: str = "uv-tool",
    args: list[str] | None = None,
    existing: tuple[str, str, bool, str] | None = None,
    source: bool = False,
    expected: int = 0,
    manager_failure: bool = False,
    platform: tuple[str, str] | None = None,
    release_fixture: Path | None = None,
    allow_origin: bool = False,
    extra_env: dict[str, str] | None = None,
    pre_hold_lock: bool = False,
    fake_curl: str | None = None,
    fake_python3: str | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_bin = root / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    environment = _env(root, fake_bin)
    (root / "home").mkdir(parents=True, exist_ok=True)
    if extra_env:
        environment.update(extra_env)
    if release_fixture is not None:
        environment["EGGPOOL_RELEASE_BASE_URL"] = f"file://{release_fixture}"
        if allow_origin:
            environment["EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_ORIGIN"] = "1"
    platform = platform or ("Linux", "x86_64")
    if platform:
        _exe(
            fake_bin / "uname",
            f"""#!{sys.executable}
import sys
args = sys.argv[1:]
if args == ["-s"]:
    print({platform[0]!r})
elif args == ["-m"]:
    print({platform[1]!r})
elif not args:
    print({platform[0]!r})
else:
    print({platform[0]!r})
""",
        )
    if fake_curl is not None:
        _exe(fake_bin / "curl", fake_curl)
    if fake_python3 is not None:
        _exe(fake_bin / "python3", fake_python3)
    if manager:
        _fake_manager(fake_bin / manager, kind=manager_kind)
    if existing:
        existing_bin = root / "existing-bin"
        existing_bin.mkdir(parents=True, exist_ok=True)
        path = existing_bin / "eggpool"
        kind, version, native, mode = existing
        if mode == "python-fallback":
            interpreter = fake_bin / "owning-python"
            _exe(
                interpreter,
                f"""#!{sys.executable}
import sys
if sys.argv[1:2] == ["-c"]:
    print("kind\\t" + __import__("os").environ["FAKE_OLD_KIND"])
    print("python\\t" + __import__("os").environ["FAKE_OWNING_PYTHON"])
    print("environment\\t" + __import__("os").environ["FAKE_OLD_ENV"])
    print("version\\t" + __import__("os").environ["FAKE_OLD_VERSION"])
    print("native\\tfalse")
elif sys.argv[1:3] == ["-m", "pip"]:
    import os
    import pathlib
    version = next(
        (arg.split("==", 1)[1] for arg in sys.argv if arg.startswith("eggpool==")),
        "0.8.0",
    )
    target = pathlib.Path(os.environ["FAKE_PIP_BIN"])
    target.write_text(
        f'''#!{sys.executable}
import sys
if sys.argv[1:] == ["install-provenance", "--shell"]:
    print("kind\\tpip")
    print("version\\t{{version}}")
    print("native\\ttrue")
elif sys.argv[1:] == ["version"]:
    print("{{version}}")
'''.replace("{{version}}", version),
        encoding="utf-8",
    )
    target.chmod(0o755)
else:
    raise SystemExit(2)
""",
            )
            environment.update(
                {
                    "FAKE_OLD_KIND": kind,
                    "FAKE_OLD_ENV": str(root / "old-env"),
                    "FAKE_OLD_VERSION": version,
                    "FAKE_PIP_BIN": str(path),
                    "FAKE_OWNING_PYTHON": str(interpreter),
                }
            )
            _exe(path, f"#!{interpreter}\nraise SystemExit(2)\n")
        else:
            _fake_command(
                path,
                kind=kind,
                version=version,
                native=native,
                update_log=str(root / "update.log"),
            )
        environment["PATH"] = os.pathsep.join(
            (str(existing_bin), str(fake_bin), "/usr/bin", "/bin")
        )
    if pre_hold_lock:
        state_dir = Path(environment["XDG_STATE_HOME"]) / "eggpool"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "install.lock.d").mkdir(parents=True, exist_ok=True)
    environment["FAKE_MANAGER_FAIL"] = "1" if manager_failure else "0"
    command = ["bash", str(INSTALLER)] if source else ["bash", "-s", "--"]
    command.extend(args or [])
    input_text = None if source else INSTALLER.read_text(encoding="utf-8")
    result = subprocess.run(
        command,
        cwd=root,
        env=environment,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != expected:
        raise QualificationError(
            f"expected {expected}, got {result.returncode}: "
            f"{result.stdout[-500:]} {result.stderr[-500:]}"
        )
    return result


def _log(root: Path) -> list[str]:
    path = root / "manager.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _update_log(root: Path) -> list[str]:
    path = root / "update.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _make_release(
    releases_dir: Path,
    *,
    version: str,
    raw_os: str,
    raw_arch: str,
    binary_path: Path,
    extra_sidecar_lines: list[str] | None = None,
    sidecar_hash_override: str | None = None,
    omit_sidecar: bool = False,
    omit_asset: bool = False,
    duplicate_raw: bool = False,
    wrong_platform_only: tuple[str, str] | None = None,
) -> None:
    filename = f"eggpool-{version}-{raw_os}-{raw_arch}"
    digest = sidecar_hash_override or _hash(binary_path)
    lines: list[str] = []
    if not omit_sidecar:
        if wrong_platform_only is not None:
            wos, warch = wrong_platform_only
            wfile = f"eggpool-{version}-{wos}-{warch}"
            # Use a valid digest but wrong platform so selector finds zero.
            lines.append(f"{digest}  {wfile}")
        else:
            lines.append(f"{digest}  {filename}")
            if duplicate_raw:
                lines.append(f"{digest}  {filename}")
        # Noise that must never satisfy the raw selector.
        wheel_noise = f"  eggpool-{version}-py3-none.whl"
        lines.append("0" * 64 + wheel_noise)
        helper_noise = f"  eggpool-connect-{version}-linux-x86_64"
        lines.append("1" * 64 + helper_noise)
        if extra_sidecar_lines:
            lines.extend(extra_sidecar_lines)
        sidecar_text = "\n".join(lines) + "\n"
        (releases_dir / "latest" / "download").mkdir(parents=True, exist_ok=True)
        (releases_dir / "download" / f"v{version}").mkdir(parents=True, exist_ok=True)
        (releases_dir / "latest" / "download" / "SHA256SUMS").write_text(
            sidecar_text, encoding="utf-8"
        )
        (releases_dir / "download" / f"v{version}" / "SHA256SUMS").write_text(
            sidecar_text, encoding="utf-8"
        )
    if not omit_asset:
        dest = releases_dir / "download" / f"v{version}" / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(binary_path.read_bytes())
        dest.chmod(0o755)
        latest_asset = releases_dir / "latest" / "download" / filename
        latest_asset.parent.mkdir(parents=True, exist_ok=True)
        if not latest_asset.exists():
            latest_asset.write_bytes(binary_path.read_bytes())
            latest_asset.chmod(0o755)


def _fresh_binary_fixture(
    root: Path,
    *,
    version: str,
    raw_os: str,
    raw_arch: str,
    binary_version: str | None = None,
    kind: str = "standalone-rust",
) -> Path:
    releases = root / "releases"
    candidate = root / "candidate-bin"
    _fake_raw_binary(
        candidate,
        version=binary_version or version,
        kind=kind,
        native=(kind == "standalone-rust"),
    )
    _make_release(
        releases,
        version=version,
        raw_os=raw_os,
        raw_arch=raw_arch,
        binary_path=candidate,
    )
    return releases


# ---- fresh binary authorities (WP-B) ----


def _case_fresh_aarch64_no_python() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="aarch64"
        )
        _run(
            root,
            release_fixture=releases,
            allow_origin=True,
            platform=("Linux", "aarch64"),
        )
        assert not (root / "manager.log").exists()
        dest = root / "home/.local/bin/eggpool"
        assert dest.is_file()
        assert (root / "config-home/eggpool/config.toml").is_file()
        return {"case": "fresh-linux-aarch64-no-python", "status": "pass"}


def _case_fresh_aarch64_stale_pipx_ignored() -> dict[str, str]:
    # Regression: stale pipx with unusable Python must not block native install.
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="aarch64"
        )
        # Present but incompatible pipx: if consulted, it would fail.
        fake_bin = root / "fake-bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        _fake_manager(fake_bin / "pipx", kind="pipx")
        # Make pipx log location known; installer must never invoke it.
        result = _run(
            root,
            manager=None,
            release_fixture=releases,
            allow_origin=True,
            platform=("Linux", "aarch64"),
        )
        # pipx binary exists on PATH but manager.log must be absent/empty.
        assert "raw binary" in result.stdout
        assert not (root / "manager.log").exists() or not _log(root)
        dest = root / "home/.local/bin/eggpool"
        assert dest.is_file()
        return {"case": "fresh-linux-aarch64-stale-pipx-ignored", "status": "pass"}


def _case_fresh_x86_64() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        _run(
            root,
            release_fixture=releases,
            allow_origin=True,
            platform=("Linux", "x86_64"),
        )
        assert (root / "home/.local/bin/eggpool").is_file()
        return {"case": "fresh-linux-x86_64", "status": "pass"}


def _case_fresh_macos() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="macos", raw_arch="aarch64"
        )
        _run(
            root,
            release_fixture=releases,
            allow_origin=True,
            platform=("Darwin", "arm64"),
        )
        assert (root / "home/.local/bin/eggpool").is_file()
        return {"case": "fresh-macos-arm64", "status": "pass"}


def _case_fresh_exact_rust() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        _run(
            root,
            release_fixture=releases,
            allow_origin=True,
            platform=("Linux", "x86_64"),
            args=["--version", "v0.8.1"],
        )
        assert (root / "home/.local/bin/eggpool").is_file()
        return {"case": "fresh-exact-rust", "status": "pass"}


def _case_fresh_package_explicit() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        _run(
            root, manager="uv", manager_kind="uv-tool", args=["--package-manager", "uv"]
        )
        assert _log(root) == ["tool", "install", "eggpool"]
        return {"case": "fresh-package-explicit", "status": "pass"}


# ---- negative checksum / download paths (WP-D) ----


def _case_missing_sidecar() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = root / "releases"
        releases.mkdir()
        result = _run(
            root,
            release_fixture=releases,
            allow_origin=True,
            expected=1,
        )
        assert (
            "checksum sidecar" in result.stderr or "could not download" in result.stderr
        )
        assert not (root / "home/.local/bin/eggpool").exists()
        assert not (root / "config-home/eggpool/config.toml").exists()
        return {"case": "missing-SHA256SUMS", "status": "pass"}


def _case_zero_matching() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        candidate = root / "candidate-bin"
        _fake_raw_binary(candidate, version="0.8.1")
        releases = root / "releases"
        _make_release(
            releases,
            version="0.8.1",
            raw_os="linux",
            raw_arch="x86_64",
            binary_path=candidate,
            wrong_platform_only=("linux", "aarch64"),
        )
        result = _run(
            root,
            release_fixture=releases,
            allow_origin=True,
            platform=("Linux", "x86_64"),
            expected=1,
        )
        assert "no matching raw entry" in result.stderr
        return {"case": "zero-matching-raw", "status": "pass"}


def _case_multiple_matching() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        candidate = root / "candidate-bin"
        _fake_raw_binary(candidate, version="0.8.1")
        releases = root / "releases"
        _make_release(
            releases,
            version="0.8.1",
            raw_os="linux",
            raw_arch="x86_64",
            binary_path=candidate,
            duplicate_raw=True,
        )
        result = _run(
            root,
            release_fixture=releases,
            allow_origin=True,
            expected=1,
        )
        assert "ambiguous" in result.stderr
        return {"case": "multiple-matching-raw", "status": "pass"}


def _case_malformed_digest() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        candidate = root / "candidate-bin"
        _fake_raw_binary(candidate, version="0.8.1")
        releases = root / "releases"
        (releases / "latest" / "download").mkdir(parents=True)
        (releases / "download" / "v0.8.1").mkdir(parents=True)
        bad = "ZZ" + "0" * 62 + "  eggpool-0.8.1-linux-x86_64\n"
        (releases / "latest" / "download" / "SHA256SUMS").write_text(bad)
        (releases / "download" / "v0.8.1" / "SHA256SUMS").write_text(bad)
        dest = releases / "download" / "v0.8.1" / "eggpool-0.8.1-linux-x86_64"
        dest.write_bytes(candidate.read_bytes())
        dest.chmod(0o755)
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "malformed digest" in result.stderr or "malformed" in result.stderr
        return {"case": "malformed-digest", "status": "pass"}


def _case_checksum_mismatch() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        candidate = root / "candidate-bin"
        _fake_raw_binary(candidate, version="0.8.1")
        releases = root / "releases"
        _make_release(
            releases,
            version="0.8.1",
            raw_os="linux",
            raw_arch="x86_64",
            binary_path=candidate,
            sidecar_hash_override="0" * 64,
        )
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "checksum mismatch" in result.stderr
        assert not (root / "home/.local/bin/eggpool").exists()
        assert not (root / "config-home/eggpool/config.toml").exists()
        return {"case": "checksum-mismatch", "status": "pass"}


def _case_oversized() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        candidate = root / "candidate-bin"
        # Sparse 129 MiB file that still reports its size; header is a shell
        # script so hash tooling works, size bound must reject it.
        candidate.write_bytes(b"#!/bin/sh\necho oversized\n")
        # Extend sparsely to 129 MiB without writing all bytes.
        with candidate.open("r+b") as handle:
            handle.truncate(129 * 1024 * 1024)
        candidate.chmod(0o755)
        releases = root / "releases"
        _make_release(
            releases,
            version="0.8.1",
            raw_os="linux",
            raw_arch="x86_64",
            binary_path=candidate,
        )
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "128 MiB" in result.stderr or "exceeds" in result.stderr
        return {"case": "oversized-artifact", "status": "pass"}


def _case_truncated_download() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        candidate = root / "candidate-bin"
        _fake_raw_binary(candidate, version="0.8.1")
        releases = root / "releases"
        _make_release(
            releases,
            version="0.8.1",
            raw_os="linux",
            raw_arch="x86_64",
            binary_path=candidate,
            omit_asset=True,
        )
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "could not download" in result.stderr
        assert not (root / "home/.local/bin/eggpool").exists()
        return {"case": "truncated-download", "status": "pass"}


def _case_wrong_target_filename() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        candidate = root / "candidate-bin"
        _fake_raw_binary(candidate, version="0.8.1")
        releases = root / "releases"
        # Sidecar only knows 0.8.0 for this platform; exact 0.8.1 must fail.
        _make_release(
            releases,
            version="0.8.0",
            raw_os="linux",
            raw_arch="x86_64",
            binary_path=candidate,
        )
        # Also create an empty 0.8.1 sidecar dir so exact URL exists but has no match.
        (releases / "download" / "v0.8.1").mkdir(parents=True, exist_ok=True)
        (releases / "download" / "v0.8.1" / "SHA256SUMS").write_text(
            "0" * 64 + "  eggpool-0.8.0-linux-x86_64\n"
        )
        result = _run(
            root,
            release_fixture=releases,
            allow_origin=True,
            args=["--version", "0.8.1"],
            expected=1,
        )
        assert (
            "no matching raw entry" in result.stderr
            or "does not match" in result.stderr
        )
        return {"case": "wrong-target-filename", "status": "pass"}


def _case_staged_wrong_version() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        # Binary reports 0.8.0 but sidecar filename says 0.8.1 with matching hash.
        candidate = root / "candidate-bin"
        _fake_raw_binary(candidate, version="0.8.0")
        releases = root / "releases"
        _make_release(
            releases,
            version="0.8.1",
            raw_os="linux",
            raw_arch="x86_64",
            binary_path=candidate,
        )
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "wrong version" in result.stderr
        return {"case": "staged-wrong-version", "status": "pass"}


def _case_staged_not_native() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        candidate = root / "candidate-bin"
        _fake_raw_binary(candidate, version="0.8.1", kind="uv-tool", native=False)
        # Override provenance to claim non-standalone but same version.
        releases = root / "releases"
        _make_release(
            releases,
            version="0.8.1",
            raw_os="linux",
            raw_arch="x86_64",
            binary_path=candidate,
        )
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "standalone" in result.stderr
        return {"case": "staged-not-native", "status": "pass"}


# ---- destination / concurrency (WP-D) ----


def _case_dest_symlink() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        dest_dir = root / "home/.local/bin"
        dest_dir.mkdir(parents=True)
        (dest_dir / "eggpool").symlink_to("/tmp/unrelated")
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "symlink" in result.stderr
        assert not (root / "config-home/eggpool/config.toml").exists()
        return {"case": "dest-symlink-collision", "status": "pass"}


def _case_dest_special() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        dest_dir = root / "home/.local/bin"
        dest_dir.mkdir(parents=True)
        try:
            os.mkfifo(dest_dir / "eggpool")
        except OSError:
            (dest_dir / "eggpool").mkdir()
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "regular file" in result.stderr or "symlink" in result.stderr
        return {"case": "dest-special-collision", "status": "pass"}


def _case_dest_unrelated() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        dest_dir = root / "home/.local/bin"
        dest_dir.mkdir(parents=True)
        (dest_dir / "eggpool").write_text("unrelated\n")
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "already exists" in result.stderr
        assert (dest_dir / "eggpool").read_text() == "unrelated\n"
        return {"case": "dest-unrelated-collision", "status": "pass"}


def _case_fresh_force_unowned_refusal() -> dict[str, str]:
    # M002 Finding A: fresh `--force` must not replace an unowned regular
    # file. No EggPool command is on PATH, so the destination is a collision
    # even with `--force`.
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        dest_dir = root / "home/.local/bin"
        dest_dir.mkdir(parents=True)
        dest = dest_dir / "eggpool"
        dest.write_bytes(b"unrelated\n")
        dest.chmod(0o755)
        before_bytes = dest.read_bytes()
        before_mode = dest.stat().st_mode & 0o777
        result = _run(
            root,
            release_fixture=releases,
            allow_origin=True,
            args=["--force"],
            expected=1,
        )
        assert (
            "not a verified" in result.stderr
            or "cannot overwrite" in result.stderr
            or "collision" in result.stderr
        )
        assert "--force" in result.stderr
        assert dest.read_bytes() == before_bytes
        assert (dest.stat().st_mode & 0o777) == before_mode
        assert not (root / "config-home/eggpool/config.toml").exists()
        assert not (root / "manager.log").exists()
        return {"case": "fresh-force-unowned-regular-refusal", "status": "pass"}


def _fresh_failing_release(root: Path, *, version: str, kind: str = "failing") -> Path:
    releases = root / "releases"
    candidate = root / "candidate-bin"
    if kind == "race":
        _fake_raw_binary_race_on_config(candidate, version=version)
    elif kind == "config-concurrent-writer":
        _fake_raw_binary_config_concurrent_writer(candidate, version=version)
    elif kind == "config-publish-race":
        _fake_raw_binary_config_publish_race(candidate, version=version)
    elif kind == "blocking-init":
        _fake_raw_binary_blocking_init(candidate, version=version)
    else:
        _fake_raw_binary_failing_init(candidate, version=version)
    _make_release(
        releases,
        version=version,
        raw_os="linux",
        raw_arch="x86_64",
        binary_path=candidate,
    )
    return releases


def _assert_no_staging_residue(root: Path) -> None:
    config_dir = root / "config-home/eggpool"
    if not config_dir.is_dir():
        return
    leftovers = list(config_dir.glob(".eggpool-config-staging.*"))
    assert leftovers == [], f"staging residue: {leftovers}"
    # No nested staging dirs either.
    for child in config_dir.iterdir():
        assert not child.name.startswith(".eggpool-config-staging"), child


def _case_fresh_init_config_failure_rollback() -> dict[str, str]:
    # M002 Finding B: first-time `init-config` failure after executable commit
    # must roll back the executable and any partial config created by this
    # invocation.
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_failing_release(root, version="0.8.1", kind="failing")
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "init-config" in result.stderr or "config" in result.stderr
        assert not (root / "home/.local/bin/eggpool").exists()
        assert not (root / "config-home/eggpool/config.toml").exists()
        # Lock and temp state must be released so a later clean install can
        # proceed; the lock dir is removed by the installer trap.
        assert not (root / "state-home/eggpool/install.lock.d").exists()
        return {"case": "fresh-init-config-failure-rolls-back-binary", "status": "pass"}


def _case_fresh_init_failure_preserves_preexisting() -> dict[str, str]:
    # When config already exists, fresh install never invokes `init-config`
    # (seed-after-commit is skipped by dispatch), so a failing `init-config`
    # implementation cannot trigger rollback. This case proves pre-existing
    # config is preserved and documents why the failure-with-existing-config
    # state is impossible by dispatch.
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_failing_release(root, version="0.8.1", kind="failing")
        config = root / "config-home/eggpool/config.toml"
        config.parent.mkdir(parents=True)
        config.write_bytes(b"operator-config\n")
        _run(root, release_fixture=releases, allow_origin=True, expected=0)
        assert (root / "home/.local/bin/eggpool").is_file()
        assert config.read_bytes() == b"operator-config\n"
        return {
            "case": "fresh-init-config-failure-preserves-preexisting-config",
            "status": "pass",
        }


def _case_fresh_config_failure_race_refusal() -> dict[str, str]:
    # Rollback must prove it is removing the candidate committed by this
    # transaction. If the destination changed after commit, it must not be
    # deleted and the installer must emit manual recovery guidance.
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_failing_release(root, version="0.8.1", kind="race")
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        dest = root / "home/.local/bin/eggpool"
        assert dest.is_file()
        assert dest.read_bytes() == b"racing\n"
        combined = result.stderr + result.stdout
        assert (
            "manual recovery" in combined
            or "changed" in combined
            or "could not be proven safe" in combined
        )
        return {
            "case": "fresh-config-failure-destination-race-refusal",
            "status": "pass",
        }


def _case_fresh_config_concurrent_writer_preserved() -> dict[str, str]:
    # M003 primary race: concurrent operator creates FINAL while staged
    # generation fails. Installer must preserve FINAL byte-for-byte, clean
    # staging, and roll back DEST. Baseline deletes FINAL (absent-before
    # implies ownership) and fails this assertion.
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_failing_release(
            root, version="0.8.1", kind="config-concurrent-writer"
        )
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "init-config" in result.stderr or "config" in result.stderr
        final = root / "config-home/eggpool/config.toml"
        assert final.is_file()
        assert final.read_bytes() == b"operator-config-concurrent\n"
        assert not (root / "home/.local/bin/eggpool").exists()
        _assert_no_staging_residue(root)
        assert not (root / "state-home/eggpool/install.lock.d").exists()
        return {"case": "fresh-config-concurrent-writer-preserved", "status": "pass"}


def _case_fresh_config_publish_race_preserves_winner() -> dict[str, str]:
    # M003 publish race: staged generation succeeds but another actor wins
    # FINAL before no-clobber publish. Installer must preserve the winner
    # and discard staging. Baseline overwrites FINAL with staged bytes.
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_failing_release(
            root, version="0.8.1", kind="config-publish-race"
        )
        result = _run(root, release_fixture=releases, allow_origin=True, expected=0)
        final = root / "config-home/eggpool/config.toml"
        assert final.is_file()
        assert final.read_bytes() == b"concurrent-winner\n"
        assert (root / "home/.local/bin/eggpool").is_file()
        assert (
            "concurrent" in (result.stdout + result.stderr).lower()
            or "preserved" in (result.stdout + result.stderr).lower()
        )
        _assert_no_staging_residue(root)
        return {"case": "fresh-config-publish-race-preserves-winner", "status": "pass"}


def _case_fresh_staged_first_config_created() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        _run(root, release_fixture=releases, allow_origin=True, expected=0)
        final = root / "config-home/eggpool/config.toml"
        assert final.is_file()
        assert b"port = 11300" in final.read_bytes()
        assert (root / "home/.local/bin/eggpool").is_file()
        _assert_no_staging_residue(root)
        return {"case": "fresh-staged-first-config-created", "status": "pass"}


def _case_fresh_staged_generation_failure_leaves_final_absent() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_failing_release(root, version="0.8.1", kind="failing")
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "init-config" in result.stderr or "config" in result.stderr
        assert not (root / "config-home/eggpool/config.toml").exists()
        assert not (root / "home/.local/bin/eggpool").exists()
        _assert_no_staging_residue(root)
        return {
            "case": "fresh-staged-generation-failure-leaves-final-absent",
            "status": "pass",
        }


def _case_fresh_staged_partial_cleaned() -> dict[str, str]:
    # Failing staged generation must not leave a partial staging file or
    # directory behind, and must never create FINAL.
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_failing_release(root, version="0.8.1", kind="failing")
        _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert not (root / "config-home/eggpool/config.toml").exists()
        _assert_no_staging_residue(root)
        config_dir = root / "config-home/eggpool"
        if config_dir.is_dir():
            names = [p.name for p in config_dir.iterdir()]
            assert all("partial" not in n for n in names)
        return {
            "case": "fresh-staged-partial-cleaned-after-generation-failure",
            "status": "pass",
        }


def _case_fresh_config_symlink_refusal() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        config = root / "config-home/eggpool/config.toml"
        config.parent.mkdir(parents=True)
        target = root / "config-home/operator-target.toml"
        target.write_bytes(b"operator-target\n")
        config.symlink_to(target)
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert "symlink" in result.stderr.lower()
        assert config.is_symlink()
        assert target.read_bytes() == b"operator-target\n"
        assert not (root / "home/.local/bin/eggpool").exists()
        _assert_no_staging_residue(root)
        return {"case": "fresh-config-symlink-refusal", "status": "pass"}


def _case_fresh_config_special_refusal() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        config = root / "config-home/eggpool/config.toml"
        config.parent.mkdir(parents=True)
        try:
            os.mkfifo(config)
            is_fifo = True
        except OSError:
            config.mkdir()
            is_fifo = False
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        combined = (result.stderr + result.stdout).lower()
        assert (
            "regular file" in combined
            or "refusing" in combined
            or "not a regular" in combined
        )
        if is_fifo:
            import stat as _stat

            mode = os.stat(config).st_mode
            assert _stat.S_ISFIFO(mode)
        assert not (root / "home/.local/bin/eggpool").exists()
        _assert_no_staging_residue(root)
        return {"case": "fresh-config-special-refusal", "status": "pass"}


def _case_fresh_config_noclobber_never_overwrites() -> dict[str, str]:
    # Distinct winner bytes from the publish-race case to prove byte-for-byte
    # preservation is not keyed to a single constant.
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_failing_release(
            root, version="0.8.1", kind="config-publish-race"
        )
        _run(root, release_fixture=releases, allow_origin=True, expected=0)
        final = root / "config-home/eggpool/config.toml"
        content = final.read_bytes()
        assert content == b"concurrent-winner\n"
        assert b"port = 11300" not in content
        _assert_no_staging_residue(root)
        return {"case": "fresh-config-noclobber-never-overwrites", "status": "pass"}


def _case_fresh_signal_cleanup_removes_staging() -> dict[str, str]:
    # Signal during staged generation must remove transaction staging but
    # never create or delete FINAL. Synchronized on the fake's init-started
    # marker under a bounded timeout (no fixed sleeps for readiness).
    import signal
    import time

    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_failing_release(root, version="0.8.1", kind="blocking-init")
        fake_bin = root / "fake-bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        environment = _env(root, fake_bin)
        (root / "home").mkdir(parents=True, exist_ok=True)
        environment["EGGPOOL_RELEASE_BASE_URL"] = f"file://{releases}"
        environment["EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_ORIGIN"] = "1"
        _exe(
            fake_bin / "uname",
            f"""#!{sys.executable}
import sys
args = sys.argv[1:]
if args == ["-s"]:
    print("Linux")
elif args == ["-m"]:
    print("x86_64")
else:
    print("Linux")
""",
        )
        proc = subprocess.Popen(
            ["bash", "-s", "--"],
            cwd=root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert proc.stdin is not None
        try:
            proc.stdin.write(INSTALLER.read_text(encoding="utf-8"))
            proc.stdin.close()
            marker = root / "home/init-started"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if marker.exists():
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            assert marker.exists(), "init-config did not start under timeout"
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=10)
            assert proc.returncode is not None and proc.returncode != 0
        finally:
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=10)
        final = root / "config-home/eggpool/config.toml"
        assert not final.exists() or final.read_bytes() != b"[server]\nport = 11300\n"
        _assert_no_staging_residue(root)
        assert not (root / "state-home/eggpool/install.lock.d").exists()
        return {"case": "fresh-signal-cleanup-removes-staging", "status": "pass"}


def _case_fresh_executable_rollback_on_generation_failure() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_failing_release(root, version="0.8.1", kind="failing")
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        assert (
            "rolled back" in (result.stderr + result.stdout).lower()
            or "config" in result.stderr.lower()
        )
        assert not (root / "home/.local/bin/eggpool").exists()
        assert not (root / "config-home/eggpool/config.toml").exists()
        _assert_no_staging_residue(root)
        return {
            "case": "fresh-executable-rollback-on-config-generation-failure",
            "status": "pass",
        }


def _case_fresh_executable_race_still_preserved() -> dict[str, str]:
    # M002 executable-race guard must remain intact under M003 staging.
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_failing_release(root, version="0.8.1", kind="race")
        result = _run(root, release_fixture=releases, allow_origin=True, expected=1)
        dest = root / "home/.local/bin/eggpool"
        assert dest.is_file()
        assert dest.read_bytes() == b"racing\n"
        combined = result.stderr + result.stdout
        assert (
            "manual recovery" in combined
            or "could not be proven safe" in combined
            or "changed" in combined
        )
        return {"case": "fresh-executable-race-still-preserved", "status": "pass"}


def _case_existing_owner_first_config_uses_safe_staging() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        config = root / "config-home/eggpool/config.toml"
        assert not config.exists()
        _run(
            root,
            manager=None,
            existing=("standalone-rust", "0.8.1", True, "rust"),
        )
        assert config.is_file()
        assert b"port = 11300" in config.read_bytes()
        _assert_no_staging_residue(root)
        return {
            "case": "existing-owner-first-config-uses-safe-staging",
            "status": "pass",
        }


def _case_existing_owner_concurrent_preserved() -> dict[str, str]:
    # Existing-owner seed must also use no-clobber staging: pre-existing
    # config is preserved and never treated as staging scratch.
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        config = root / "config-home/eggpool/config.toml"
        config.parent.mkdir(parents=True)
        config.write_bytes(b"operator-existing\n")
        _run(
            root,
            manager=None,
            existing=("standalone-rust", "0.8.1", True, "rust"),
        )
        assert config.read_bytes() == b"operator-existing\n"
        _assert_no_staging_residue(root)
        return {"case": "existing-owner-concurrent-config-preserved", "status": "pass"}


def _case_existing_ownership_unchanged() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        _run(
            root,
            manager=None,
            args=["--version", "0.8.1"],
            existing=("uv-tool", "0.8.0", True, "rust"),
        )
        assert _update_log(root) != []
        _assert_no_staging_residue(root)
        return {"case": "existing-owner-package-standalone-unchanged", "status": "pass"}


def _case_lock_contention() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        result = _run(
            root,
            release_fixture=releases,
            allow_origin=True,
            expected=1,
            pre_hold_lock=True,
        )
        assert "already in progress" in result.stderr
        return {"case": "lock-contention", "status": "pass"}


def _case_target_race() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        # Fake curl creates DEST during asset fetch to simulate a race.
        # Simpler robust wrapper: delegate to system curl, side-effect first.
        fake_simple = f"""#!{sys.executable}
import os
import pathlib
import subprocess
import sys
home = os.environ.get("HOME", "")
dest = pathlib.Path(home) / ".local/bin/eggpool"
if any("eggpool-0.8.1-linux-" in a for a in sys.argv[1:]):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_text("racing\\n", encoding="utf-8")
result = subprocess.run(["/usr/bin/curl"] + sys.argv[1:])
raise SystemExit(result.returncode)
"""
        # Fall back to `curl` from PATH if /usr/bin/curl is missing.
        if not Path("/usr/bin/curl").exists():
            fake_simple = fake_simple.replace("/usr/bin/curl", "curl")
        result = _run(
            root,
            release_fixture=releases,
            allow_origin=True,
            expected=1,
            fake_curl=fake_simple,
        )
        assert (
            "changed during install" in result.stderr
            or "already exists" in result.stderr
            or "appeared during install" in result.stderr
        )
        return {"case": "target-race-refusal", "status": "pass"}


# ---- existing owners (WP-C) ----


def _case_existing_standalone_delegates() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        config = root / "config-home/eggpool/config.toml"
        config.parent.mkdir(parents=True)
        config.write_bytes(b"operator-config\n")
        _run(
            root,
            manager=None,
            existing=("standalone-rust", "0.8.1", True, "rust"),
        )
        assert _update_log(root) != []
        assert "update" in " ".join(_update_log(root))
        assert not (root / "manager.log").exists() or not _log(root)
        assert config.read_bytes() == b"operator-config\n"
        return {"case": "existing-standalone-delegates", "status": "pass"}


def _case_existing_uv_retained() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        _run(
            root,
            manager=None,
            args=["--version", "0.8.1"],
            existing=("uv-tool", "0.8.0", True, "rust"),
        )
        assert _update_log(root) != []
        assert "0.8.1" in " ".join(_update_log(root))
        return {"case": "existing-uv-retained", "status": "pass"}


def _case_existing_pipx_retained() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        _run(
            root,
            manager=None,
            args=["--version", "0.8.1"],
            existing=("pipx", "0.8.0", True, "rust"),
        )
        assert _update_log(root) != []
        return {"case": "existing-pipx-retained", "status": "pass"}


def _case_existing_pip_retained() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        _run(
            root,
            manager=None,
            existing=("pip", "0.8.0", True, "rust"),
        )
        assert _update_log(root) != []
        return {"case": "existing-pip-retained", "status": "pass"}


def _case_existing_legacy_uv() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        _run(
            root,
            manager="uv",
            manager_kind="uv-tool",
            args=["--version", "v0.8.0"],
            existing=("uv-tool", "0.7.4", False, "python-fallback"),
        )
        assert _log(root) == ["tool", "install", "--force", "eggpool==0.8.0"]
        return {"case": "existing-legacy-uv", "status": "pass"}


def _case_standalone_adoption() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        old = root / "existing-bin/eggpool"
        old.parent.mkdir(parents=True, exist_ok=True)
        _fake_command(old, kind="standalone-rust", version="0.8.0", native=True)
        _run(
            root,
            manager="uv",
            args=["--adopt-standalone", "--version", "0.8.0"],
            existing=("standalone-rust", "0.8.0", True, "rust"),
        )
        backup = old.with_name("eggpool.eggpool-standalone-0.8.0.rollback")
        assert backup.is_file() and not old.exists()
        return {"case": "standalone-adoption", "status": "pass"}


def _case_force_repair_standalone() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        _run(
            root,
            manager=None,
            args=["--force"],
            existing=("standalone-rust", "0.8.1", True, "rust"),
        )
        assert _update_log(root) != []
        existing_bin = root / "existing-bin/eggpool"
        assert existing_bin.is_file()
        return {"case": "force-repair-standalone", "status": "pass"}


def _case_existing_config_preserved() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        config = root / "config-home/eggpool/config.toml"
        config.parent.mkdir(parents=True)
        config.write_bytes(b"operator-config\n")
        database = root / "data-home/eggpool/usage.sqlite3"
        database.parent.mkdir(parents=True)
        database.write_bytes(b"operator-database\n")
        _run(
            root,
            manager=None,
            existing=("standalone-rust", "0.8.1", True, "rust"),
        )
        assert config.read_bytes() == b"operator-config\n"
        assert database.read_bytes() == b"operator-database\n"
        return {"case": "existing-config-preserved", "status": "pass"}


def _case_historical_exact() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        fake_py = f"""#!{sys.executable}
import sys
print("3.11")
"""
        _run(
            root,
            manager="uv",
            manager_kind="uv-tool",
            args=["--version", "0.7.4"],
            fake_python3=fake_py,
        )
        assert _log(root) == ["tool", "install", "--force", "eggpool==0.7.4"]
        assert not (root / "home/.local/bin/eggpool").exists()
        return {"case": "historical-exact-compat", "status": "pass"}


def _case_historical_python_incompat() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        # Always report 3.9 for any -c probe.
        fake_py_simple = f"""#!{sys.executable}
import sys
print("3.9")
"""
        result = _run(
            root,
            manager="uv",
            args=["--version", "0.7.4"],
            expected=1,
            fake_python3=fake_py_simple,
        )
        assert "Python" in result.stderr and "0.7.4" in result.stderr
        assert not (root / "manager.log").exists()
        return {"case": "historical-python-incompat", "status": "pass"}


def _case_standalone_historical_refusal() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        result = _run(
            root,
            manager="uv",
            args=["--version", "0.7.4"],
            existing=("standalone-rust", "0.8.1", True, "rust"),
            expected=1,
        )
        assert "standalone" in result.stderr and "0.7.4" in result.stderr
        return {"case": "standalone-historical-refusal", "status": "pass"}


# ---- legacy negative / misc ----


def _negative_cases() -> list[dict[str, str]]:
    cases: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        result = _run(
            Path(value),
            manager="uv",
            existing=("ambiguous", "0.7.4", False, "rust"),
            expected=1,
        )
        assert "ambiguous" in result.stderr
        cases.append({"case": "ambiguous-refusal", "status": "pass"})
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        manager_bin = root / "manager-bin"
        manager_bin.mkdir(parents=True)
        (manager_bin / "eggpool").write_text("unrelated\n", encoding="utf-8")
        fake_py = f"""#!{sys.executable}
import sys
print("3.11")
"""
        result = _run(
            root,
            manager="uv",
            args=["--version", "0.7.4"],
            expected=1,
            fake_python3=fake_py,
        )
        assert "collision" in result.stderr
        cases.append({"case": "manager-path-collision", "status": "pass"})
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        _exe(fake_bin / "id", "#!/bin/sh\nprintf '0\\n'\n")
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        result = _run(
            root, manager="uv", release_fixture=releases, allow_origin=True, expected=1
        )
        assert "refuses root" in result.stderr
        cases.append({"case": "root-refusal", "status": "pass"})
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        result = _run(Path(value), manager="uv", args=["--unknown"], expected=2)
        assert "Unknown argument" in result.stderr
        cases.append({"case": "unknown-argument-exit-2", "status": "pass"})
    with tempfile.TemporaryDirectory(prefix="eggpool-installer-") as value:
        root = Path(value)
        result = _run(root, manager="uv", args=["--version", "0.1.0"], expected=1)
        assert "not in the schema-compatible catalog" in result.stderr
        assert not (root / "manager.log").exists()
        cases.append({"case": "uncatalogued-historical-refusal", "status": "pass"})
    with tempfile.TemporaryDirectory(prefix="eggpool-installer-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        result = _run(
            root,
            manager="uv",
            release_fixture=releases,
            allow_origin=True,
            platform=("FreeBSD", "x86_64"),
            expected=1,
        )
        assert "unsupported platform" in result.stderr
        cases.append({"case": "unsupported-platform-refusal", "status": "pass"})
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        result = _run(
            Path(value),
            manager=None,
            existing=("source-checkout", "0.8.1", True, "rust"),
            expected=1,
        )
        assert "source checkout" in result.stderr
        cases.append({"case": "source-checkout-refusal", "status": "pass"})
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        releases = _fresh_binary_fixture(
            root, version="0.8.1", raw_os="linux", raw_arch="x86_64"
        )
        # Custom origin without opt-in must fail before any mutation.
        result = _run(root, release_fixture=releases, allow_origin=False, expected=1)
        assert "non-production release origin requires" in result.stderr
        assert not (root / "home/.local/bin/eggpool").exists()
        cases.append({"case": "nonprod-origin-optin", "status": "pass"})
    return cases


def _source_case() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        result = _run(root, manager="uv", source=True)
        log = _log(root)
        assert str(ROOT / "packaging/pypi") in log and "eggpool" not in log
        assert "Using source checkout" in result.stdout
        return {"case": "source-checkout-local-candidate", "status": "pass"}


def _release_manifest_case() -> dict[str, str]:
    manifest = json.loads(
        (ROOT / "packaging/release/release-manifest.json").read_text()
    )
    raws = {r["raw"]["filename"] for r in manifest["artifacts"]}
    assert raws == {
        "eggpool-0.8.2-linux-x86_64",
        "eggpool-0.8.2-linux-aarch64",
        "eggpool-0.8.2-macos-aarch64",
    }
    # Wheel/helper entries must never satisfy the raw selector: simulate the
    # installer's basename + pattern gate.
    import re

    raw_re = re.compile(
        r"^eggpool-[0-9]+\.[0-9]+\.[0-9]+-(linux|macos)-(x86_64|aarch64)$"
    )
    wheels = [r["wheel"]["filename"] for r in manifest["artifacts"]]
    assert all(not raw_re.fullmatch(w.split("/")[-1]) for w in wheels)
    helpers = [c["filename"] for c in manifest.get("connect_artifacts", [])]
    assert all(not raw_re.fullmatch(h) for h in helpers)
    return {"case": "release-manifest-raw-contract", "status": "pass"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    results = [
        _case_fresh_aarch64_no_python(),
        _case_fresh_aarch64_stale_pipx_ignored(),
        _case_fresh_x86_64(),
        _case_fresh_macos(),
        _case_fresh_exact_rust(),
        _case_fresh_package_explicit(),
        _case_missing_sidecar(),
        _case_zero_matching(),
        _case_multiple_matching(),
        _case_malformed_digest(),
        _case_checksum_mismatch(),
        _case_oversized(),
        _case_truncated_download(),
        _case_wrong_target_filename(),
        _case_staged_wrong_version(),
        _case_staged_not_native(),
        _case_dest_symlink(),
        _case_dest_special(),
        _case_dest_unrelated(),
        _case_fresh_force_unowned_refusal(),
        _case_fresh_init_config_failure_rollback(),
        _case_fresh_init_failure_preserves_preexisting(),
        _case_fresh_config_failure_race_refusal(),
        _case_fresh_config_concurrent_writer_preserved(),
        _case_fresh_config_publish_race_preserves_winner(),
        _case_fresh_staged_first_config_created(),
        _case_fresh_staged_generation_failure_leaves_final_absent(),
        _case_fresh_staged_partial_cleaned(),
        _case_fresh_config_symlink_refusal(),
        _case_fresh_config_special_refusal(),
        _case_fresh_config_noclobber_never_overwrites(),
        _case_fresh_signal_cleanup_removes_staging(),
        _case_fresh_executable_rollback_on_generation_failure(),
        _case_fresh_executable_race_still_preserved(),
        _case_existing_owner_first_config_uses_safe_staging(),
        _case_existing_owner_concurrent_preserved(),
        _case_existing_ownership_unchanged(),
        _case_lock_contention(),
        _case_target_race(),
        _case_existing_standalone_delegates(),
        _case_existing_uv_retained(),
        _case_existing_pipx_retained(),
        _case_existing_pip_retained(),
        _case_existing_legacy_uv(),
        _case_standalone_adoption(),
        _case_force_repair_standalone(),
        _case_existing_config_preserved(),
        _case_historical_exact(),
        _case_historical_python_incompat(),
        _case_standalone_historical_refusal(),
        _source_case(),
        _release_manifest_case(),
        *_negative_cases(),
    ]
    print(json.dumps({"cases": results, "status": "pass"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
