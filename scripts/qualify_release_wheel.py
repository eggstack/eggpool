#!/usr/bin/env python3
"""Qualify a built release wheel in disposable package-manager environments."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from inspect_release_wheel import WheelInspectionError, inspect_wheel

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 90


class QualificationError(RuntimeError):
    """A disposable install or runtime qualification failure."""


def _prefix(target_class: str) -> list[str]:
    if (
        target_class == "macos-arm64"
        and platform.system() == "Darwin"
        and platform.machine().lower() in {"x86_64", "amd64"}
    ):
        arch = shutil.which("arch")
        if arch:
            return [arch, "-arm64"]
    return []


def _run(command: list[str], *, env: dict[str, str], cwd: Path | None = None) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QualificationError("bounded subprocess execution failed") from error
    if result.returncode:
        raise QualificationError(f"subprocess failed ({result.returncode})")


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _base_environment(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
        "XDG_RUNTIME_DIR": str(root / "xdg-runtime"),
    }


def _check_config_and_health(
    executable: Path, *, prefix: list[str], root: Path, env: dict[str, str]
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    config = root / "config.toml"
    config_text = (ROOT / "config.sbc.example.toml").read_text(encoding="utf-8")
    config.write_text(
        config_text.replace("port = 11300", f"port = {_free_port()}"), encoding="utf-8"
    )
    command = prefix + [str(executable), "--config", str(config), "check-config"]
    _run(command, env=env, cwd=root)
    port = next(
        int(line.split("=", 1)[1].strip())
        for line in config.read_text(encoding="utf-8").splitlines()
        if line.startswith("port =")
    )
    server = subprocess.Popen(
        prefix + [str(executable), "--config", str(config), "serve", "--verbose"],
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/healthz", timeout=1
                ) as response:
                    if response.status == 200:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/static/dashboard.css",
                            timeout=1,
                        ) as asset:
                            if asset.status != 200 or not asset.read(64):
                                raise QualificationError(
                                    "wheel-installed dashboard asset was unavailable"
                                )
                        return
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        raise QualificationError("wheel-installed server did not answer healthz")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def _pip_qualification(wheel: Path, root: Path, prefix: list[str]) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    environment = _base_environment(root)
    venv = root / "venv"
    _run(prefix + [sys.executable, "-m", "venv", str(venv)], env=environment, cwd=root)
    python = venv / "bin/python"
    executable = venv / "bin/eggpool"
    python_command = prefix + [str(python)]
    _run(
        python_command
        + ["-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
        env=environment,
        cwd=root,
    )
    _run(prefix + [str(executable), "version"], env=environment, cwd=root)
    _run(prefix + [str(executable), "help"], env=environment, cwd=root)
    _check_config_and_health(executable, prefix=prefix, root=root, env=environment)
    _run(
        python_command + ["-m", "pip", "uninstall", "-y", "eggpool"],
        env=environment,
        cwd=root,
    )
    if executable.exists():
        raise QualificationError("pip uninstall left the wheel-owned executable")
    _run(
        python_command
        + ["-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
        env=environment,
        cwd=root,
    )
    return {"status": "pass", "version_command": "pass", "uninstall": "pass"}


def _uv_qualification(
    wheel: Path, root: Path, prefix: list[str], target_class: str
) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    if uv is None:
        return {"status": "skipped", "reason": "uv unavailable"}
    environment = _base_environment(root)
    environment.update(
        {
            "UV_TOOL_DIR": str(root / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(root / "uv-bin"),
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "UV_NO_CONFIG": "1",
        }
    )
    uv_platform = {
        "linux-x86_64": "x86_64-manylinux2014",
        "linux-aarch64": "aarch64-manylinux2014",
        "macos-arm64": "aarch64-apple-darwin",
    }[target_class]
    try:
        _run(
            [
                uv,
                "tool",
                "install",
                "--force",
                "--no-index",
                "--python-platform",
                uv_platform,
                str(wheel),
            ],
            env=environment,
            cwd=root,
        )
    except QualificationError:
        return {"status": "skipped", "reason": "uv host cannot install target wheel"}
    executable = root / "uv-bin/eggpool"
    _run(prefix + [str(executable), "version"], env=environment, cwd=root)
    _run([uv, "tool", "uninstall", "eggpool"], env=environment, cwd=root)
    return {"status": "pass", "version_command": "pass", "uninstall": "pass"}


def _pipx_qualification(wheel: Path, root: Path, prefix: list[str]) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    pipx = shutil.which("pipx")
    if pipx is None:
        return {"status": "skipped", "reason": "pipx unavailable"}
    environment = _base_environment(root)
    environment.update(
        {
            "PIPX_HOME": str(root / "pipx-home"),
            "PIPX_BIN_DIR": str(root / "pipx-bin"),
            "PIPX_MAN_DIR": str(root / "pipx-man"),
        }
    )
    try:
        _run(
            prefix + [pipx, "install", "--force", str(wheel)],
            env=environment,
            cwd=root,
        )
    except QualificationError:
        return {"status": "skipped", "reason": "pipx host cannot install target wheel"}
    executable = root / "pipx-bin/eggpool"
    _run(prefix + [str(executable), "version"], env=environment, cwd=root)
    _run(prefix + [pipx, "uninstall", "eggpool"], env=environment, cwd=root)
    return {"status": "pass", "version_command": "pass", "uninstall": "pass"}


def _runtime_independence(wheel: Path, root: Path, prefix: list[str]) -> dict[str, str]:
    import zipfile

    root.mkdir(parents=True, exist_ok=True)
    executable = root / "native-eggpool"
    with zipfile.ZipFile(wheel) as archive:
        member = next(
            name for name in archive.namelist() if name.endswith("/scripts/eggpool")
        )
        executable.write_bytes(archive.read(member))
    executable.chmod(0o755)
    empty_path = root / "empty-path"
    empty_path.mkdir()
    environment = {"PATH": str(empty_path), "HOME": str(root / "home")}
    _run(prefix + [str(executable), "version"], env=environment, cwd=root)
    return {"status": "pass", "python_path": "unavailable"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--target-class", required=True)
    parser.add_argument("--version", default="0.8.0")
    args = parser.parse_args(argv)
    try:
        wheel = args.wheel.resolve()
        inspect_wheel(
            wheel, expected_version=args.version, target_class=args.target_class
        )
        prefix = _prefix(args.target_class)
        with tempfile.TemporaryDirectory(prefix="eggpool-release-wheel-") as temporary:
            root = Path(temporary)
            (root / "home").mkdir()
            try:
                pip_result = _pip_qualification(wheel, root / "pip", prefix)
            except QualificationError as error:
                raise QualificationError(
                    f"pip qualification failed: {error}"
                ) from error
            try:
                uv_result = _uv_qualification(
                    wheel, root / "uv", prefix, args.target_class
                )
            except QualificationError as error:
                raise QualificationError(f"uv qualification failed: {error}") from error
            try:
                pipx_result = _pipx_qualification(wheel, root / "pipx", prefix)
            except QualificationError as error:
                raise QualificationError(
                    f"pipx qualification failed: {error}"
                ) from error
            try:
                native_result = _runtime_independence(wheel, root / "native", prefix)
            except QualificationError as error:
                raise QualificationError(
                    f"native runtime qualification failed: {error}"
                ) from error
            results = {
                "wheel": wheel.name,
                "target_class": args.target_class,
                "pip": pip_result,
                "uv": uv_result,
                "pipx": pipx_result,
                "native_runtime": native_result,
            }
        print(json.dumps(results, sort_keys=True))
        return 0
    except (QualificationError, WheelInspectionError, OSError, StopIteration) as error:
        print(f"release packaging qualification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
