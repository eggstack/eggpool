"""Contract tests for the guarded rootful qualification disposable-host runner."""

from __future__ import annotations

import json
import platform
import stat
from pathlib import Path

import pytest

from scripts.qualification_rootful_linux import (
    MANIFEST_VERSION,
    bounded,
    file_fact,
    main,
    write_config,
)


def test_q006_is_explicitly_blocked_off_linux_without_host_mutation(
    tmp_path: Path,
) -> None:
    if platform.system() == "Linux":
        pytest.skip("host-specific preflight is covered by the non-Linux contract")
    report = tmp_path / "q006.json"
    assert (
        main(
            [
                "--binary",
                str(Path(__file__)),
                "--output",
                str(report),
                "--i-understand-disposable-host",
            ]
        )
        == 2
    )
    value = json.loads(report.read_text(encoding="utf-8"))
    assert value["schema"] == MANIFEST_VERSION
    assert value["status"] == "blocked"
    assert "Linux" in value["reason"]


def test_q006_requires_explicit_disposable_host_acknowledgement(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--binary", str(Path(__file__))])


def test_q006_redacts_diagnostics_and_reports_modes(tmp_path: Path) -> None:
    path = tmp_path / "fixture"
    path.write_text("value", encoding="utf-8")
    path.chmod(stat.S_IRUSR)
    fact = file_fact(path)
    assert fact["exists"] is True
    assert fact["mode"] == 0o400
    assert bounded("Bearer q006-provider-key token=secret") == "<redacted>"


def test_q006_fixture_uses_loopback_and_no_real_credentials(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_config(
        config,
        database=tmp_path / "db",
        backup=tmp_path / "backup",
        port=11301,
        provider="http://127.0.0.1:12345",
    )
    text = config.read_text(encoding="utf-8")
    assert "127.0.0.1" in text
    assert "q006-provider-key" in text
    assert "OPENAI_API_KEY" not in text
    assert "https://" not in text


def test_q006_cleanup_mode_refuses_without_ownership_marker(tmp_path: Path) -> None:
    assert (
        main(
            [
                "--cleanup",
                "--output",
                str(tmp_path / "q006.json"),
                "--i-understand-disposable-host",
            ]
        )
        == 2
    )
    value = json.loads((tmp_path / "q006.json").read_text(encoding="utf-8"))
    assert value["status"] == "blocked"
