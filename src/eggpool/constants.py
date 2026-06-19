"""Project-wide constants."""

import os
from pathlib import Path

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 11300
DEFAULT_DATABASE_PATH = "usage.sqlite3"
API_V1_PREFIX = "/v1"
MAX_REQUEST_BODY_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_SSE_FRAME_SIZE = 64 * 1024  # 64 KB

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
PID_FILE = RUNTIME_DIR / "eggpool.pid"
