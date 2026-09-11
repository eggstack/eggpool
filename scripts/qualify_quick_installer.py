#!/usr/bin/env python3
"""Qualify the quick installer in disposable fake-manager environments.

This deterministic installer harness never touches a real package manager or
user state. Real wheel/index qualification remains owned by the release tests.
"""

from __future__ import annotations

import argparse
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


def _fake_command(path: Path, *, kind: str, version: str, native: bool) -> None:
    _exe(
        path,
        f"""#!{sys.executable}
import pathlib
import sys

if sys.argv[1:] == ["install-provenance", "--shell"]:
    print("kind\\t{kind}")
    print("version\\t{version}")
    print("native\\t{str(native).lower()}")
elif sys.argv[1:] == ["version"]:
    print("{version}")
elif sys.argv[1:2] == ["init-config"]:
    target = pathlib.Path(sys.argv[2])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[server]\\nport = 11300\\n", encoding="utf-8")
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
        "FAKE_MANAGER_FAIL": "0",
        "UV_TOOL_BIN_DIR": str(root / "manager-bin"),
        "PIPX_BIN_DIR": str(root / "manager-bin"),
    }


def _run(
    root: Path,
    *,
    manager: str | None,
    manager_kind: str = "uv-tool",
    args: list[str] | None = None,
    existing: tuple[str, str, bool, str] | None = None,
    source: bool = False,
    expected: int = 0,
    manager_failure: bool = False,
    platform: tuple[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_bin = root / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    environment = _env(root, fake_bin)
    (root / "home").mkdir(parents=True, exist_ok=True)
    platform = platform or ("Linux", "x86_64")
    if platform:
        _exe(
            fake_bin / "uname",
            f"""#!{sys.executable}
import sys

print({platform[0]!r} if sys.argv[1:] == ["-s"] else {platform[1]!r})
""",
        )
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
            _fake_command(path, kind=kind, version=version, native=native)
        environment["PATH"] = os.pathsep.join(
            (str(existing_bin), str(fake_bin), "/usr/bin", "/bin")
        )
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
            f"{result.stdout[-300:]} {result.stderr[-300:]}"
        )
    return result


def _log(root: Path) -> list[str]:
    path = root / "manager.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _case_fresh(kind: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        manager = None if kind == "pip" else ("uv" if kind == "uv-tool" else "pipx")
        _run(root, manager=manager, manager_kind=kind)
        expected = (
            ["tool", "install", "eggpool"]
            if kind == "uv-tool"
            else ["install", "eggpool"]
        )
        assert _log(root) == expected
        assert (root / "config-home/eggpool/config.toml").is_file()
        assert not (root / "home/eggpool").exists()
        return {"case": f"fresh-{kind}", "status": "pass"}


def _case_existing(kind: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        config = root / "config-home/eggpool/config.toml"
        config.parent.mkdir(parents=True)
        config.write_bytes(b"operator-config\n")
        database = root / "data-home/eggpool/usage.sqlite3"
        database.parent.mkdir(parents=True)
        database.write_bytes(b"operator-database\n")
        manager = None if kind == "pip" else ("uv" if kind == "uv-tool" else "pipx")
        _run(
            root,
            manager=manager,
            manager_kind=kind,
            args=["--version", "v0.8.0"],
            existing=(kind, "0.7.4", False, "python-fallback"),
        )
        expected = (
            ["tool", "install", "--force", "eggpool==0.8.0"]
            if kind == "uv-tool"
            else ["install", "--force", "eggpool==0.8.0"]
        )
        if kind == "pip":
            assert not (root / "manager.log").exists()
        else:
            assert _log(root) == expected
        assert config.read_bytes() == b"operator-config\n"
        assert database.read_bytes() == b"operator-database\n"
        return {"case": f"existing-python-{kind}", "status": "pass"}


def _case_standalone(failure: bool) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        old = root / "existing-bin/eggpool"
        old.parent.mkdir(parents=True)
        _fake_command(old, kind="standalone-rust", version="0.7.4", native=True)
        _run(
            root,
            manager="uv",
            args=["--adopt-standalone", "--version", "0.8.0"],
            existing=("standalone-rust", "0.7.4", True, "rust"),
            expected=1 if failure else 0,
            manager_failure=failure,
        )
        backup = old.with_name("eggpool.eggpool-standalone-0.7.4.rollback")
        assert old.is_file() if failure else backup.is_file() and not old.exists()
        return {
            "case": "standalone-rollback" if failure else "standalone-adoption",
            "status": "pass",
        }


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
        result = _run(root, manager="uv", expected=1)
        assert "collision" in result.stderr
        cases.append({"case": "manager-path-collision", "status": "pass"})
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        _exe(fake_bin / "id", "#!/bin/sh\nprintf '0\\n'\n")
        result = _run(root, manager="uv", expected=1)
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
        result = _run(
            root,
            manager="uv",
            platform=("FreeBSD", "x86_64"),
            expected=1,
        )
        assert "unsupported platform" in result.stderr
        assert not (root / "manager.log").exists()
        cases.append({"case": "unsupported-platform-refusal", "status": "pass"})
    return cases


def _source_case() -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="eggpool-") as value:
        root = Path(value)
        result = _run(root, manager="uv", source=True)
        log = _log(root)
        assert str(ROOT / "packaging/pypi") in log and "eggpool" not in log
        assert "Using source checkout" in result.stdout
        return {"case": "source-checkout-local-candidate", "status": "pass"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    results = [
        _case_fresh("uv-tool"),
        _case_fresh("pipx"),
        _case_existing("uv-tool"),
        _case_existing("pipx"),
        _case_existing("pip"),
        _case_standalone(False),
        _case_standalone(True),
        _source_case(),
        *_negative_cases(),
    ]
    print(json.dumps({"cases": results, "status": "pass"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
