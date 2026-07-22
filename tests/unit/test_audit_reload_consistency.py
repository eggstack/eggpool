"""Unit tests for the reload consistency audit script.

The script (``scripts/audit_reload_consistency.py``) is exercised
through its programmatic checks so we don't need to spawn a
subprocess.  Tests cover:

* provider rows vs active config (presence, protocols, base_url drift)
* account rows vs active config (presence, provider_id, enabled, weight drift)
* task specs vs active registry (introspection unavailable case is silent)
* routing-trace writer mode alignment
* snapshot template emission
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "audit_reload_consistency.py"
)


@pytest.fixture()
def audit_module():
    """Import the audit script as a module.

    Skips if the script is unreachable (e.g., packaged build without
    ``scripts/``).
    """
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    try:
        import audit_reload_consistency as mod  # type: ignore[import-not-found]

        return mod
    finally:
        sys.path.pop(0)


def _make_snapshot(**overrides: object) -> dict[str, object]:
    base = {
        "generation_id": 0,
        "config_digest": "abc",
        "providers": {
            "test-provider-a": {
                "base_url": "https://a.example.com/v1",
                "protocols": ["openai"],
            }
        },
        "accounts": {
            "acct-a1": {"provider_id": "test-provider-a", "enabled": True},
            "acct-a2": {"provider_id": "test-provider-a", "enabled": True},
        },
        "task_specs": ["checkpoint"],
        "routing_trace_mode": "sampled",
    }
    base.update(overrides)
    return base


def _make_db(tmpdir: Path, *, providers=None, accounts=None) -> str:
    """Build a SQLite database with providers/accounts tables."""
    db_path = tmpdir / "usage.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE providers ("
            "  provider_id TEXT PRIMARY KEY, "
            "  base_url TEXT, "
            "  protocols TEXT, "
            "  enabled INTEGER DEFAULT 1"
            ")"
        )
        conn.execute(
            "CREATE TABLE accounts ("
            "  name TEXT PRIMARY KEY, "
            "  provider_id TEXT, "
            "  enabled INTEGER DEFAULT 1, "
            "  weight INTEGER DEFAULT 1"
            ")"
        )
        if providers:
            for pid, base_url, protocols in providers:
                conn.execute(
                    "INSERT INTO providers (provider_id, base_url, protocols) "
                    "VALUES (?, ?, ?)",
                    (pid, base_url, json.dumps(protocols)),
                )
        if accounts:
            for name, pid, enabled, weight in accounts:
                conn.execute(
                    "INSERT INTO accounts (name, provider_id, enabled, weight) "
                    "VALUES (?, ?, ?, ?)",
                    (name, pid, int(enabled), int(weight)),
                )
        conn.commit()
    return str(db_path)


def test_parse_snapshot_missing_fields_raises(audit_module, tmp_path: Path) -> None:
    """Missing required fields raise a ValueError before any DB hit."""
    snapshot = tmp_path / "snap.json"
    snapshot.write_text(json.dumps({"generation_id": 0}))
    with pytest.raises(ValueError, match="missing required fields"):
        audit_module.parse_snapshot(str(snapshot))


def test_audit_provider_rows_pass_on_match(audit_module, tmp_path: Path) -> None:
    """Matching provider rows and snapshot yield no violations."""
    db_path = _make_db(
        tmp_path,
        providers=[("test-provider-a", "https://a.example.com/v1", ["openai"])],
    )
    snapshot = _make_snapshot()
    violations = audit_module._audit_provider_rows(
        db_path,
        snapshot,  # type: ignore[arg-type]
    )
    assert violations == []


def test_audit_provider_rows_detects_missing_provider(
    audit_module, tmp_path: Path
) -> None:
    """A configured provider not present in the DB is flagged."""
    db_path = _make_db(tmp_path, providers=[])
    snapshot = _make_snapshot()
    violations = audit_module._audit_provider_rows(
        db_path,
        snapshot,  # type: ignore[arg-type]
    )
    assert any(
        v.check_name == "provider_rows_vs_active_config"
        and "missing from" in v.description
        for v in violations
    )


def test_audit_provider_rows_detects_stale_provider(
    audit_module, tmp_path: Path
) -> None:
    """A DB provider not in snapshot is flagged as drift."""
    db_path = _make_db(
        tmp_path,
        providers=[("orphan", "https://x.example.com/v1", ["openai"])],
    )
    snapshot = _make_snapshot()
    violations = audit_module._audit_provider_rows(
        db_path,
        snapshot,  # type: ignore[arg-type]
    )
    assert any(
        v.check_name == "provider_rows_vs_active_config"
        and "still enabled" in v.description
        for v in violations
    )


def test_audit_provider_rows_detects_disabled(audit_module, tmp_path: Path) -> None:
    """A DB-row marked disabled while snapshot says enabled is flagged."""
    db_path = _make_db(
        tmp_path,
        providers=[("test-provider-a", "https://a.example.com/v1", ["openai"])],
    )
    # Manually flip the row to disabled.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE providers SET enabled = 0 WHERE provider_id = ?",
            ("test-provider-a",),
        )
        conn.commit()
    snapshot = _make_snapshot()
    violations = audit_module._audit_provider_rows(
        db_path,
        snapshot,  # type: ignore[arg-type]
    )
    assert any(
        v.check_name == "provider_rows_vs_active_config" and "disabled" in v.description
        for v in violations
    )


def test_audit_provider_rows_detects_protocol_drift(
    audit_module, tmp_path: Path
) -> None:
    """Protocol list mismatch is reported as a per-provider drift."""
    db_path = _make_db(
        tmp_path,
        providers=[("test-provider-a", "https://a.example.com/v1", ["openai"])],
    )
    # Snapshot says anthropic too.
    snapshot = _make_snapshot(
        providers={
            "test-provider-a": {
                "base_url": "https://a.example.com/v1",
                "protocols": ["openai", "anthropic"],
            }
        }
    )
    violations = audit_module._audit_provider_rows(
        db_path,
        snapshot,  # type: ignore[arg-type]
    )
    assert any("protocols drift" in v.description for v in violations)


def test_audit_account_rows_detects_provider_drift(
    audit_module, tmp_path: Path
) -> None:
    """An account row with a different provider_id than snapshot is flagged."""
    db_path = _make_db(
        tmp_path,
        accounts=[
            ("acct-a1", "wrong-provider", True, 1),
            ("acct-a2", "test-provider-a", True, 1),
        ],
    )
    snapshot = _make_snapshot()
    violations = audit_module._audit_account_rows(
        db_path,
        snapshot,  # type: ignore[arg-type]
    )
    assert any("provider_id drift" in v.description for v in violations)


def test_audit_account_rows_detects_disabled(audit_module, tmp_path: Path) -> None:
    """An account row disabled while snapshot says enabled is flagged."""
    db_path = _make_db(
        tmp_path,
        accounts=[
            ("acct-a1", "test-provider-a", False, 1),
            ("acct-a2", "test-provider-a", True, 1),
        ],
    )
    snapshot = _make_snapshot()
    violations = audit_module._audit_account_rows(
        db_path,
        snapshot,  # type: ignore[arg-type]
    )
    assert any(
        v.check_name == "account_rows_vs_active_config" and "disabled" in v.description
        for v in violations
    )


def test_audit_task_specs_noop_when_unavailable(
    audit_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``EGGPOOL_AUDIT_TASK_SPECS_ACTIVE`` env var ⇒ no violations."""
    monkeypatch.delenv("EGGPOOL_AUDIT_TASK_SPECS_ACTIVE", raising=False)
    violations = audit_module._audit_task_specs(_make_snapshot())  # type: ignore[arg-type]
    assert violations == []


