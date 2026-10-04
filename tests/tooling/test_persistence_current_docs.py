"""Guard current persistence documentation against stale M007/M008 status."""

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_persistence_roadmap_records_m008_as_closed_and_rejected() -> None:
    roadmap = (ROOT / "plans/subsystems/persistence-roadmap.md").read_text(
        encoding="utf-8"
    )
    milestone = roadmap.split("### Milestone 008 —", maxsplit=1)[1].split(
        "### Milestone 009 —", maxsplit=1
    )[0]
    assert "Status: closed — rejected" in milestone
    assert "Status: ready" not in milestone
    assert "Status: active" not in milestone
    assert "plans/closure/persistence/007-pi5-qualification.md" in roadmap
    assert "plans/closure/persistence/008-status.md" in roadmap


def test_database_deep_dive_records_final_m007_m008_disposition() -> None:
    deep_dive = (ROOT / "architecture/deep-dive-database.md").read_text(
        encoding="utf-8"
    )
    assert (
        "M007's dedicated-checkpointer qualification and M008's PERSIST/EXTRA"
        in deep_dive
    )
    assert "M007 is registered as a qualification-only experiment" not in deep_dive
    assert "one connection/gate/worker on WAL/NORMAL" in deep_dive
    assert "plans/closure/persistence/007-pi5-qualification.md" in deep_dive
    assert "plans/closure/persistence/008-status.md" in deep_dive


def test_background_deep_dive_does_not_present_m007_as_pending() -> None:
    deep_dive = (ROOT / "architecture/deep-dive-background.md").read_text(
        encoding="utf-8"
    )
    assert "M007 is the registered qualification-only experiment" not in deep_dive
    assert (
        "M007's dedicated-checkpointer qualification and M008's PERSIST/EXTRA"
        in deep_dive
    )
    assert "plans/closure/persistence/007-pi5-qualification.md" in deep_dive
    assert "plans/closure/persistence/008-status.md" in deep_dive
