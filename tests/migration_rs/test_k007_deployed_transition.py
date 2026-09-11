"""Contract tests for the guarded K007 deployed-transition runner."""

from __future__ import annotations

import json
import platform
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from scripts.qualification_deployed_transition import (
    bounded,
    main,
    write_unit,
)


def test_k007_runner_requires_explicit_disposable_host_ack(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "--python-wheel",
                str(tmp_path / "python.whl"),
                "--rust-wheel",
                str(tmp_path / "rust.whl"),
                "--output",
                str(tmp_path / "run.json"),
            ]
        )


def test_k007_runner_blocks_without_linux_mutation(tmp_path: Path) -> None:
    if platform.system() == "Linux":
        pytest.skip("host-specific preflight is covered by disposable Linux execution")
    output = tmp_path / "run.json"
    assert (
        main(
            [
                "--python-wheel",
                str(tmp_path / "python.whl"),
                "--rust-wheel",
                str(tmp_path / "rust.whl"),
                "--output",
                str(output),
                "--i-understand-disposable-host",
            ]
        )
        == 2
    )
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "blocked"


def test_k007_unit_uses_one_stable_executable_and_redacts_output(
    tmp_path: Path,
) -> None:
    unit = tmp_path / "eggpool.service"
    digest = write_unit(
        unit,
        tmp_path / "venv/bin/eggpool",
        tmp_path / "config.toml",
        "operator",
        tmp_path / "home",
    )
    text = unit.read_text(encoding="utf-8")
    assert digest
    assert text.count("ExecStart=") == 1
    assert str(tmp_path / "venv/bin/eggpool") in text
    assert "api_key" not in text
    assert bounded("Bearer q007-provider-key token=secret") == "<redacted>"