def test_audit_task_specs_detects_missing(
    audit_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing task spec name is reported when env var is set."""
    monkeypatch.setenv("EGGPOOL_AUDIT_TASK_SPECS_ACTIVE", "checkpoint,metrics_flush")
    snapshot = _make_snapshot(
        task_specs=["checkpoint", "metrics_flush", "update_checker"]
    )
    violations = audit_module._audit_task_specs(snapshot)  # type: ignore[arg-type]
    assert any(v.check_name == "task_specs_vs_active_registry" for v in violations)


def test_audit_task_specs_passes_when_all_present(
    audit_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All configured task specs present in env var passes."""
    monkeypatch.setenv("EGGPOOL_AUDIT_TASK_SPECS_ACTIVE", "checkpoint,metrics_flush")
    snapshot = _make_snapshot(task_specs=["checkpoint", "metrics_flush"])
    violations = audit_module._audit_task_specs(snapshot)  # type: ignore[arg-type]
    assert violations == []


def test_audit_routing_trace_writer_match(audit_module) -> None:
    """Writer mode matches snapshot ⇒ no violation."""
    snapshot = _make_snapshot(routing_trace_mode="full")
    violations = audit_module._audit_routing_trace_writer(snapshot, "full")
    assert violations == []


def test_audit_routing_trace_writer_drift(audit_module) -> None:
    """Writer mode differs from snapshot ⇒ one violation."""
    snapshot = _make_snapshot(routing_trace_mode="full")
    violations = audit_module._audit_routing_trace_writer(snapshot, "off")
    assert len(violations) == 1
    assert "writer.mode=" in violations[0].description


def test_emit_snapshot_template(audit_module) -> None:
    """``--emit-snapshot-template`` produces a JSON template."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = audit_module.main(["--emit-snapshot-template"])
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert "providers" in payload and "accounts" in payload
    assert "task_specs" in payload and "routing_trace_mode" in payload


def test_cli_missing_args_returns_error_code(audit_module) -> None:
    """Without ``--db-path`` and ``--active-snapshot`` returns exit 2."""
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as ei:
        audit_module.main([])
    assert ei.value.code == 2


def test_run_audit_full_pipeline_clean(audit_module, tmp_path: Path) -> None:
    """End-to-end audit with matching rows returns exit 0."""
    db_path = _make_db(
        tmp_path,
        providers=[("test-provider-a", "https://a.example.com/v1", ["openai"])],
        accounts=[
            ("acct-a1", "test-provider-a", True, 1),
            ("acct-a2", "test-provider-a", True, 1),
        ],
    )
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(_make_snapshot()))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = audit_module.run_audit(db_path, str(snap_path))
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["passed"] is True


def test_run_audit_full_pipeline_dirty(audit_module, tmp_path: Path) -> None:
    """End-to-end audit with stale provider row returns exit 1."""
    db_path = _make_db(
        tmp_path,
        providers=[
            ("test-provider-a", "https://a.example.com/v1", ["openai"]),
            ("orphan", "https://o.example.com/v1", ["openai"]),
        ],
        accounts=[("acct-a1", "test-provider-a", True, 1)],
    )
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(_make_snapshot()))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = audit_module.run_audit(db_path, str(snap_path))
    assert rc == 1
    payload = json.loads(buf.getvalue())
    assert payload["passed"] is False
    assert any(
        v["check_name"] == "provider_rows_vs_active_config"
        for v in payload["violations"]
    )


def test_missing_db_file_emits_violation(audit_module, tmp_path: Path) -> None:
    """No DB file present at ``--db-path`` is reported as a violation."""
    db_path = str(tmp_path / "does-not-exist.sqlite3")
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(_make_snapshot()))
    violations = audit_module._audit_provider_rows(
        db_path,
        _make_snapshot(),  # type: ignore[arg-type]
    )
    assert any("not found" in v.description for v in violations)


def test_audit_provider_rows_handles_bad_protocols_json(
    audit_module, tmp_path: Path
) -> None:
    """A protocols column containing invalid JSON does not crash audit.

    Audit surfaces the drift as a protocols-drift violation when the
    configured protocols differ from the parsed DB protocols (which
    degrades to ``()`` on parse failure).  The test asserts that the
    audit completes without raising and reports at least one drift.
    """
    db_path = _make_db(
        tmp_path,
        providers=[("test-provider-a", "https://a.example.com/v1", ["openai"])],
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE providers SET protocols = ? WHERE provider_id = ?",
            ("not-json", "test-provider-a"),
        )
        conn.commit()
    violations = audit_module._audit_provider_rows(
        db_path,
        _make_snapshot(),  # type: ignore[arg-type]
    )
    assert any("protocols drift" in v.description for v in violations)


def test_emit_snapshot_via_argv_runner(audit_module) -> None:
    """``--emit-snapshot-template`` via CLI returns 0 and emits JSON."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = audit_module.main(
            [
                "--emit-snapshot-template",
            ]
        )
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert isinstance(payload, dict)


def test_run_audit_account_weight_drift(audit_module, tmp_path: Path) -> None:
    """Account weight mismatch produces a drift violation."""
    db_path = _make_db(
        tmp_path,
        accounts=[
            ("acct-a1", "test-provider-a", True, 5),
            ("acct-a2", "test-provider-a", True, 1),
        ],
    )
    snap_path = tmp_path / "snap.json"
    snap = _make_snapshot(
        accounts={
            "acct-a1": {
                "provider_id": "test-provider-a",
                "enabled": True,
                "weight": 1,
            },
            "acct-a2": {
                "provider_id": "test-provider-a",
                "enabled": True,
            },
        }
    )
    snap_path.write_text(json.dumps(snap))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = audit_module.run_audit(db_path, str(snap_path))
    assert rc == 1
    payload = json.loads(buf.getvalue())
    assert any("weight drift" in v["description"] for v in payload["violations"])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip audit env vars from test environment."""
    for key in ("EGGPOOL_AUDIT_DB", "EGGPOOL_AUDIT_SNAPSHOT"):
        monkeypatch.delenv(key, raising=False)
