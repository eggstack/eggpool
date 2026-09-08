"""Executable O001 observations for the M9 operational boundary.

The fixture files are intentionally scalar and symbolic.  This module derives
the command shape from the checked-out Python Click object so a future command
addition cannot silently escape the frozen ownership matrix.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from eggpool.cli_full import cli

if TYPE_CHECKING:
    from collections.abc import Iterable


FIXTURE_DIR = Path(__file__).parents[2] / "migration-rs" / "fixtures" / "operations"
MATRIX_PATH = FIXTURE_DIR / "o001-fixture-matrix.json"
OBSERVATIONS_PATH = FIXTURE_DIR / "o001-python-observations.json"


def load_json(path: Path) -> dict[str, Any]:
    """Load one checked-in structured observation fixture."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"fixture must be an object: {path}")
    return value


def fixture_matrix() -> dict[str, Any]:
    """Return the O001 command/effect matrix."""
    return load_json(MATRIX_PATH)


def python_command_inventory() -> list[dict[str, Any]]:
    """Derive the current Python command paths and option names from Click."""

    def walk(
        command: click.Command, prefix: tuple[str, ...] = ()
    ) -> Iterable[dict[str, Any]]:
        if not isinstance(command, click.Group):
            return
        context = click.Context(command)
        info = command.to_info_dict(context)
        for name in sorted(info.get("commands", {})):
            child = command.commands[name]
            child_info = child.to_info_dict(click.Context(child))
            options: list[str] = []
            for parameter in child_info.get("params", []):
                if parameter.get("name") == "help":
                    continue
                if parameter.get("param_type_name") == "option":
                    options.extend(parameter.get("opts", []))
                    options.extend(parameter.get("secondary_opts", []))
            yield {
                "path": " ".join((*prefix, name)),
                "options": sorted(options),
            }
            yield from walk(child, (*prefix, name))

    return list(walk(cli))


def fixture_command_inventory() -> list[dict[str, Any]]:
    """Return the normalized command rows committed in the matrix."""
    rows = fixture_matrix().get("commands")
    if not isinstance(rows, list):
        raise TypeError("commands fixture must be a list")
    return [
        {
            "path": row["path"],
            "owner": row["owner"],
            "options": sorted(row["options"]),
        }
        for row in rows
    ]


def walk_strings(value: object) -> Iterable[str]:
    """Yield every string in a JSON-compatible value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from walk_strings(key)
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)


def assert_secret_free(value: object) -> None:
    """Reject secret-shaped values and host-specific paths in observations."""
    forbidden = (
        "api-key",
        "api_key=",
        "authorization:",
        "bearer ",
        "password=",
        "/Users/",
        "/home/",
        "eggpool-migration-",
        "127.0.0.1:",
        "localhost:",
    )
    for item in walk_strings(value):
        lowered = item.lower()
        if any(token in lowered for token in forbidden) or re.search(
            r"\bsk-[a-z0-9]{8,}\b", lowered
        ):
            raise AssertionError(f"secret or host-specific value in fixture: {item}")


def deferred_task_names() -> tuple[str, ...]:
    """Return the three R008 capabilities intentionally left to M9."""
    from eggpool.runtime_task_inventory import RUNTIME_TASK_INVENTORY

    names = {spec.name for spec in RUNTIME_TASK_INVENTORY}
    return tuple(
        name
        for name in ("metrics_flush", "update_checker", "automatic_backup")
        if name in names
    )
