#!/usr/bin/env python3
"""Audit test files for non-strict xfails and unconditional skips.

Exit 0 if clean, exit 1 if violations found.
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Allowlist: (relative_path, line_number_or_None)
# line_number_or_None == None means any occurrence in that file is exempt.
# ---------------------------------------------------------------------------

ALLOWLIST: list[tuple[str, int | None, str]] = [
    # --- optional-dependency skipif (always allowed) ---
    ("tests/unit/test_jsonx.py", 27, "orjson skipif (parametrize marks=)"),
    ("tests/unit/test_jsonx.py", 185, "orjson skipif (decorator)"),
    ("tests/unit/test_jsonx.py", 194, "orjson skipif (decorator)"),
    ("tests/perf/test_rehash_d3_performance.py", 166, "psutil skipif (decorator)"),
    # --- live-test gate skipif (always allowed) ---
    ("tests/live/test_model_info_openrouter_live.py", 31, "live gate skipif"),
]

# Build a lookup dict for O(1) checks: {(rel_path, line): rationale, ...}
_ALLOWLIST: dict[tuple[str, int], str] = {}
_ALLOWLIST_ANY: dict[str, str] = {}  # rel_path -> rationale (any line)

for _path, _line, _rationale in ALLOWLIST:
    if _line is None:
        _ALLOWLIST_ANY[_path] = _rationale
    else:
        _ALLOWLIST[(Path(_path).as_posix(), _line)] = _rationale


# ---------------------------------------------------------------------------
# AST visitor
# ---------------------------------------------------------------------------


class MarkFinder(ast.NodeVisitor):
    """Walk a module AST and collect pytest mark usages."""

    def __init__(self, source: str, rel_path: str) -> None:
        self.source_lines = source.splitlines()
        self.rel_path = rel_path
        self.violations: list[Violation] = []
        # Track decorator call nodes already checked to avoid double-counting
        # when generic_visit descends into decorator_list.
        self._decorator_calls: set[tuple[int, int]] = set()

    # -- helpers --

    def _get_source_segment(self, node: ast.expr) -> str:
        """Return the raw source text for *node* (best-effort)."""
        try:
            src = "\n".join(self.source_lines)
            return ast.get_source_segment(src, node, padded=False) or ""
        except Exception:
            return ""

    def _line_of(self, node: ast.expr | ast.Assign) -> int:
        return getattr(node, "lineno", 0)

    def _is_xfail(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "xfail"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "pytest"
        )

    def _is_skip(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "skip"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "pytest"
        )

    def _is_skipif(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "skipif"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "pytest"
        )

    def _is_allowed(self, line: int) -> tuple[bool, str]:
        key = (self.rel_path, line)
        if key in _ALLOWLIST:
            return True, _ALLOWLIST[key]
        if self.rel_path in _ALLOWLIST_ANY:
            return True, _ALLOWLIST_ANY[self.rel_path]
        return False, ""

    # -- check decorator expressions --

    def _check_decorator(self, dec: ast.expr) -> None:
        line = self._line_of(dec)
        # Record decorator call nodes so _check_call skips them
        if isinstance(dec, ast.Call):
            self._decorator_calls.add((line, dec.end_col_offset or 0))

        # @pytest.mark.xfail(...)
        if isinstance(dec, ast.Call) and self._is_xfail(dec.func):
            allowed, _rationale = self._is_allowed(line)
            if allowed:
                return
            # Determine strictness
            strict_val = self._extract_strict(dec)
            if strict_val is False:
                self.violations.append(
                    Violation(
                        path=self.rel_path,
                        line=line,
                        annotation="pytest.mark.xfail(strict=False)",
                        reason=(
                            "Non-strict xfail in test area. "
                            "Add rationale and expiry to allowlist, "
                            "or make strict=True if the test should fail hard."
                        ),
                    )
                )
            elif strict_val is None:
                # No strict kwarg — defaults to strict=False
                self.violations.append(
                    Violation(
                        path=self.rel_path,
                        line=line,
                        annotation="pytest.mark.xfail (no strict=)",
                        reason=(
                            "Non-strict xfail (default) in test area. "
                            "Add rationale and expiry to allowlist, "
                            "or make strict=True."
                        ),
                    )
                )

        # @pytest.mark.skip(...)
        elif isinstance(dec, ast.Call) and self._is_skip(dec.func):
            allowed, _rationale = self._is_allowed(line)
            if allowed:
                return
            self.violations.append(
                Violation(
                    path=self.rel_path,
                    line=line,
                    annotation="pytest.mark.skip",
                    reason=(
                        "Unconditional skip. Add rationale and expiry "
                        "to allowlist, or remove the skip."
                    ),
                )
            )

        # @pytest.mark.skipif — always allowed (checked in allowlist too)
        elif isinstance(dec, ast.Call) and self._is_skipif(dec.func):
            return  # skipif is always acceptable

        # @pytest.mark.xfail (bare, no call)
        elif self._is_xfail(dec):
            allowed, _rationale = self._is_allowed(line)
            if not allowed:
                self.violations.append(
                    Violation(
                        path=self.rel_path,
                        line=line,
                        annotation="pytest.mark.xfail (bare)",
                        reason=(
                            "Non-strict xfail (bare, no strict=) in test area. "
                            "Add rationale and expiry to allowlist, "
                            "or use strict=True with a call."
                        ),
                    )
                )

    # -- check function/class body for calls --

    def _check_call(self, node: ast.Call) -> None:
        """Check calls like ``pytest.mark.xfail(...)`` used as marks= in parametrize."""
        line = self._line_of(node)
        # Skip if this call was already checked as a decorator
        if (line, node.end_col_offset or 0) in self._decorator_calls:
            return

        # Only care about marks= usage inside pytest.param / etc
        if self._is_xfail(node.func):
            allowed, _rationale = self._is_allowed(line)
            if allowed:
                return
            strict_val = self._extract_strict(node)
            if strict_val is False or strict_val is None:
                self.violations.append(
                    Violation(
                        path=self.rel_path,
                        line=line,
                        annotation="pytest.mark.xfail (inline call)",
                        reason="Non-strict xfail in test area.",
                    )
                )
        elif self._is_skip(node.func):
            allowed, _rationale = self._is_allowed(line)
            if not allowed:
                self.violations.append(
                    Violation(
                        path=self.rel_path,
                        line=line,
                        annotation="pytest.mark.skip (inline call)",
                        reason="Unconditional skip.",
                    )
                )

    def _extract_strict(self, call: ast.Call) -> bool | None:
        """Return the value of the ``strict`` kwarg, or None if absent."""
        for kw in call.keywords:
            if kw.arg == "strict":
                if isinstance(kw.value, ast.Constant):
                    return bool(kw.value.value)
                return None  # non-literal
        return None  # not specified

    # -- public interface --

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:  # noqa: N802
        for dec in node.decorator_list:
            self._check_decorator(dec)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        for dec in node.decorator_list:
            self._check_decorator(dec)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._check_call(node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Detect ``pytestmark = pytest.mark.xfail(...)`` assignments."""
        # Check if this is a pytestmark assignment
        if not (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "pytestmark"
        ):
            self.generic_visit(node)
            return

        line = self._line_of(node)
        val = node.value

        # pytestmark = pytest.mark.xfail(...)
        if isinstance(val, ast.Call) and self._is_xfail(val.func):
            allowed, _rationale = self._is_allowed(line)
            if not allowed:
                self.violations.append(
                    Violation(
                        path=self.rel_path,
                        line=line,
                        annotation="pytestmark = pytest.mark.xfail(...)",
                        reason="Module-level non-strict xfail.",
                    )
                )
        elif isinstance(val, ast.Call) and self._is_skip(val.func):
            allowed, _rationale = self._is_allowed(line)
            if not allowed:
                self.violations.append(
                    Violation(
                        path=self.rel_path,
                        line=line,
                        annotation="pytestmark = pytest.mark.skip(...)",
                        reason="Module-level unconditional skip.",
                    )
                )
        # pytestmark = [pytest.mark.xfail(...), ...]
        elif isinstance(val, ast.List):
            for elt in val.elts:
                if isinstance(elt, ast.Call) and self._is_xfail(elt.func):
                    eline = self._line_of(elt)
                    allowed, _ = self._is_allowed(eline)
                    if not allowed:
                        self.violations.append(
                            Violation(
                                path=self.rel_path,
                                line=eline,
                                annotation="pytestmark = [..., pytest.mark.xfail(...)]",
                                reason="Module-level non-strict xfail in list.",
                            )
                        )
                elif isinstance(elt, ast.Call) and self._is_skip(elt.func):
                    eline = self._line_of(elt)
                    allowed, _ = self._is_allowed(eline)
                    if not allowed:
                        self.violations.append(
                            Violation(
                                path=self.rel_path,
                                line=eline,
                                annotation="pytestmark = [..., pytest.mark.skip(...)]",
                                reason="Module-level unconditional skip in list.",
                            )
                        )

        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Violation dataclass
