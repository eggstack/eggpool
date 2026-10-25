"""Guard the planning hierarchy against stale in-flight status declarations.

A closed milestone must not still advertise itself as ready, active, blocked,
or closing. The closure record is the gate; a stale marker on an already-closed
milestone misdirects the next agent into re-implementing finished work, so this
guard makes it fail the ordinary tooling suite.

The forbidden statuses are derived from the templates' own vocabulary lines so
the guard follows the documented status scheme instead of hardcoding a second
copy of it. Both the milestone detailed body in a subsystem roadmap and the
header of an implementation plan are covered.

Two sources decide that a milestone is closed:

- a closure record for the milestone whose disposition is ``closed`` or
  ``conditionally closed`` — the closure record is the gate;
- the milestone's own row in the roadmap's ``## 12. Milestone status`` table,
  which is how a milestone closed *through* a later corrective milestone is
  recorded, because the earlier record keeps the disposition it was written
  with.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]

IMPLEMENTATION = ROOT / "plans/implementation"
CLOSURE = ROOT / "plans/closure"
SUBSYSTEMS = ROOT / "plans/subsystems"

IMPLEMENTATION_README = IMPLEMENTATION / "README.md"
SUBSYSTEM_README = SUBSYSTEMS / "README.md"

# Terminal dispositions for a closure record. "corrective pass required" and
# "blocked" are open: the milestone still has work outstanding.
CLOSED_DISPOSITIONS = ("closed", "conditionally closed")

# Plan vocabulary values that mean the milestone is not finished.
PLAN_TERMINAL_STATUSES = ("implemented", "superseded")
# Roadmap vocabulary values that mean the milestone is not finished.
ROADMAP_TERMINAL_STATUSES = ("closed", "superseded")

MILESTONE_HEADING_RE = re.compile(r"^###\s+Milestone\s+(\d{1,3})\b", re.MULTILINE)
STATUS_WORD_RE = re.compile(r"\bStatus:\s*([A-Za-z]+)")
ROADMAP_REF_RE = re.compile(r"plans/subsystems/([A-Za-z0-9\-]+)-roadmap\.md")
PLAN_PATH_RE = re.compile(r"plans/implementation/([A-Za-z0-9\-]+)/([A-Za-z0-9\-]+\.md)")
CLOSURE_PATH_RE = re.compile(r"plans/closure/([A-Za-z0-9\-]+)/([A-Za-z0-9\-]+\.md)")
MILESTONE_NUMBER_RE = re.compile(r"(\d{1,3})")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _vocabulary(readme: Path, terminal: tuple[str, ...]) -> set[str]:
    """Return the non-terminal status values declared by a status template."""
    declared: set[str] = set()
    for line in _read(readme).splitlines():
        if line.startswith("Status:"):
            for value in line.split(":", 1)[1].split("|"):
                value = value.strip().lower()
                if value and value != "status":
                    declared.add(value)
    assert declared, f"{readme.name} declares no status vocabulary"
    in_flight = declared - set(terminal)
    assert in_flight, f"{readme.name} vocabulary leaves no in-flight status"
    return in_flight


def _declared_statuses(body: str) -> list[str]:
    """Return the status values a document declares about itself.

    Only a line that declares a status counts. Prose that merely quotes a
    status — a closure record explaining that a body once said ``ready``, for
    example — is not a declaration and must not trip the guard.

    The declared value is the first word after ``Status:`` so that a trailing
    period, a parenthetical, or a qualifying suffix does not change how a
    declaration is read. Every status in either vocabulary is a single word.
    """
    declared: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("Status:") or stripped.startswith("Class:")):
            continue
        match = STATUS_WORD_RE.search(stripped)
        if match is not None:
            declared.append(match.group(1).strip().lower())
    return declared


def _milestone_bodies(roadmap: str) -> list[tuple[int, str]]:
    """Return (milestone number, body) for every detailed milestone body."""
    headings = list(MILESTONE_HEADING_RE.finditer(roadmap))
    bodies: list[tuple[int, str]] = []
    for index, heading in enumerate(headings):
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(roadmap)
        bodies.append((int(heading.group(1)), roadmap[start:end]))
    return bodies


def _status_table(roadmap: str) -> dict[int, str]:
    """Return the milestone number to status mapping from section 12.

    The table is the roadmap's own record of its milestones. A milestone
    closed *through* a later corrective milestone is only closed here, because
    the earlier closure record keeps the disposition it was written with.
    """
    marker = "## 12. Milestone status"
    if marker not in roadmap:
        return {}
    section = roadmap.split(marker, maxsplit=1)[1]
    section = section.split("\n## ", maxsplit=1)[0]
    statuses: dict[int, str] = {}
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 3:
            continue
        identifier = cells[1].strip(" `")
        number = MILESTONE_NUMBER_RE.search(identifier)
        status = cells[2].strip()
        if number is None or not status:
            continue
        statuses[int(number.group(1))] = status
    return statuses


def _normalise_disposition(status: str) -> str:
    """Reduce a status cell to its disposition word."""
    value = status.strip().lower()
    for terminator in (" (", " —"):
        value = value.split(terminator, 1)[0]
    return value.strip()


def _closure_records() -> list[tuple[str, int, str, str]]:
    """Return (subsystem, number, disposition, text) for every closure record."""
    records: list[tuple[str, int, str, str]] = []
    for path in sorted(CLOSURE.glob("*/*.md")):
        if path.name == "README.md":
            continue
        text = _read(path)
        dispositions = _declared_statuses(text)
        if not dispositions:
            continue
        number = MILESTONE_NUMBER_RE.search(path.stem)
        if number is None:
            continue
        records.append((path.parent.name, int(number.group(1)), dispositions[0], text))
    return records


def _closed_plan_paths(records: list[tuple[str, int, str, str]]) -> set[str]:
    """Return the implementation plan paths whose milestone is closed."""
    closed: set[str] = set()
    for subsystem, number, disposition, _ in records:
        if disposition not in CLOSED_DISPOSITIONS:
            continue
        for plan in sorted((IMPLEMENTATION / subsystem).glob(f"{number:03d}-*.md")):
            closed.add(plan.relative_to(ROOT).as_posix())
        for plan in sorted((IMPLEMENTATION / subsystem).glob(f"{number}-*.md")):
            closed.add(plan.relative_to(ROOT).as_posix())
    return closed


def _roadmaps_closing_a_milestone(
    records: list[tuple[str, int, str, str]],
) -> dict[str, set[int]]:
    """Return the milestone numbers each roadmap records as closed."""
    closed: dict[str, set[int]] = {}
    for _, number, disposition, text in records:
        if disposition not in CLOSED_DISPOSITIONS:
            continue
        for match in ROADMAP_REF_RE.finditer(text):
            closed.setdefault(match.group(1), set()).add(number)
    return closed


def test_plan_header_of_a_closed_milestone_declares_no_in_flight_status() -> None:
    """A closed milestone's plan must not still read ready, active, or closing."""
    forbidden = _vocabulary(IMPLEMENTATION_README, PLAN_TERMINAL_STATUSES)
    records = _closure_records()
    closed_plans = _closed_plan_paths(records)

    assert closed_plans, "no closed implementation plan was discovered"

    offenders: list[str] = []
    for relative in sorted(closed_plans):
        plan = ROOT / relative
        if not plan.exists():
            continue
        declared = _declared_statuses(_read(plan))
        stale = [value for value in declared if value in forbidden]
        if stale:
            offenders.append(f"{relative} declares {stale} but its milestone is closed")

    assert not offenders, "stale status on closed milestone(s):\n" + "\n".join(
        offenders
    )


