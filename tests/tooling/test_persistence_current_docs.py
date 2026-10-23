"""Guard current persistence documentation against stale M007–M011 status."""

from pathlib import Path

ROOT = Path(__file__).parents[2]

ROADMAP = ROOT / "plans/subsystems/persistence-roadmap.md"
REGISTRY = ROOT / "plans/registry.md"
MIGRATIONS = ROOT / "rust/assets/db/migrations"

# Registry tables that register executable persistence work. Historical prose
# paragraphs elsewhere in the registry are snapshots and are deliberately not
# parsed by this guard.
REGISTRY_WORK_TABLES = (
    "## Dependency-ready implementation plans",
    "## Active implementation plans",
    "## Blocked work",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _milestone_body(roadmap: str, number: str) -> str:
    """Return one roadmap milestone's detailed body, without its neighbours."""
    marker = f"### Milestone {number} —"
    assert marker in roadmap, f"milestone {number} detailed body is missing"
    remainder = roadmap.split(marker, maxsplit=1)[1]
    for terminator in ("\n### Milestone ", "\n## "):
        remainder = remainder.split(terminator, maxsplit=1)[0]
    return remainder


def _status_lines(body: str) -> list[str]:
    return [
        line.strip() for line in body.splitlines() if line.strip().startswith("Status:")
    ]


def _registry_rows(registry: str, heading: str) -> list[str]:
    assert heading in registry, f"registry table {heading!r} is missing"
    section = registry.split(heading, maxsplit=1)[1]
    section = section.split("\n## ", maxsplit=1)[0]
    return [line for line in section.splitlines() if line.startswith("|")]


def _subsystem_cell(row: str) -> str:
    return row.split("|")[1].strip()


def _highest_migration_version() -> int:
    versions = [int(path.name.split("_", 1)[0]) for path in MIGRATIONS.glob("*.sql")]
    assert versions, "no migration files found"
    return max(versions)


def test_persistence_roadmap_records_m008_as_closed_and_rejected() -> None:
    roadmap = _read(ROADMAP)
    milestone = _milestone_body(roadmap, "008")
    assert "Status: closed — rejected" in milestone
    assert "Status: ready" not in milestone
    assert "Status: active" not in milestone
    assert "plans/closure/persistence/007-pi5-qualification.md" in roadmap
    assert "plans/closure/persistence/008-status.md" in roadmap


def test_m008_detailed_body_declares_exactly_one_status() -> None:
    """M008 previously carried two adjacent status declarations for one verdict."""
    roadmap = _read(ROADMAP)
    statuses = _status_lines(_milestone_body(roadmap, "008"))
    assert len(statuses) == 1, (
        f"M008 body declares {len(statuses)} statuses: {statuses}"
    )


def test_m009_detailed_body_is_closed_not_in_flight() -> None:
    roadmap = _read(ROADMAP)
    milestone = _milestone_body(roadmap, "009")
    statuses = _status_lines(milestone)
    assert len(statuses) == 1, (
        f"M009 body declares {len(statuses)} statuses: {statuses}"
    )
    assert statuses[0].startswith("Status: closed")
    for stale in ("Status: active", "Status: ready", "Status: closing"):
        assert stale not in milestone, f"M009 body still declares {stale!r}"


def test_m010_detailed_body_is_closed_and_rejected() -> None:
    roadmap = _read(ROADMAP)
    milestone = _milestone_body(roadmap, "010")
    statuses = _status_lines(milestone)
    assert len(statuses) == 1, (
        f"M010 body declares {len(statuses)} statuses: {statuses}"
    )
    assert statuses[0].startswith("Status: closed")
    assert "rejected" in statuses[0]
    for stale in ("Status: ready", "Status: active", "Status: closing"):
        assert stale not in milestone, f"M010 body still declares {stale!r}"


def test_m011_detailed_body_is_closed_and_links_its_closure_record() -> None:
    roadmap = _read(ROADMAP)
    milestone = _milestone_body(roadmap, "011")
    statuses = _status_lines(milestone)
    assert len(statuses) == 1, (
        f"M011 body declares {len(statuses)} statuses: {statuses}"
    )
    assert statuses[0].startswith("Status: closed")
    assert "plans/closure/persistence/011-status.md" in roadmap


def test_roadmap_milestone_table_links_m009_and_m010_closures() -> None:
    roadmap = _read(ROADMAP)
    table = roadmap.split("## 12. Milestone status", maxsplit=1)[1]
    assert "plans/closure/persistence/009-status.md" in table
    assert "plans/closure/persistence/010-status.md" in table
    assert "plans/closure/persistence/011-status.md" in table


def test_roadmap_remains_top_level_active() -> None:
    roadmap = _read(ROADMAP)
    header = roadmap.split("## 1.", maxsplit=1)[0]
    assert "Status: active" in header


def test_roadmap_states_the_current_schema_census() -> None:
    """The cross-cutting requirement must not pin a superseded schema version."""
    roadmap = _read(ROADMAP)
    highest = _highest_migration_version()
    assert f"v1–v{highest}" in roadmap
    assert "schema 54 remain authoritative" not in roadmap


def test_production_wal_normal_invariant_remains_present() -> None:
    roadmap = _read(ROADMAP)
    deep_dive = _read(ROOT / "architecture/deep-dive-database.md")
    for document in (roadmap, deep_dive):
        assert "one connection/gate/worker on WAL/NORMAL" in document


def test_registry_registers_no_persistence_executable_work() -> None:
    """After M011 closure no persistence plan may be ready, active, or blocked."""
    registry = _read(REGISTRY)
    for heading in REGISTRY_WORK_TABLES:
        rows = _registry_rows(registry, heading)
        assert rows, f"registry table {heading!r} has no rows to inspect"
        for row in rows:
            assert _subsystem_cell(row) != "Persistence", (
                f"registry still registers persistence work under {heading!r}: {row}"
            )


def test_database_deep_dive_records_final_m007_m008_disposition() -> None:
    deep_dive = _read(ROOT / "architecture/deep-dive-database.md")
    assert (
        "M007's dedicated-checkpointer qualification and M008's PERSIST/EXTRA"
        in deep_dive
    )
    assert "M007 is registered as a qualification-only experiment" not in deep_dive
    assert "one connection/gate/worker on WAL/NORMAL" in deep_dive
    assert "plans/closure/persistence/007-pi5-qualification.md" in deep_dive
    assert "plans/closure/persistence/008-status.md" in deep_dive


def test_background_deep_dive_does_not_present_m007_as_pending() -> None:
    deep_dive = _read(ROOT / "architecture/deep-dive-background.md")
    assert "M007 is the registered qualification-only experiment" not in deep_dive
    assert (
        "M007's dedicated-checkpointer qualification and M008's PERSIST/EXTRA"
        in deep_dive
    )
    assert "plans/closure/persistence/007-pi5-qualification.md" in deep_dive
    assert "plans/closure/persistence/008-status.md" in deep_dive


def test_investigation_record_does_not_pin_a_superseded_schema_version() -> None:
    investigation = _read(
        ROOT / "architecture/persistence-control-projection-investigation.md"
    )
    assert (
        "Production remains the schema-54 monolithic SQLite database"
        not in investigation
    )
    assert "Production remains the monolithic SQLite database" in investigation
