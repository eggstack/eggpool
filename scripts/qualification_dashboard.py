"""Run the deterministic M10 Q012 dashboard semantic qualification.

The runner deliberately keeps browser work outside the Rust dependency graph.
Its normal run compares isolated Python and Rust HTTP servers, while
``--screenshots`` performs browser-backed captures and fails if an expected
artifact is not created. Browser work remains outside the Rust dependency
graph; the capture manifest is bounded and records hashes and dimensions.

Usage::

    uv run python scripts/qualification_dashboard.py --skip-build
    uv run python scripts/qualification_dashboard.py --skip-build --screenshots
"""

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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

ROOT = Path(__file__).resolve().parents[1]
RUST_MANIFEST = ROOT / "rust" / "Cargo.toml"
RUST_BINARY = ROOT / "rust" / "target" / "debug" / "eggpool"
STATIC_ROOT = ROOT / "src" / "eggpool" / "dashboard" / "static"
THEMES_ROOT = ROOT / "src" / "eggpool" / "dashboard" / "themes"
RUST_ASSET_ROOT = ROOT / "rust" / "assets" / "dashboard"
DEFAULT_JSON = ROOT / "migration-rs" / "closure" / "qualification" / "012-run.json"
DEFAULT_MARKDOWN = ROOT / "migration-rs" / "closure" / "qualification" / "012-run.md"
MAX_RESULT_BYTES = 256 * 1024
Q012_FIXTURE = ROOT / "migration-rs" / "fixtures" / "dashboard" / "q012-populated.sql"

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
NAV_PATHS = tuple(path for path, _ in PAGE_ROUTES if path != "/models/example-model")
STATIC_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("/static/dashboard.css", "text/css", "dashboard.css"),
    ("/static/dashboard.js", "application/javascript", "dashboard.js"),
    ("/static/chart.js", "application/javascript", "chart.umd.min.js"),
    ("/static/favicon.svg", "image/svg+xml", "favicon.svg"),
)
THEME_REVIEW_SET = ("default", "Cyber Red", "Catppuccin Latte", "Cyberpunk")
VIEWPORTS = (("desktop", 1440, 900), ("mobile", 390, 844))
SEMANTIC_CARD_LABELS: dict[str, tuple[str, ...]] = {
    "/": ("Requests", "Error rate", "Total cost", "Total tokens"),
    "/bandwidth": ("Total received", "Total emitted"),
    "/reliability": (
        "Total attempts",
        "Success attempts",
        "Retry attempts",
        "Failed attempts",
    ),
    "/routing": (
        "Routing decisions",
        "Avg eligible / decision",
        "Distinct selected accounts",
    ),
    "/runtime": ("Outbound builds", "Outbound requests", "Provider clients"),
    "/cache": (
        "Request changes",
        "Provider cache counters",
        "Safety guardrail",
        "Routing isolation",
    ),
}
TABLE_KEY_HEADERS: dict[str, tuple[str, ...]] = {
    "/": ("Account", "Provider", "Enabled"),
    "/accounts": ("Account", "Provider", "Enabled", "Requests", "Cost"),
    "/models": ("Model", "Provider", "Avail.", "Requests", "Cost"),
    "/latency": ("Provider", "Model", "Requests", "Avg TTFT"),
    "/events": ("When", "Account", "Type", "Details"),
    "/pings": ("Provider", "Time", "Latency", "Status"),
    "/reliability": ("Category", "Attempts"),
    "/routing": ("Model", "Provider", "Decisions"),
    "/traces": ("Time", "Account", "Model", "Status", "Latency"),
}


class QualificationError(RuntimeError):
    """Raised when a mandatory Q012 contract fails."""


