"""Project-wide constants."""

import os
from pathlib import Path

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 11300
DEFAULT_PROVIDER_ID = "opencode-go"

# Synthetic model_id used to relink historical usage rows when a
# model is withdrawn upstream.  The original id is preserved in
# ``requests.original_model_id`` and ``reservations.original_model_id``
# so stats queries can still filter by the real model name.
DEPRECATED_MODEL_ID = "__deprecated__"

# Use an absolute path so the database is not dependent on the process
# working directory.  Follows XDG Base Directory conventions.
_xdg_data = os.environ.get("XDG_DATA_HOME", "")
if _xdg_data:
    _data_dir = Path(_xdg_data) / "eggpool"
else:
    _data_dir = Path.home() / ".local" / "share" / "eggpool"
DEFAULT_DATABASE_PATH = str(_data_dir / "usage.sqlite3")

API_V1_PREFIX = "/v1"
MAX_REQUEST_BODY_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_SSE_FRAME_SIZE = 64 * 1024  # 64 KB

# ``PID_FILE`` is kept for backwards compatibility with modules that
# still import it directly. The authoritative resolver is
# :func:`eggpool.runtime_paths.default_pid_file`, which honours
# ``$EGGPOOL_PID_FILE``, ``$XDG_RUNTIME_DIR``, and the per-user state
# directory with a UID-scoped ``/tmp`` fallback. The wrapped property
# below resolves the live path on every read so tests that monkey-patch
# environment variables see the updated value.
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))


class _PIDFileProxy:
    """Lazy proxy that always resolves through :func:`runtime_paths.default_pid_file`.

    Returning a ``Path`` instance from a class attribute is impossible
    at import time (the resolver depends on the current environment),
    so the constant is exposed as a proxy. ``str(PID_FILE)`` and
    ``PID_FILE / "x"`` both work because the proxy forwards
    ``__fspath__`` and the path operations fall through to the
    resolved :class:`pathlib.Path`.
    """

    def __getattr__(self, name: str) -> object:
        return getattr(self._resolve(), name)

    def __fspath__(self) -> str:
        return str(self._resolve())

    def __truediv__(self, other: object) -> Path:
        return self._resolve() / other  # type: ignore[operator]

    def __str__(self) -> str:
        return str(self._resolve())

    def __repr__(self) -> str:
        return repr(self._resolve())

    def __eq__(self, other: object) -> bool:
        return self._resolve() == other

    def __hash__(self) -> int:
        return hash(self._resolve())

    @staticmethod
    def _resolve() -> Path:
        from eggpool.runtime_paths import default_pid_file

        return default_pid_file()


PID_FILE = _PIDFileProxy()

PLACEHOLDER_API_KEYS: frozenset[str] = frozenset(
    {
        "your-proxy-api-key",
        "your-opencode-go-key-1",
        "your-opencode-go-key-2",
        "your-api-key-here",
        "your-local-api-key-here",
    }
)
