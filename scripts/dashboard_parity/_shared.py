# pyright: reportUnusedImport=false
from __future__ import annotations

import argparse
import base64
import hashlib
import html.parser
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
RUST_MANIFEST = ROOT / "rust" / "Cargo.toml"
RUST_BINARY = ROOT / "rust" / "target" / "debug" / "eggpool"
RUST_ASSET_ROOT = ROOT / "rust" / "assets" / "dashboard"
DEFAULT_JSON = ROOT / "migration-rs" / "closure" / "qualification" / "012-run.json"
DEFAULT_MARKDOWN = ROOT / "migration-rs" / "closure" / "qualification" / "012-run.md"
MAX_RESULT_BYTES = 256 * 1024
ORACLE_COMMIT = "c23a70961f4b7858fdb0264cfb27b7ea26a8a334"
ORACLE_DIR = ROOT / "tests" / "fixtures" / "dashboard-python-oracle"
ORACLE_SOURCE_ROOT = Path(
    os.environ.get("EGGPOOL_DASHBOARD_ORACLE_ROOT", str(ROOT))
).resolve()
ORACLE_PYTHON = os.environ.get("EGGPOOL_DASHBOARD_ORACLE_PYTHON", sys.executable)
CAPTURES_DIR = ORACLE_DIR / "captures"
API_ROUTES = (
    "/api/timeseries",
    "/api/timeseries/grouped",
    "/api/stats/summary",
    "/api/stats/transcoding",
    "/api/stats/cache-observability",
    "/api/stats/canonical-request-segmentation",
    "/api/stats/cache-stability",
    "/api/stats/request-shaping",
)
VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
POPULATED_SQL_FIXTURE = (
    ROOT / "migration-rs" / "fixtures" / "dashboard" / "q012-populated.sql"
)
PAGE_ROUTES: tuple[tuple[str, str], ...] = (
    ("/", "Overview"),
    ("/accounts", "Accounts"),
    ("/models", "Models"),
    ("/models/example-model", "Model detail"),
    ("/latency", "Latency"),
    ("/events", "Events"),
    ("/timeseries", "Timeseries"),
    ("/bandwidth", "Bandwidth"),
    ("/pings", "Provider Pings"),
    ("/reliability", "Reliability"),
    ("/routing", "Routing"),
    ("/traces", "Traces"),
    ("/runtime", "Runtime"),
    ("/cache", "Cache"),
)
STATIC_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("/static/dashboard.css", "text/css", "dashboard.css"),
    ("/static/dashboard.js", "application/javascript", "dashboard.js"),
    ("/static/chart.js", "application/javascript", "chart.umd.min.js"),
    ("/static/favicon.svg", "image/svg+xml", "favicon.svg"),
)
THEME_REVIEW_SET = ("default", "Cyber Red", "Catppuccin Latte", "Cyberpunk")
VIEWPORTS = (("desktop", 1440, 900), ("mobile", 390, 844))
MANUALLY_REVIEWED_PAIRS = frozenset(
    {
        ("/", "default", "desktop"),
        ("/", "default", "mobile"),
        ("/accounts", "Cyber Red", "desktop"),
        ("/timeseries", "default", "desktop"),
        ("/timeseries", "Cyberpunk", "mobile"),
        ("/runtime", "default", "desktop"),
        ("/runtime", "default", "mobile"),
        ("/cache", "Catppuccin Latte", "desktop"),
        ("/models", "Cyber Red", "mobile"),
        ("/models/q012-chat-model", "Cyber Red", "mobile"),
    }
)


class QualificationError(RuntimeError):
    """Raised when a mandatory dashboard contract cannot be qualified."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: str


@dataclass(frozen=True)
class TableProjection:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class DomNode:
    """Complete ordered DOM projection; only insignificant whitespace is folded."""

    tag: str
    attributes: tuple[tuple[str, str], ...]
    children: tuple[DomNode | str, ...]


@dataclass(frozen=True)
class HtmlProjection:
    """Semantic DOM facts retained for cross-implementation comparison."""

    title: str
    headings: tuple[str, ...]
    nav_paths: tuple[str, ...]
    active_nav: str | None
    form_signatures: tuple[tuple[str, str, tuple[str, ...]], ...]
    ids: tuple[str, ...]
    asset_paths: tuple[str, ...]
    internal_links: tuple[str, ...]
    unsafe_links: tuple[str, ...]
    duplicate_ids: tuple[str, ...]
    text: tuple[str, ...]
    cards: tuple[tuple[str, str], ...]
    tables: tuple[TableProjection, ...]
    status_messages: tuple[str, ...]
    tree: DomNode


SCREENSHOT_ROUTES: tuple[tuple[str, str], ...] = tuple(
    (route, label) for route, label in PAGE_ROUTES if route != "/models/example-model"
) + (("/models/q012-chat-model", "Model detail"),)

__all__ = [
    "API_ROUTES",
    "Any",
    "CAPTURES_DIR",
    "DEFAULT_JSON",
    "DEFAULT_MARKDOWN",
    "DomNode",
    "HtmlProjection",
    "HttpResult",
    "MANUALLY_REVIEWED_PAIRS",
    "MAX_RESULT_BYTES",
    "ORACLE_COMMIT",
    "ORACLE_DIR",
    "ORACLE_PYTHON",
    "ORACLE_SOURCE_ROOT",
    "PAGE_ROUTES",
    "POPULATED_SQL_FIXTURE",
    "Path",
    "QualificationError",
    "ROOT",
    "RUST_ASSET_ROOT",
    "RUST_BINARY",
    "RUST_MANIFEST",
    "SCREENSHOT_ROUTES",
    "STATIC_ROUTES",
    "THEME_REVIEW_SET",
    "TableProjection",
    "VIEWPORTS",
    "VOID_ELEMENTS",
    "argparse",
    "asdict",
    "base64",
    "cast",
    "dataclass",
    "hashlib",
    "html",
    "json",
    "os",
    "re",
    "shutil",
    "socket",
    "sqlite3",
    "subprocess",
    "sys",
    "tempfile",
    "threading",
    "time",
    "urllib",
]
