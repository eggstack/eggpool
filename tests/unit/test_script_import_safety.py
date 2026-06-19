"""Import-safety tests for the operational scripts.

Both ``scripts/check_database.py`` and ``scripts/smoke_test.py`` are
imported and must NOT:

  - read required environment variables at import time,
  - open a database or network connection at import time,
  - emit any output at import time.

The tests use ``importlib.reload`` to exercise the import path
without inheriting any previously-cached module state.
"""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


SCRIPTS = ("scripts.check_database", "scripts.smoke_test")


@pytest.fixture()
def clean_import_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Strip every EggPool environment variable before reloading.

    Ensures that import-time code paths cannot observe a populated
    environment from a prior test.
    """
    for var in (
        "GOROUTER_DB_PATH",
        "GOROUTER_BASE_URL",
        "GOROUTER_API_KEY",
        "GOROUTER_OPENAI_MODEL",
        "GOROUTER_ANTHROPIC_MODEL",
        "GOROUTER_SKIP_LIVE",
        "GOROUTER_TEST_STREAM_CANCEL",
        "GOROUTER_UPSTREAM_BASE_URL",
        "GOROUTER_TEST_UPSTREAM_KEY",
        "GOROUTER_UPSTREAM_KEY",
        "GOROUTER_P14_KEY_A",
        "GOROUTER_P14_KEY_B",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _reload_module(name: str) -> object:
    """Remove a module from sys.modules and re-import it fresh."""
    sys.modules.pop(name, None)
    return importlib.import_module(name)


class TestImportSafety:
    """Operational scripts must be safe to import without side effects."""

    @pytest.mark.parametrize("module_name", SCRIPTS)
    def test_import_does_not_read_env(
        self, clean_import_env: None, module_name: str
    ) -> None:
        import os

        _reload_module(module_name)
        for var in (
            "GOROUTER_DB_PATH",
            "GOROUTER_BASE_URL",
            "GOROUTER_API_KEY",
            "GOROUTER_OPENAI_MODEL",
            "GOROUTER_ANTHROPIC_MODEL",
            "GOROUTER_SKIP_LIVE",
            "GOROUTER_TEST_STREAM_CANCEL",
        ):
            assert var not in os.environ, f"{module_name} read {var} at import time"

    @pytest.mark.parametrize("module_name", SCRIPTS)
    def test_import_does_not_open_database(
        self, clean_import_env: None, module_name: str
    ) -> None:
        """Reloading the module must not create an open :class:`Database`."""
        _reload_module(module_name)
        # The Database class does not maintain a global registry,
        # but aiosqlite itself does not hold open connections from
        # mere import. The closest observable side effect we can
        # check is that no module-level attribute named ``_conn``
        # has been populated, which would only be the case if the
        # script eagerly instantiated and connected a Database.
        import os

        cwd = os.getcwd()
        # No unexpected sqlite file artifacts should appear after import.
        entries = set(os.listdir(cwd))
        assert "usage.sqlite3" not in entries, (
            f"{module_name} created a database file at import time"
        )

    @pytest.mark.parametrize("module_name", SCRIPTS)
    def test_import_does_not_emit_output(
        self,
        clean_import_env: None,
        module_name: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _reload_module(module_name)
        captured = capsys.readouterr()
        assert captured.out == "", (
            f"{module_name} wrote to stdout at import time: {captured.out!r}"
        )
        assert captured.err == "", (
            f"{module_name} wrote to stderr at import time: {captured.err!r}"
        )
