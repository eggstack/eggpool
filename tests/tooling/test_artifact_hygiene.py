"""Repository-state guards for tracked qualification artifacts.

The qualification artifacts under ``artifacts/qualification/`` are retained
closure evidence: closure records cite them and committed tooling tests read
them. The ignore rule and the tracked state must therefore agree, and no path
may be simultaneously ignored and tracked.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
QUALIFICATION_ROOT = "artifacts/qualification"
GITIGNORE = ROOT / ".gitignore"
# The ignore rule re-includes the retained evidence trees and the retained
# loose evidence files. The patterns are read from `.gitignore` so the guard
# follows the rule instead of keeping a second, hardcoded copy of it.
EVIDENCE_DIRECTORY_PATTERN = re.compile(r"^!/artifacts/qualification/([^/]+)/$")
EVIDENCE_FILE_PATTERN = re.compile(r"^!/artifacts/qualification/([^/]+)$")
# The retained corpus the qualification tooling writes into by default. It is
# deliberately retained evidence (docs/releasing.md, "Qualification artifact
# retention"), so it stays un-ignored and its files stay tracked.
RETAINED_CORPUS = (
    "artifacts/qualification/on-prem-concurrency-performance/ordinary-server"
)
# Qualification run output the tooling writes by default. It is generated
# output, not evidence, so it stays untracked.
GENERATED_OUTPUTS = (
    "artifacts/qualification/005-run.json",
    "artifacts/qualification/005-run-linux-x86_64.json",
    "artifacts/qualification/006-run.json",
    "artifacts/qualification/007-run.json",
    "artifacts/qualification/008-run.json",
    "artifacts/qualification/009-run.json",
    "artifacts/qualification/service-transition.json",
    "artifacts/qualification/release-rehearsal.md",
    "artifacts/qualification/m001-ssd-c4-u5-user-global-1.json",
)
# References that already failed to resolve before this milestone: historical
# records that name a flat layout or a range that the repository never
# committed. The set is pinned so that a new dangling reference fails.
HISTORICAL_DANGLING_REFERENCES = frozenset(
    {
        "artifacts/qualification/237-sbc-finite-tail-run-1..3.json",
        "artifacts/qualification/m007-candidate-1.json",
        "artifacts/qualification/m007-candidate-2.json",
        "artifacts/qualification/m007-candidate-300.json",
        "artifacts/qualification/m007-control-1.json",
        "artifacts/qualification/m007-control-2.json",
        "artifacts/qualification/m007-control-3.json",
        "artifacts/qualification/on-prem-concurrency-performance/"
        "microsd-c4-u5-user-global-run-1.json",
    }
)
REFERENCE = re.compile(r"artifacts/qualification/[A-Za-z0-9._/*-]+")


def _git(
    *args: str, stdin: bytes | None = None, allowed: tuple[int, ...] = (0,)
) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        input=stdin,
    )
    if result.returncode not in allowed:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.returncode} {result.stderr!r}"
        )
    return result.stdout


def tracked_paths() -> list[str]:
    raw = _git("ls-files", "-z")
    return [entry.decode("utf-8") for entry in raw.split(b"\0") if entry]


def ignored_paths(paths: list[str]) -> list[str]:
    if not paths:
        return []
    raw = _git(
        "check-ignore",
        "--no-index",
        "-z",
        "--stdin",
        stdin="\0".join(paths).encode("utf-8") + b"\0",
        allowed=(0, 1),
    )
    return [entry.decode("utf-8") for entry in raw.split(b"\0") if entry]


def ignore_patterns() -> tuple[str, ...]:
    """Return the `.gitignore` patterns that re-include retained evidence."""
    return tuple(
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(f"!/{QUALIFICATION_ROOT}/")
    )


def evidence_directories() -> tuple[str, ...]:
    """Return the retained evidence trees the ignore rule re-includes."""
    names = sorted(
        match.group(1)
        for pattern in ignore_patterns()
        if (match := EVIDENCE_DIRECTORY_PATTERN.match(pattern)) is not None
    )
    return tuple(f"{QUALIFICATION_ROOT}/{name}" for name in names)


def evidence_files() -> tuple[str, ...]:
    """Return the retained evidence files the ignore rule re-includes."""
    names = sorted(
        match.group(1)
        for pattern in ignore_patterns()
        if (match := EVIDENCE_FILE_PATTERN.match(pattern)) is not None
    )
    return tuple(f"{QUALIFICATION_ROOT}/{name}" for name in names)


def generated_output_shapes() -> tuple[str, ...]:
    """Return the file-name shapes of the generated qualification output."""
    return tuple(sorted({Path(output).name for output in GENERATED_OUTPUTS}))


def closure_references() -> set[str]:
    references: set[str] = set()
    for record in sorted((ROOT / "plans/closure").rglob("*.md")):
        text = record.read_text(encoding="utf-8")
        for match in REFERENCE.findall(text):
            references.add(match.rstrip(".,;:"))
    return references


def resolves(reference: str) -> bool:
    if any(character in reference for character in "*"):
        head, _, tail = reference.partition("*")
        parent = head.rsplit("/", 1)[0]
        prefix = head.rsplit("/", 1)[-1]
        pattern = f"{prefix}*{tail}"
        return any(ROOT.glob(f"{parent}/{pattern}"))
    return (ROOT / reference).exists()


def test_no_tracked_path_is_simultaneously_ignored_and_tracked() -> None:
    tracked = tracked_paths()
    assert tracked
    assert ignored_paths(tracked) == []


def test_retained_qualification_evidence_is_tracked() -> None:
    tracked = set(tracked_paths())
    directories = evidence_directories()
    assert directories
    for directory in directories:
        assert any(path.startswith(f"{directory}/") for path in tracked), directory
    files = set(evidence_files())
    for evidence in files:
        assert evidence in tracked, evidence
    # The allow-list and the tracked state must agree in both directions:
    # every tracked artifact is covered by a re-included evidence tree or
    # file, so narrowing the ignore rule fails here.
    uncovered = sorted(
        path
        for path in tracked
        if path.startswith(f"{QUALIFICATION_ROOT}/")
        and path not in files
        and not any(path.startswith(f"{directory}/") for directory in directories)
    )
    assert uncovered == []


def test_generated_qualification_output_stays_untracked() -> None:
    for output in GENERATED_OUTPUTS:
        assert output in ignored_paths([output]), output


def test_generated_output_inside_evidence_directories_stays_untracked() -> None:
    """Generated output inside a retained evidence tree must stay untracked.

    The ignore rule re-includes each retained evidence tree wholesale, so a
    generated-output shape written inside one of those trees is not ignored.
    That is the deliberate state and the reason the flat rule cannot simply
    be widened: the tree is retained evidence, so a fresh file inside it
    stays untracked until an operator retains it on purpose, instead of the
    ignore rule swallowing it silently. The guard therefore asserts that no
    generated-output shape inside a retained evidence tree is tracked.

    The flat generated output at the top of ``artifacts/qualification/``
    stays ignored instead; see
    ``test_generated_qualification_output_stays_untracked``.
    """
    tracked = set(tracked_paths())
    directories = evidence_directories()
    assert directories
    for directory in directories:
        for shape in generated_output_shapes():
            assert f"{directory}/{shape}" not in tracked, f"{directory}/{shape}"


def test_the_retained_corpus_is_deliberately_unignored_and_tracked() -> None:
    """The retained corpus is the documented exception to the ignore rule.

    ``scripts/qualification_ordinary_server.py`` writes its corpus into the
    re-included evidence tree by default, so that output is deliberately
    retained evidence (docs/releasing.md, "Qualification artifact
    retention"). It must stay un-ignored, and the corpus must stay tracked.
    """
    assert any(
        RETAINED_CORPUS.startswith(f"{directory}/")
        for directory in evidence_directories()
    ), RETAINED_CORPUS
    tracked = sorted(
        path for path in tracked_paths() if path.startswith(f"{RETAINED_CORPUS}/")
    )
    assert tracked
    assert tracked[0] not in ignored_paths([tracked[0]])


def test_closure_records_resolve_the_retained_qualification_artifacts() -> None:
    dangling = {
        reference for reference in closure_references() if not resolves(reference)
    }
    assert dangling == set(HISTORICAL_DANGLING_REFERENCES)


def test_retained_evidence_is_referenced_by_a_closure_record() -> None:
    references = closure_references()
    referenced = {
        reference
        for reference in references
        if reference.startswith(QUALIFICATION_ROOT)
    }
    assert len(referenced) >= 30