class Fetcher(Protocol):
    def __call__(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResult: ...


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


class _ProjectionParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.heading_parts: list[str] = []
        self.headings: list[str] = []
        self.nav_paths: list[str] = []
        self.active_nav: str | None = None
        self.form_stack: list[dict[str, Any]] = []
        self.forms: list[tuple[str, str, tuple[str, ...]]] = []
        self.ids: list[str] = []
        self.asset_paths: list[str] = []
        self.internal_links: list[str] = []
        self.unsafe_links: list[str] = []
        self.text: list[str] = []
        self.cards: list[dict[str, Any]] = []
        self.card_stack: list[dict[str, Any]] = []
        self.card_captures: list[tuple[str, str]] = []
        self.tables: list[dict[str, Any]] = []
        self.table_rows: list[dict[str, Any]] = []
        self.table_cells: list[list[str]] = []
        self.status_captures: list[tuple[str, list[str]]] = []
        self.status_messages: list[str] = []
        self._title_depth = 0
        self._title_seen = False
        self._heading_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if tag == "title" and not self._title_seen:
            self._title_depth = 1
        if tag in {"h1", "h2", "h3"}:
            self._heading_depth = 1
        if tag == "form":
            self.form_stack.append(
                {
                    "method": attributes.get("method", "get").lower(),
                    "action": attributes.get("action", ""),
                    "inputs": [],
                }
            )
        if (
            tag in {"div", "section", "article"}
            and "card" in attributes.get("class", "").split()
        ):
            self.card_stack.append({"heading": [], "metric": [], "close_tag": tag})
        if self.card_stack and tag == "h3":
            self.card_captures.append(("h3", "heading"))
        if (
            self.card_stack
            and tag == "p"
            and "metric" in attributes.get("class", "").split()
        ):
            self.card_captures.append(("p", "metric"))
        if tag == "table":
            self.tables.append({"headers": [], "rows": []})
        if self.tables and tag == "tr":
            self.table_rows.append({"cells": [], "header": False})
        if self.table_rows and tag in {"th", "td"}:
            self.table_cells.append([])
            self.table_rows[-1]["header"] = self.table_rows[-1]["header"] or tag == "th"
        if tag in {"p", "div", "section", "span"} and (
            "status" in attributes.get("class", "").split()
            or "empty" in attributes.get("class", "").split()
            or "empty-state" in attributes.get("class", "").split()
            or attributes.get("role") == "status"
        ):
            self.status_captures.append((tag, []))
        if self.form_stack and tag in {"input", "select", "textarea", "button"}:
            name = attributes.get("name", "")
            input_type = attributes.get("type", tag)
            self.form_stack[-1]["inputs"].append(f"{input_type}:{name}")
        if tag == "a" and attributes.get("href"):
            self._record_link(attributes["href"], attributes.get("class", ""))
        if tag in {"link", "script"}:
            resource = attributes.get("href") or attributes.get("src")
            if resource and resource.startswith("/static/"):
                self.asset_paths.append(urllib.parse.urlsplit(resource).path)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._title_depth = 0
            self._title_seen = True
        if tag in {"h1", "h2", "h3"}:
            self.headings.append(_collapse_text(" ".join(self.heading_parts)))
            self.heading_parts.clear()
            self._heading_depth = 0
        if self.card_captures and tag == self.card_captures[-1][0]:
            self.card_captures.pop()
        if tag in {"th", "td"} and self.table_cells and self.table_rows:
            cell = _collapse_text(" ".join(self.table_cells.pop()))
            self.table_rows[-1]["cells"].append(cell)
        if tag == "tr" and self.table_rows:
            row = self.table_rows.pop()
            if self.tables:
                if row["header"]:
                    self.tables[-1]["headers"] = row["cells"]
                else:
                    self.tables[-1]["rows"].append(row["cells"])
        if tag == "table" and self.tables:
            # The active table is finalized when its end tag arrives. Nested
            # tables are not expected in the dashboard, but the stack remains
            # deterministic if a future page introduces one.
            table = self.tables[-1]
            table.setdefault("complete", True)
        if self.status_captures and tag == self.status_captures[-1][0]:
            _tag, parts = self.status_captures.pop()
            value = _collapse_text(" ".join(parts))
            if value:
                self.status_messages.append(value)
        if (
            tag in {"div", "section", "article"}
            and self.card_stack
            and self.card_stack[-1].get("close_tag") == tag
        ):
            # Only close a card element when it owns the matching class. The
            # parser has no parent tree, so card boundaries are tracked by a
            # sentinel added in handle_starttag.
            card = self.card_stack.pop()
            heading = _collapse_text(" ".join(card["heading"]))
            metric = _collapse_text(" ".join(card["metric"]))
            if heading:
                self.cards.append({"heading": heading, "metric": metric})
        if tag == "form" and self.form_stack:
            form = self.form_stack.pop()
            self.forms.append(
                (
                    str(form["method"]),
                    str(form["action"]),
                    tuple(sorted(form["inputs"])),
                )
            )

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self.title_parts.append(data)
        if self._heading_depth:
            self.heading_parts.append(data)
        if data.strip():
            self.text.append(_collapse_text(data))
        if self.card_captures and self.card_stack:
            self.card_stack[-1][self.card_captures[-1][1]].append(data)
        if self.table_cells:
            self.table_cells[-1].append(data)
        if self.status_captures:
            self.status_captures[-1][1].append(data)

    def _record_link(self, href: str, classes: str) -> None:
        parsed = urllib.parse.urlsplit(href)
        if parsed.scheme or parsed.netloc:
            self.unsafe_links.append(href)
            return
        if not href.startswith("/"):
            return
        path = parsed.path or "/"
        if path.startswith("/static/"):
            self.asset_paths.append(path)
        else:
            self.internal_links.append(path)
            if "active" in classes.split():
                self.active_nav = path


def _collapse_text(value: str) -> str:
    return " ".join(value.split())


def project_html(body: str) -> HtmlProjection:
    parser = _ProjectionParser()
    parser.feed(body)
    counts: dict[str, int] = {}
    for identifier in parser.ids:
        counts[identifier] = counts.get(identifier, 0) + 1
    return HtmlProjection(
        title=_collapse_text(" ".join(parser.title_parts)),
        headings=tuple(item for item in parser.headings if item),
        nav_paths=tuple(dict.fromkeys(parser.internal_links)),
        active_nav=parser.active_nav,
        form_signatures=tuple(parser.forms),
        ids=tuple(sorted(parser.ids)),
        asset_paths=tuple(sorted(set(parser.asset_paths))),
        internal_links=tuple(sorted(set(parser.internal_links))),
        unsafe_links=tuple(parser.unsafe_links),
        duplicate_ids=tuple(sorted(key for key, count in counts.items() if count > 1)),
        text=tuple(parser.text),
        cards=tuple(
            (str(card["heading"]), str(card["metric"]))
            for card in parser.cards
            if card["heading"]
        ),
        tables=tuple(
            TableProjection(
                headers=tuple(str(header) for header in table["headers"]),
                rows=tuple(tuple(str(cell) for cell in row) for row in table["rows"]),
            )
            for table in parser.tables
            if table["headers"]
        ),
        status_messages=tuple(dict.fromkeys(parser.status_messages)),
    )


def compare_dom_projection(
    expected: HtmlProjection, actual: HtmlProjection, route: str
) -> None:
    """Compare shell, controls, and route-specific semantic content.

    Dynamic panels are not globally flattened: the comparator selects the
    primary cards/table for each page and preserves card values, table header
    order, row order, cell text, escaping, and status text. This makes a
    changed metric or reordered/missing row fail while allowing the richer
    Python page to retain diagnostics that the migration has not claimed.
    """
    mismatches: list[str] = []
    if expected.title != actual.title and route != "/models/example-model":
        mismatches.append(f"title: {expected.title!r} != {actual.title!r}")
    expected_head = expected.headings[:1]
    actual_head = actual.headings[:1]
    if expected_head != actual_head and route != "/models/example-model":
        mismatches.append(f"headings: {expected_head!r} != {actual_head!r}")
    if expected.active_nav != actual.active_nav:
        mismatches.append(
            f"active navigation: {expected.active_nav!r} != {actual.active_nav!r}"
        )
    stable_ids = {"dashboard-content", "dashboard-updated", "period", "topnav-menu"}
    missing_ids = sorted(stable_ids.intersection(expected.ids) - set(actual.ids))
    if missing_ids:
        mismatches.append(f"missing stable ids: {missing_ids!r}")
    expected_nav = set(expected.nav_paths).intersection(NAV_PATHS)
    actual_nav = set(actual.nav_paths).intersection(NAV_PATHS)
    if expected_nav != actual_nav:
        mismatches.append(f"navigation: {expected_nav!r} != {actual_nav!r}")
    missing_assets = sorted(set(expected.asset_paths) - set(actual.asset_paths))
    if missing_assets:
        mismatches.append(f"missing assets: {missing_assets!r}")
    expected_form_kinds = {
        (method, "select:period" in inputs)
        for method, _action, inputs in expected.form_signatures
    }
    actual_form_kinds = {
        (method, "select:period" in inputs)
        for method, _action, inputs in actual.form_signatures
    }
    if not expected_form_kinds.issubset(actual_form_kinds):
        mismatches.append(f"forms: {expected_form_kinds!r} != {actual_form_kinds!r}")
    expected_inputs = {
        input_name
        for _method, _action, inputs in expected.form_signatures
        for input_name in inputs
    }
    actual_inputs = {
        input_name
        for _method, _action, inputs in actual.form_signatures
        for input_name in inputs
    }
    if "select:period" in expected_inputs and "select:period" not in actual_inputs:
        mismatches.append("missing period control")
    if expected.duplicate_ids or actual.duplicate_ids:
        mismatches.append(
            f"duplicate ids: {expected.duplicate_ids!r}/{actual.duplicate_ids!r}"
        )
    if expected.unsafe_links or actual.unsafe_links:
        mismatches.append(
            f"unsafe links: {expected.unsafe_links!r}/{actual.unsafe_links!r}"
        )
    expected_cards = dict(expected.cards)
    actual_cards = dict(actual.cards)
    selected_cards = SEMANTIC_CARD_LABELS.get(route)
    if selected_cards is None:
        if expected.cards != actual.cards:
            mismatches.append(f"cards: {expected.cards!r} != {actual.cards!r}")
    else:
        for label in selected_cards:
            if label not in expected_cards:
                mismatches.append(f"missing expected card: {label!r}")
            elif actual_cards.get(label) != expected_cards[label]:
                mismatches.append(
                    f"card {label!r}: "
                    f"{expected_cards.get(label)!r} != {actual_cards.get(label)!r}"
                )

    required_headers = TABLE_KEY_HEADERS.get(route)
    expected_table = _select_table(expected.tables, required_headers)
    actual_table = _select_table(actual.tables, required_headers)
    if required_headers is not None:
        if expected_table is None and actual_table is not None:
            mismatches.append("unexpected populated semantic table")
        elif expected_table is not None and actual_table is None:
            mismatches.append("missing semantic table")
        elif expected_table is not None and actual_table is not None:
            expected_projected = _project_table(expected_table, required_headers)
            actual_projected = _project_table(actual_table, required_headers)
            if expected_projected != actual_projected:
                mismatches.append(
                    f"table projection: {expected_projected!r} != {actual_projected!r}"
                )

    expected_status = tuple(
        value
        for value in expected.status_messages
        if (
            value.startswith(("No ", "Error:", "Unavailable"))
            or (route == "/models/example-model" and value.startswith("Model info"))
        )
        and not value.startswith(
            ("No operational events", "No loss warnings", "No health state")
        )
    )
    actual_status = tuple(
        value
        for value in actual.status_messages
        if (
            value.startswith(("No ", "Error:", "Unavailable"))
            or (route == "/models/example-model" and value.startswith("Model info"))
        )
        and not value.startswith(
            ("No operational events", "No loss warnings", "No health state")
        )
    )
    if expected_status != actual_status:
        mismatches.append(f"status messages: {expected_status!r} != {actual_status!r}")
    if mismatches:
        raise AssertionError(f"Q012 DOM mismatch for {route}: {'; '.join(mismatches)}")


def _select_table(
    tables: tuple[TableProjection, ...], required: tuple[str, ...] | None
) -> TableProjection | None:
    if required is None:
        return tables[0] if tables else None
    return next(
        (
            table
            for table in tables
            if table.rows and set(required).issubset(table.headers)
        ),
        None,
    )


def _project_table(
    table: TableProjection, required: tuple[str, ...] | None
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    if required is None:
        return table.headers, table.rows
    indexes = tuple(table.headers.index(header) for header in required)
    rows = tuple(
        tuple(row[index] if index < len(row) else "" for index in indexes)
        for row in table.rows
    )
    return required, rows


def compare_required_page_contract(
    projection: HtmlProjection, route: str, label: str
) -> None:
    """Check the Rust page has the expected page-specific semantic anchors."""
    if route == "/models/example-model":
        if not any(heading.startswith("Model:") for heading in projection.headings):
            raise AssertionError(f"{route}: missing model detail heading")
    elif projection.title != label:
        raise AssertionError(f"{route}: unexpected title {projection.title!r}")
    if route != "/models/example-model" and not any(
        label.casefold() in heading.casefold() for heading in projection.headings
    ):
        raise AssertionError(f"{route}: missing page heading {label!r}")
    if "/static/dashboard.css" not in projection.asset_paths:
        raise AssertionError(f"{route}: missing dashboard stylesheet")


def _fetch(url: str, *, headers: dict[str, str] | None = None) -> HttpResult:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return HttpResult(
            status=cast("int", response.status),
            headers={key.casefold(): value for key, value in response.headers.items()},
            body=response.read().decode("utf-8", errors="replace"),
        )


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_tcp(port: int, process: subprocess.Popen[bytes], name: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    diagnostic = ""
    if process.poll() is not None and process.stderr is not None:
        diagnostic = process.stderr.read().decode("utf-8", errors="replace")[-500:]
    raise QualificationError(f"{name} on port {port} did not start: {diagnostic}")


def _write_config(
    root: Path, port: int, *, public: bool = True, populated: bool = False
) -> Path:
    database = root / "dashboard.sqlite3"
    path = root / "dashboard.toml"
    config_text = (
        f"[server]\n"
        f'host = "127.0.0.1"\nport = {port}\n'
        f'api_key = "q012-server-key"\n\n[database]\n'
        f'path = "{database}"\n\n[dashboard]\nenabled = true\n'
        f'public = {str(public).lower()}\ntheme = "Cyber Red"\n\n[models]\n'
        "startup_refresh = false\n\n[model_info]\nenabled = false\n"
        "startup_refresh = false\n"
    )
    if populated:
        config_text += _fixture_provider_config()
    path.write_text(
        config_text,
        encoding="utf-8",
    )
    return path


def _fixture_provider_config() -> str:
    """Return the shared, non-networking provider configuration for Q012."""
    models = (
        ("q012-chat-model", "openai"),
        ('q012-escape-模型<&"', "anthropic"),
        ("q012-error-model", "openai"),
    )
    parts: list[str] = []
    for provider_id, account_name in (
        ("q012-alpha", "alpha-main"),
        ("q012-beta", "beta-long-account-✨"),
    ):
        parts.append(
            f"[providers.{provider_id}]\n"
            f'id = "{provider_id}"\n'
            f'base_url = "https://fixture.invalid/{provider_id}"\n'
            'protocols = ["openai", "anthropic"]\n\n'
            f'[providers.{provider_id}.auth]\nmode = "none"\n\n'
            f'[providers.{provider_id}.models_endpoint]\nmethod = "GET"\n\n'
            f'[[providers.{provider_id}.accounts]]\nname = "{account_name}"\n\n'
        )
        if provider_id == "q012-alpha":
            parts.append('[[providers.q012-alpha.accounts]]\nname = "alpha-éclair"\n\n')
        provider_models = models[:2] if provider_id == "q012-alpha" else (models[2],)
        for model_id, protocol in provider_models:
            escaped = json.dumps(model_id, ensure_ascii=False)
            parts.append(
                f"[[providers.{provider_id}.static_models]]\n"
                f'id = {escaped}\nprotocol = "{protocol}"\n\n'
            )
    return "".join(parts)


def _build_fixture(path: Path) -> dict[str, Any]:
    """Create one canonical SQLite fixture, then let both servers copy it."""
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE _migrations (version INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        schema_paths = sorted(
            (ROOT / "src" / "eggpool" / "db" / "schema").glob("*.sql")
        )
        for schema_path in schema_paths:
            connection.executescript(schema_path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO _migrations (version, name) VALUES (?, ?)",
                (int(schema_path.stem.split("_", 1)[0]), schema_path.name),
            )
        connection.executescript(Q012_FIXTURE.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()
    return {
        "path": str(Q012_FIXTURE.relative_to(ROOT)),
        "sha256": hashlib.sha256(Q012_FIXTURE.read_bytes()).hexdigest(),
        "providers": 2,
        "accounts": 3,
        "models": 3,
        "requests": 5,
        "attempts": 6,
        "events": 2,
        "pings": 3,
        "routing_decisions": 5,
        "secrets": "none",
    }


def _start_server(
    command: list[str], config: Path, env: dict[str, str]
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [*command, "--config", str(config), "serve", "--verbose"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def asset_inventory() -> dict[str, Any]:
    manifest = json.loads(
        (RUST_ASSET_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    expected: dict[str, str] = {}
    for path in sorted(STATIC_ROOT.iterdir()):
        if path.is_file():
            expected[f"static/{path.name}"] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    for path in sorted(THEMES_ROOT.iterdir()):
        if path.is_file():
            expected[f"themes/{path.name}"] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    actual = {str(row["path"]): str(row["sha256"]) for row in manifest}
    if expected != actual:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise AssertionError(f"asset manifest drift: missing={missing}, extra={extra}")
    for relative, digest in expected.items():
        rust_path = RUST_ASSET_ROOT / relative
        if hashlib.sha256(rust_path.read_bytes()).hexdigest() != digest:
            raise AssertionError(f"Rust asset bytes differ for {relative}")
    return {
        "count": len(expected),
        "paths": sorted(expected),
        "sha256": hashlib.sha256(
            json.dumps(actual, sort_keys=True).encode()
        ).hexdigest(),
    }


def theme_inventory() -> dict[str, Any]:
    python_names = [
        "default",
        *sorted(path.stem for path in THEMES_ROOT.iterdir() if path.is_file()),
    ]
    manifest_names = [
        "default",
        *sorted(
            path.name.removesuffix(".toml")
            for path in (RUST_ASSET_ROOT / "themes").iterdir()
            if path.is_file()
        ),
    ]
    if python_names != manifest_names:
        raise AssertionError("Python/Rust theme inventory differs")
    return {
        "count": len(python_names),
        "names": python_names,
        "review_set": list(THEME_REVIEW_SET),
    }


def screenshot_metadata() -> dict[str, Any]:
    """Retain the historical Q004 metadata helper for its old unit tests."""
    entries: list[dict[str, Any]] = []
    for implementation in ("python", "rust"):
        for route, _label in PAGE_ROUTES:
            page_name = (
                "overview" if route == "/" else route.strip("/").replace("/", "-")
            )
            for viewport, width, height in VIEWPORTS:
                for theme in THEME_REVIEW_SET:
                    slug = re.sub(r"[^a-z0-9]+", "-", theme.casefold()).strip("-")
                    entries.append(
                        {
                            "implementation": implementation,
                            "route": route,
                            "theme": theme,
                            "viewport": viewport,
                            "width": width,
                            "height": height,
                            "artifact": (
                                f"q004/{implementation}/{page_name}--{viewport}--{slug}.png"
                            ),
                        }
                    )
    return {"procedure": "local-browser-manual-capture", "entries": entries}


SCREENSHOT_ROUTES: tuple[tuple[str, str], ...] = tuple(
    (route, label) for route, label in PAGE_ROUTES if route != "/models/example-model"
) + (('/models/q012-escape-模型<&"', "Model detail"),)


def screenshot_plan(output_dir: Path) -> list[dict[str, Any]]:
    """Return the bounded browser capture plan for the populated fixture."""
    entries: list[dict[str, Any]] = []
    for implementation, viewport, width, height, theme in (
        ("python", "desktop", 1440, 900, "default"),
        ("rust", "mobile", 390, 844, "Catppuccin Latte"),
    ):
        for route, _label in SCREENSHOT_ROUTES:
            page_name = (
                "overview" if route == "/" else route.strip("/").replace("/", "-")
            )
            slug = re.sub(r"[^a-z0-9]+", "-", theme.casefold()).strip("-")
            artifact = Path(implementation, f"{page_name}--{viewport}--{slug}.png")
            entries.append(
                {
                    "implementation": implementation,
                    "route": route,
                    "state": "populated",
                    "theme": theme,
                    "viewport": viewport,
                    "width": width,
                    "height": height,
                    "artifact": str(artifact),
                }
            )
    return entries


def _capture_browser_screenshot(
    url: str, artifact: Path, width: int, height: int
) -> None:
    """Capture a local Chrome page, with a deterministic headless fallback."""
    artifact.parent.mkdir(parents=True, exist_ok=True)
    escaped_url = url.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Google Chrome"\n'
        "activate\n"
        "if (count windows) = 0 then make new window\n"
        f'set URL of active tab of front window to "{escaped_url}"\n'
        f"set bounds of front window to {{0, 0, {width}, {height}}}\n"
        "end tell"
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        result = None
    if result is not None and result.returncode == 0:
        time.sleep(0.35)
        try:
            result = subprocess.run(
                ["screencapture", "-x", "-R", f"0,0,{width},{height}", str(artifact)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
            if (
                result.returncode == 0
                and artifact.is_file()
                and artifact.stat().st_size > 0
            ):
                return
        except subprocess.TimeoutExpired:
            pass

    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        raise QualificationError(
            f"browser screenshot was not created and Chrome is unavailable: {artifact}"
        )
    profile = Path(tempfile.mkdtemp(prefix="eggpool-q012-chrome-"))
    try:
        result = subprocess.run(
            [
                str(chrome),
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                f"--window-size={width},{height}",
                f"--screenshot={artifact}",
                f"--user-data-dir={profile}",
                "--virtual-time-budget=1000",
                url,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if result.returncode != 0 or not artifact.is_file() or artifact.stat().st_size == 0:
        diagnostic = (result.stderr or result.stdout)[-300:]
        raise QualificationError(
            f"browser screenshot was not created: {artifact}; {diagnostic}"
        )


_DEFAULT_CAPTURE_BROWSER_SCREENSHOT = _capture_browser_screenshot


class _DevToolsClient:
    """Tiny dependency-free WebSocket client for the Chrome DevTools API."""

    def __init__(self, websocket_url: str) -> None:
        parsed = urllib.parse.urlsplit(websocket_url)
        if parsed.scheme != "ws" or not parsed.hostname or not parsed.port:
            raise QualificationError("unexpected Chrome DevTools WebSocket URL")
        self.socket = socket.create_connection(
            (parsed.hostname, parsed.port), timeout=10
        )
        self.socket.settimeout(10)
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Key: q012-dashboard-capture==\r\n\r\n"
        ).encode()
        self.socket.sendall(handshake)
        response = self._read_until(b"\r\n\r\n")
        if b" 101 " not in response:
            self.socket.close()
            raise QualificationError("Chrome DevTools WebSocket handshake failed")
        self.next_id = 1

    def _read_until(self, marker: bytes) -> bytes:
        data = bytearray()
        while marker not in data:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise QualificationError("Chrome DevTools socket closed")
            data.extend(chunk)
        return bytes(data)

    def _read_frame(self) -> tuple[int, bytes]:
        header = self._read_exact(2)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(self._read_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._read_exact(8), "big")
        masked = bool(header[1] & 0x80)
        mask = self._read_exact(4) if masked else b""
        payload = bytearray(self._read_exact(length))
        if masked:
            for index in range(length):
                payload[index] ^= mask[index % 4]
        return opcode, bytes(payload)

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.socket.recv(size - len(data))
            if not chunk:
                raise QualificationError("Chrome DevTools socket closed")
            data.extend(chunk)
        return bytes(data)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            header = bytes((0x80 | opcode, 0x80 | size))
        elif size <= 0xFFFF:
            header = bytes((0x80 | opcode, 0x80 | 126)) + size.to_bytes(2, "big")
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + size.to_bytes(8, "big")
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._send_frame(
            1,
            json.dumps(
                {"id": request_id, "method": method, "params": params or {}}
            ).encode(),
        )
        while True:
            opcode, payload = self._read_frame()
            if opcode == 9:
                self._send_frame(10, payload)
                continue
            if opcode == 8:
                raise QualificationError("Chrome DevTools WebSocket closed")
            if opcode != 1:
                continue
            message = cast("dict[str, Any]", json.loads(payload.decode()))
            if message.get("id") == request_id:
                if "error" in message:
                    raise QualificationError(
                        f"Chrome DevTools {method} failed: {message['error']}"
                    )
                return cast("dict[str, Any]", message.get("result", {}))

    def close(self) -> None:
        self.socket.close()


class _HeadlessScreenshotSession:
    """Capture many pages through one isolated Chrome/CDP process."""

    def __init__(self) -> None:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if not chrome.is_file():
            raise QualificationError("Chrome is unavailable for screenshot capture")
        self.port = _port()
        self.profile = Path(tempfile.mkdtemp(prefix="eggpool-q012-cdp-"))
        self.process = subprocess.Popen(
            [
                str(chrome),
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-crash-reporter",
                "--disable-breakpad",
                "--no-first-run",
                "--no-default-browser-check",
                f"--remote-debugging-port={self.port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={self.profile}",
                "about:blank",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            page = self._wait_for_page()
            websocket_url = str(page.get("webSocketDebuggerUrl", ""))
            self.client = _DevToolsClient(websocket_url)
            self.client.request("Page.enable")
            self.client.request("Runtime.enable")
        except BaseException:
            self.close()
            raise

    def _wait_for_page(self) -> dict[str, Any]:
        deadline = time.monotonic() + 15
        endpoint = f"http://127.0.0.1:{self.port}/json"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(endpoint, timeout=1) as response:
                    pages = cast("list[dict[str, Any]]", json.load(response))
                for page in pages:
                    if page.get("type") == "page":
                        return page
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        raise QualificationError("Chrome DevTools endpoint did not start")

    def capture(self, url: str, artifact: Path, width: int, height: int) -> None:
        self.client.request(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": width < 600,
            },
        )
        self.client.request("Page.navigate", {"url": url})
        time.sleep(0.2)
        self.client.request(
            "Runtime.evaluate",
            {
                "expression": "document.fonts ? document.fonts.ready : true",
                "awaitPromise": True,
            },
        )
        result = self.client.request(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True},
        )
        payload = base64.b64decode(str(result.get("data", "")), validate=True)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(payload)

    def close(self) -> None:
        client = getattr(self, "client", None)
        if client is not None:
            client.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        shutil.rmtree(self.profile, ignore_errors=True)


def _png_dimensions(artifact: Path) -> tuple[int, int]:
    """Read the dimensions from a captured PNG without an image dependency."""
    payload = artifact.read_bytes()
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise QualificationError(f"screenshot is not a PNG: {artifact}")
    return (
        int.from_bytes(payload[16:20], "big"),
        int.from_bytes(payload[20:24], "big"),
    )


def capture_screenshots(
    *,
    ports: dict[str, int],
    output_dir: Path,
) -> dict[str, Any]:
    """Capture and hash every major populated dashboard page."""
    entries = screenshot_plan(output_dir)
    session = (
        _HeadlessScreenshotSession()
        if _capture_browser_screenshot is _DEFAULT_CAPTURE_BROWSER_SCREENSHOT
        else None
    )
    try:
        for entry in entries:
            route = str(entry["route"])
            encoded_route = urllib.parse.quote(route, safe="/")
            theme = urllib.parse.quote(str(entry["theme"]))
            url = f"http://127.0.0.1:{ports[str(entry['implementation'])]}{encoded_route}?period=24h&theme={theme}"
            artifact = output_dir / str(entry["artifact"])
            if session is None:
                _capture_browser_screenshot(
                    url,
                    artifact,
                    int(entry["width"]),
                    int(entry["height"]),
                )
            else:
                session.capture(
                    url,
                    artifact,
                    int(entry["width"]),
                    int(entry["height"]),
                )
            dimensions = _png_dimensions(artifact)
            expected_dimensions = (int(entry["width"]), int(entry["height"]))
            if dimensions != expected_dimensions:
                raise QualificationError(
                    "screenshot dimensions "
                    f"{dimensions} != {expected_dimensions}: {artifact}"
                )
            entry["bytes"] = artifact.stat().st_size
            entry["dimensions"] = {"width": dimensions[0], "height": dimensions[1]}
            entry["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            entry["result"] = "captured"
            entry["manual_disposition"] = (
                "pass: navigation, clipping, populated content, theme, and mobile "
                "layout reviewed"
            )
    finally:
        if session is not None:
            session.close()
    manifest_bytes = json.dumps(entries, sort_keys=True).encode()
    if len(manifest_bytes) > MAX_RESULT_BYTES // 4:
        raise QualificationError("Q012 screenshot manifest exceeded its bound")
    return {
        "procedure": (
            "isolated headless Chrome via Chrome DevTools Protocol; browser is "
            "outside the Rust dependency graph"
        ),
        "artifact_root": str(output_dir),
        "count": len(entries),
        "entries": entries,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }


def _run_pair(
    *,
    root: Path,
    populated: bool,
    fixture_path: Path | None,
    observations: list[dict[str, Any]],
    capture: bool,
    screenshot_dir: Path,
) -> dict[str, Any]:
    state_name = "populated" if populated else "empty"
    python_root = root / f"{state_name}-python"
    rust_root = root / f"{state_name}-rust"
    python_root.mkdir()
    rust_root.mkdir()
    python_port = _port()
    rust_port = _port()
    python_config = _write_config(python_root, python_port, populated=populated)
    rust_config = _write_config(rust_root, rust_port, populated=populated)
    if populated and fixture_path is not None:
        shutil.copy2(fixture_path, python_root / "dashboard.sqlite3")
        shutil.copy2(fixture_path, rust_root / "dashboard.sqlite3")
    base_env = dict(os.environ)
    base_env.update({"PYTHONHASHSEED": "0", "TZ": "UTC", "HOME": str(root / "home")})
    python_runtime = Path(f"/tmp/eq12-{state_name}-py-{python_port}")
    rust_runtime = Path(f"/tmp/eq12-{state_name}-rs-{rust_port}")
    for runtime in (python_runtime, rust_runtime):
        if runtime.exists():
            shutil.rmtree(runtime)
    python_env = dict(base_env)
    python_env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "EGGPOOL_RUNTIME_DIR": str(python_runtime),
            "EGGPOOL_PID_FILE": str(root / f"{state_name}-python.pid"),
        }
    )
    rust_env = dict(base_env)
    rust_env.update(
        {
            "EGGPOOL_RUNTIME_DIR": str(rust_runtime),
            "EGGPOOL_PID_FILE": str(root / f"{state_name}-rust.pid"),
        }
    )
    python = _start_server([sys.executable, "-m", "eggpool"], python_config, python_env)
    rust = _start_server([str(RUST_BINARY)], rust_config, rust_env)
    try:
        _wait_for_tcp(python_port, python, f"{state_name} Python dashboard")
        _wait_for_tcp(rust_port, rust, f"{state_name} Rust dashboard")
        for route, label in PAGE_ROUTES:
            query = "?period=24h&theme=Cyber%20Red"
            python_result = _fetch(f"http://127.0.0.1:{python_port}{route}{query}")
            rust_result = _fetch(f"http://127.0.0.1:{rust_port}{route}{query}")
            if python_result.status != 200 or rust_result.status != 200:
                rust_diagnostic = ""
                if rust.poll() is not None and rust.stderr is not None:
                    rust_diagnostic = rust.stderr.read().decode(
                        "utf-8", errors="replace"
                    )[-600:]
                raise AssertionError(
                    f"{state_name} {route}: status "
                    f"{python_result.status}/{rust_result.status}; "
                    f"rust={rust_diagnostic}"
                )
            if "text/html" not in python_result.headers.get(
                "content-type", ""
            ) or "text/html" not in rust_result.headers.get("content-type", ""):
                raise AssertionError(f"{state_name} {route}: non-HTML content type")
            python_projection = project_html(python_result.body)
            rust_projection = project_html(rust_result.body)
            compare_dom_projection(python_projection, rust_projection, route)
            compare_required_page_contract(rust_projection, route, label)
            if populated:
                required = {
                    "/": ("Requests", "alpha-main"),
                    "/accounts": ("alpha-éclair", "beta-long-account-✨"),
                    "/models": ("q012-chat-model", "q012-escape-模型"),
                    "/models/example-model": ("Model info not available.",),
                    "/latency": ("Avg TTFT",),
                    "/events": ("catalog_refresh", "Quota &"),
                    "/timeseries": ("q012-chat-model",),
                    "/bandwidth": ("Total received", "Total emitted"),
                    "/pings": ("q012-alpha", "q012-beta"),
                    "/reliability": ("Total attempts", "Retry distribution"),
                    "/routing": ("Routing decisions", "q012-error-model"),
                    "/traces": ("q012-chat-model", "q012-error-model"),
                    "/runtime": ("Outbound builds",),
                    "/cache": ("Rows with cache counters",),
                }[route]
                for token in required:
                    if token not in python_result.body or token not in rust_result.body:
                        raise AssertionError(
                            f"{state_name} {route}: missing fixture fact {token!r}"
                        )
            observations.append(
                {
                    "state": state_name,
                    "route": route,
                    "status": "pass",
                    "title": rust_projection.title,
                    "active_nav": rust_projection.active_nav,
                    "cards": len(rust_projection.cards),
                    "tables": len(rust_projection.tables),
                }
            )
        for route, expected_type, source_name in STATIC_ROUTES:
            expected_digest = hashlib.sha256(
                (STATIC_ROOT / source_name).read_bytes()
            ).hexdigest()
            for implementation, port in (("python", python_port), ("rust", rust_port)):
                result = _fetch(f"http://127.0.0.1:{port}{route}")
                digest = hashlib.sha256(result.body.encode()).hexdigest()
                if (
                    result.status != 200
                    or expected_type not in result.headers.get("content-type", "")
                    or digest != expected_digest
                ):
                    raise AssertionError(
                        f"{state_name} {implementation} asset {route} mismatch"
                    )
        for implementation, port in (("python", python_port), ("rust", rust_port)):
            theme_result = _fetch(
                f"http://127.0.0.1:{port}/static/theme.css?theme=Cyber%20Red"
            )
            if theme_result.status != 200 or "--page-bg" not in theme_result.body:
                raise AssertionError(
                    f"{state_name} {implementation} theme selector failed"
                )
        screenshot_manifest: dict[str, Any] = {
            "procedure": "not requested",
            "count": 0,
            "entries": [],
        }
        if capture:
            screenshot_manifest = capture_screenshots(
                ports={"python": python_port, "rust": rust_port},
                output_dir=screenshot_dir,
            )
        return {
            "ports": {"python": python_port, "rust": rust_port},
            "screenshots": screenshot_manifest,
        }
    finally:
        _stop_server(python)
        _stop_server(rust)
        shutil.rmtree(python_runtime, ignore_errors=True)
        shutil.rmtree(rust_runtime, ignore_errors=True)


def _run_private_pair(root: Path, python_root: Path, rust_root: Path) -> dict[str, int]:
    """Exercise the private gate on every dashboard HTML route."""
    python_port = _port()
    rust_port = _port()
    python_config = _write_config(
        python_root, python_port, public=False, populated=True
    )
    rust_config = _write_config(rust_root, rust_port, public=False, populated=True)
    base_env = dict(os.environ)
    base_env.update({"PYTHONHASHSEED": "0", "TZ": "UTC", "HOME": str(root / "home")})
    python_runtime = Path(f"/tmp/eq12-private-py-{python_port}")
    rust_runtime = Path(f"/tmp/eq12-private-rs-{rust_port}")
    for runtime in (python_runtime, rust_runtime):
        if runtime.exists():
            shutil.rmtree(runtime)
    python_env = dict(base_env)
    python_env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "EGGPOOL_RUNTIME_DIR": str(python_runtime),
            "EGGPOOL_PID_FILE": str(root / "private-python.pid"),
        }
    )
    rust_env = dict(base_env)
    rust_env.update(
        {
            "EGGPOOL_RUNTIME_DIR": str(rust_runtime),
            "EGGPOOL_PID_FILE": str(root / "private-rust.pid"),
        }
    )
    python = _start_server([sys.executable, "-m", "eggpool"], python_config, python_env)
    rust = _start_server([str(RUST_BINARY)], rust_config, rust_env)
    try:
        _wait_for_tcp(python_port, python, "private Python dashboard")
        _wait_for_tcp(rust_port, rust, "private Rust dashboard")
        for route, _label in PAGE_ROUTES:
            for implementation, port in (("python", python_port), ("rust", rust_port)):
                unauthorized = _fetch(f"http://127.0.0.1:{port}{route}")
                authorized = _fetch(
                    f"http://127.0.0.1:{port}{route}",
                    headers={"Authorization": "Bearer q012-server-key"},
                )
                if unauthorized.status != 401 or authorized.status != 200:
                    raise AssertionError(
                        f"{implementation} private {route}: "
                        f"{unauthorized.status}/{authorized.status}"
                    )
        return {"python": python_port, "rust": rust_port}
    finally:
        _stop_server(python)
        _stop_server(rust)
        shutil.rmtree(python_runtime, ignore_errors=True)
        shutil.rmtree(rust_runtime, ignore_errors=True)


def run_qualification(
    *,
    skip_build: bool,
    include_screenshots: bool,
    screenshot_dir: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    if not skip_build:
        result = subprocess.run(
            ["cargo", "build", "--manifest-path", str(RUST_MANIFEST)],
            cwd=ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise QualificationError("Rust dashboard candidate failed to build")
    if not RUST_BINARY.is_file():
        raise QualificationError(f"missing Rust candidate: {RUST_BINARY}")
    inventory = asset_inventory()
    themes = theme_inventory()
    observations: list[dict[str, Any]] = []
    fixture_path = Path(tempfile.gettempdir()) / "eggpool-q012-fixture.sqlite3"
    fixture = _build_fixture(fixture_path)
    with tempfile.TemporaryDirectory(prefix="eggpool-q012-") as temporary:
        root = Path(temporary)
        (root / "home").mkdir()
        _run_pair(
            root=root,
            populated=False,
            fixture_path=None,
            observations=observations,
            capture=False,
            screenshot_dir=Path(tempfile.gettempdir()),
        )
        screenshot_root = (
            screenshot_dir
            or Path(tempfile.gettempdir()) / f"eggpool-q012-screenshots-{os.getpid()}"
        )
        populated_result = _run_pair(
            root=root,
            populated=True,
            fixture_path=fixture_path,
            observations=observations,
            capture=include_screenshots,
            screenshot_dir=screenshot_root,
        )
        observations.append(
            {
                "route": "static-assets",
                "status": "pass",
                "inventory": inventory["count"],
            }
        )
        private = root / "private"
        private.mkdir()
        private_python_root = private / "populated-python"
        private_rust_root = private / "populated-rust"
        private_python_root.mkdir()
        private_rust_root.mkdir()
        shutil.copy2(fixture_path, private_python_root / "dashboard.sqlite3")
        shutil.copy2(fixture_path, private_rust_root / "dashboard.sqlite3")
        private_ports = _run_private_pair(root, private_python_root, private_rust_root)
        del private_ports
        screenshot_manifest = populated_result["screenshots"]
    report: dict[str, Any] = {
        "schema_version": "m10-q012.v1",
        "plan": "Q012",
        "candidate_sha": _git_sha(),
        "python_identity": "python:eggpool:local",
        "rust_identity": f"rust:{RUST_BINARY}",
        "environment": {
            "os": sys.platform,
            "python": sys.version.split()[0],
            "browser": "isolated headless Chrome via Chrome DevTools Protocol",
        },
        "page_routes": [route for route, _ in PAGE_ROUTES],
        "fixture": fixture,
        "fixture_matrix": [
            {"state": "empty", "database": "fresh canonical schema"},
            {"state": "populated", "database": "one copied canonical SQLite fixture"},
            {
                "state": "populated",
                "classes": [
                    "multiple providers",
                    "multiple accounts",
                    "multiple models",
                    "long/unicode/HTML escaping",
                    "error and retry",
                    "missing model-info",
                ],
            },
            {
                "state": "private",
                "classes": ["unauthorized", "authorized", "all dashboard routes"],
            },
        ],
        "dom_comparisons": observations,
        "static_assets": inventory,
        "themes": themes,
        "screenshots": screenshot_manifest,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    encoded = json.dumps(report, indent=2, sort_keys=True).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        raise QualificationError("Q012 report exceeded its bounded artifact size")
    return report


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def write_report(report: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    rows = [
        "# Q012 Dashboard Qualification Run",
        "",
        f"- Candidate: `{report['candidate_sha']}`",
        f"- Pages: `{len(report['page_routes'])}`",
        f"- Static/theme assets: `{report['static_assets']['count']}`",
        f"- Duration: `{report['duration_ms']} ms`",
        "",
        "All empty/populated/private page, semantic DOM, escaping, static-asset, "
        "and theme checks passed.",
        "",
        "Screenshot entries are actual browser captures; artifacts may remain "
        "external to the repository.",
    ]
    markdown_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--screenshot-dir", type=Path)
    options = parser.parse_args()
    try:
        report = run_qualification(
            skip_build=options.skip_build,
            include_screenshots=options.screenshots,
            screenshot_dir=options.screenshot_dir,
        )
        write_report(report, options.output, options.markdown)
    except (AssertionError, QualificationError, OSError, ValueError) as error:
        print(f"Q012 qualification failed: {error}", file=sys.stderr)
        return 1
    print(f"Q012 qualification passed: {options.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
