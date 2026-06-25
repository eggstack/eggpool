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

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
PID_FILE = RUNTIME_DIR / "eggpool.pid"

PLACEHOLDER_API_KEYS: frozenset[str] = frozenset(
    {
        "your-proxy-api-key",
        "your-opencode-go-key-1",
        "your-opencode-go-key-2",
        "your-api-key-here",
        "your-local-api-key-here",
    }
)