# ---------------------------------------------------------------------------


class Violation:
    __slots__ = ("path", "line", "annotation", "reason")

    def __init__(self, path: str, line: int, annotation: str, reason: str) -> None:
        self.path = path
        self.line = line
        self.annotation = annotation
        self.reason = reason

    def __str__(self) -> str:
        return f"  {self.path}:{self.line}: {self.annotation}\n    -> {self.reason}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

IGNORE_DIRS = {"__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"}


def audit() -> list[Violation]:
    tests_root = Path("tests")
    violations: list[Violation] = []

    for py_file in sorted(tests_root.rglob("*.py")):
        # Skip __pycache__ and hidden dirs
        parts = py_file.parts
        if any(p.startswith(".") or p in IGNORE_DIRS for p in parts):
            continue

        rel = py_file.as_posix()

        try:
            source = py_file.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"WARNING: could not read {rel}: {exc}", file=sys.stderr)
            continue

        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            print(f"WARNING: could not parse {rel}: {exc}", file=sys.stderr)
            continue

        finder = MarkFinder(source, rel)
        finder.visit(tree)
        violations.extend(finder.violations)

    return violations


def main() -> int:
    violations = audit()

    if violations:
        print(f"VIOLATIONS FOUND ({len(violations)}):\n")
        for v in violations:
            print(str(v))
            print()
        print(
            textwrap.dedent(
                """\
            Summary: {} violation(s) found.
            To fix: add the (path, line, rationale, expiry) to the
            ALLOWLIST in scripts/audit_xfail_skips.py, or remove the
            xfail/skip annotation.
            """
            ).format(len(violations))
        )
        return 1

    print("OK: no non-strict xfails or unconditional skips found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
