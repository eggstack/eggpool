"""Config file helpers."""

from __future__ import annotations

import sys
from pathlib import Path

_MINIMAL_CONFIG = """\
[server]
host = "0.0.0.0"
port = 11300
log_level = "INFO"

[database]
path = "usage.sqlite3"

[models]
refresh_interval_s = 300
"""


def ensure_config(config_path: str) -> None:
    """Ensure a config file exists at *config_path*.

    If the file is missing, copy the bundled ``config.example.toml`` into
    place.  When the bundled copy is unavailable (e.g. running from a source
    checkout without ``_share``), a minimal working config is written instead.

    Only raises on write failure (e.g. permission denied).  If the file
    already exists this is a silent no-op.
    """
    path = Path(config_path)

    if path.exists():
        return

    try:
        from importlib.resources import as_file, files

        ref = files("eggpool._share").joinpath("config.example.toml")
        with as_file(ref) as source_path:
            if source_path.exists():
                import shutil

                shutil.copy2(source_path, path)
                sys.stdout.write(f"  Created {config_path} from bundled template\n")
                return
    except Exception:
        pass

    try:
        path.write_text(_MINIMAL_CONFIG, encoding="utf-8")
        sys.stdout.write(f"  Created {config_path}\n")
    except OSError as exc:
        raise RuntimeError(f"Cannot create config file {config_path}: {exc}") from exc