def test_plan_header_declares_exactly_one_status() -> None:
    """One plan states one status; two adjacent declarations is a defect.

    A milestone whose plan declared both a closed disposition and an in-flight
    one left a reader unable to tell which was current.
    """
    offenders: list[str] = []
    for plan in sorted(IMPLEMENTATION.glob("*/*.md")):
        if plan.name == "README.md":
            continue
        declared = _declared_statuses(_read(plan))
        if len(declared) > 1:
            offenders.append(f"{plan.relative_to(ROOT)} declares {declared}")

    assert not offenders, "plan(s) declaring more than one status:\n" + "\n".join(
        offenders
    )


def test_roadmap_body_of_a_closed_milestone_declares_no_in_flight_status() -> None:
    """A closed milestone's detailed body must not still read ready or active.

    The registry's milestone table and the closure record already record the
    milestone as closed. A stale body status is the same false signal as a
    stale plan header, in the document an agent reads to learn what a
    milestone is.

    A roadmap body may declare either vocabulary — the repository's own
    provider-transport bodies carry the implementation-plan word "ready" — so
    both templates' non-terminal values are forbidden here.
    """
    forbidden = _vocabulary(SUBSYSTEM_README, ROADMAP_TERMINAL_STATUSES)
    forbidden |= _vocabulary(IMPLEMENTATION_README, PLAN_TERMINAL_STATUSES)
    records = _closure_records()
    closed_by_closure = _roadmaps_closing_a_milestone(records)

    offenders: list[str] = []
    checked = 0
    for roadmap_path in sorted(SUBSYSTEMS.glob("*-roadmap.md")):
        roadmap = _read(roadmap_path)
        stem = roadmap_path.stem.removesuffix("-roadmap")
        closed_numbers = set(closed_by_closure.get(stem, set()))
        for number, status in _status_table(roadmap).items():
            if _normalise_disposition(status) in CLOSED_DISPOSITIONS:
                closed_numbers.add(number)
        if not closed_numbers:
            continue
        for number, body in _milestone_bodies(roadmap):
            if number not in closed_numbers:
                continue
            checked += 1
            declared = _declared_statuses(body)
            stale = [value for value in declared if value in forbidden]
            if stale:
                location = roadmap_path.relative_to(ROOT)
                offenders.append(f"{location} milestone {number} declares {stale}")

    assert checked, "no closed milestone body was checked"
    assert not offenders, "stale status on closed milestone body/bodies:\n" + "\n".join(
        offenders
    )
