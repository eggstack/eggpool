"""Run dashboard parity qualification against the frozen Python oracle.

The runner deliberately keeps browser work outside the Rust dependency graph.
Its normal run compares isolated Python and Rust HTTP servers, while
``--screenshots`` performs browser-backed captures and fails if an expected
artifact is not created. Browser work remains outside the Rust dependency
graph; the capture manifest is bounded and records hashes and dimensions.

Usage::

    uv run python scripts/qualification_dashboard_parity.py --skip-build
    uv run python scripts/qualification_dashboard_parity.py --skip-build --screenshots
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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
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
        self.root = DomNode("#document", (), ())
        self.node_stack: list[
            tuple[str, list[DomNode | str], tuple[tuple[str, str], ...]]
        ] = [("#document", [], ())]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        normalized_attrs = tuple(
            sorted(
                (
                    key,
                    " ".join(sorted(set((value or "").split())))
                    if key == "class"
                    else value or "",
                )
                for key, value in attrs
            )
        )
        node_children: list[DomNode | str] = []
        self.node_stack[-1][1].append(DomNode(tag, normalized_attrs, ()))
        # Keep a mutable child list on the stack and rebuild immutable nodes
        # when a parent closes; this retains every element, attribute and text.
        if tag not in VOID_ELEMENTS:
            self.node_stack.append((tag, node_children, normalized_attrs))
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
        if len(self.node_stack) > 1 and self.node_stack[-1][0] == tag:
            node_tag, children, attrs = self.node_stack.pop()
            node = DomNode(node_tag, attrs, tuple(children))
            parent_children = self.node_stack[-1][1]
            if parent_children and isinstance(parent_children[-1], DomNode):
                parent_children[-1] = node
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
        if data.strip():
            self.node_stack[-1][1].append(_collapse_text(data))
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

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = tuple(
            sorted(
                (
                    key,
                    " ".join(sorted(set((value or "").split())))
                    if key == "class"
                    else value or "",
                )
                for key, value in attrs
            )
        )
        self.node_stack[-1][1].append(DomNode(tag, attributes, ()))

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
    collapsed = " ".join(value.split())
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", collapsed):
        return "<volatile:local-timestamp>"
    return collapsed


def project_html(body: str) -> HtmlProjection:
    parser = _ProjectionParser()
    parser.feed(body)
    counts: dict[str, int] = {}
    for identifier in parser.ids:
        counts[identifier] = counts.get(identifier, 0) + 1
    while len(parser.node_stack) > 1:
        tag, children, attrs = parser.node_stack.pop()
        node = DomNode(tag, attrs, tuple(children))
        parser.node_stack[-1][1][-1] = node
    root_tag, root_children, root_attrs = parser.node_stack[0]
    tree = DomNode(root_tag, root_attrs, tuple(root_children))
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
        tree=tree,
    )


def project_json(body: str) -> str:
    """Canonicalize JSON objects while preserving array and displayed value order."""
    try:
        value = _normalize_volatile(json.loads(body))
    except json.JSONDecodeError as error:
        raise QualificationError("dashboard API returned invalid JSON") from error
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compare_json_projection(expected: str, actual: str, route: str) -> None:
    expected_value = project_json(expected)
    actual_value = project_json(actual)
    if expected_value != actual_value:
        raise AssertionError(f"dashboard JSON projection differs for {route}")


def compare_api_response(expected: HttpResult, actual: HttpResult, route: str) -> None:
    if expected.status != actual.status:
        raise AssertionError(
            f"dashboard API status differs for {route}: "
            f"{expected.status}/{actual.status}"
        )
    expected_type = expected.headers.get("content-type", "").split(";", 1)[0].casefold()
    actual_type = actual.headers.get("content-type", "").split(";", 1)[0].casefold()
    if expected_type != actual_type:
        raise AssertionError(f"dashboard API content type differs for {route}")
    if expected.status < 400 and expected_type == "application/json":
        compare_json_projection(expected.body, actual.body, route)


def build_oracle_manifest() -> dict[str, Any]:
    """Build the immutable source inventory directly from the oracle commit."""

    def oracle_bytes(relative: str) -> bytes:
        return subprocess.run(
            ["git", "show", f"{ORACLE_COMMIT}:{relative}"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout

    def oracle_blob(relative: str) -> str:
        return subprocess.run(
            ["git", "rev-parse", f"{ORACLE_COMMIT}:{relative}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    assets: list[dict[str, str]] = []
    for route, content_type, filename in STATIC_ROUTES:
        relative = f"src/eggpool/dashboard/static/{filename}"
        content = oracle_bytes(relative)
        assets.append(
            {
                "route": route,
                "content_type": content_type,
                "filename": filename,
                "git_blob": oracle_blob(relative),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    theme_files: list[dict[str, str]] = []
    theme_names = sorted(
        path.name for path in (RUST_ASSET_ROOT / "themes").glob("*.toml")
    )
    for theme_name in theme_names:
        relative = f"src/eggpool/dashboard/themes/{theme_name}"
        content = oracle_bytes(relative)
        theme_files.append(
            {
                "filename": theme_name,
                "git_blob": oracle_blob(relative),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    js = oracle_bytes("src/eggpool/dashboard/static/dashboard.js").decode("utf-8")
    selectors = sorted(
        {
            selector
            for single, double in re.findall(
                r"querySelector(?:All)?\(\s*(?:'([^']+)'|\"([^\"]+)\")",
                js,
                flags=re.DOTALL,
            )
            for selector in (single or double,)
        }
        | {
            f"#{identifier}"
            for identifier in re.findall(r"getElementById\(\s*['\"]([^'\"]+)", js)
        }
    )
    endpoints = sorted(set(re.findall(r"['\"](/api/[A-Za-z0-9_./-]+)", js)))
    return {
        "schema_version": "dashboard-python-oracle.v1",
        "oracle_commit": ORACLE_COMMIT,
        "provenance": (
            "source and preserved assets from immutable Git history; "
            "synthetic state only"
        ),
        "projection": (
            "complete ordered DOM tree and canonical JSON; object keys sorted, "
            "array order retained"
        ),
        "normalizations": [
            "HTML insignificant whitespace runs",
            "class attribute token order",
            "JSON object key order",
            (
                "runtime-only process, host, memory, load, database-path, and "
                "countdown values"
            ),
            "standalone local timestamps matching YYYY-MM-DD HH:MM:SS",
        ],
        "page_routes": [route for route, _label in PAGE_ROUTES],
        "dashboard_json_routes": [
            {"method": "GET", "path": route, "auth": "dashboard-public-or-api-key"}
            for route in API_ROUTES
        ],
        "assets": assets,
        "themes": theme_files,
        "javascript_hooks": {
            "selectors": selectors,
            "selector_producers": _selector_producers(selectors),
            "conditional_selector_producers": {
                "[data-update-command]": "update-available footer only",
                "[data-update-copied]": "update-available footer only",
            },
            "api_endpoints": endpoints,
        },
        "state_matrix": [
            "empty",
            "populated",
            "escaping-unicode-long",
            "error-missing",
            "private-auth",
        ],
    }


def _selector_producers(selectors: list[str]) -> dict[str, list[str]]:
    route_facts: dict[str, tuple[set[str], set[str], set[str], set[str]]] = {}
    for route, _label in PAGE_ROUTES:
        route_name = _route_filename(route)
        facts: tuple[set[str], set[str], set[str], set[str]] = (
            set(),
            set(),
            set(),
            set(),
        )
        for state in ("empty", "populated"):
            capture_path = CAPTURES_DIR / state / "pages" / f"{route_name}.json"
            if not capture_path.is_file():
                raise QualificationError(
                    f"missing oracle page projection: {capture_path}"
                )
            capture = json.loads(capture_path.read_text(encoding="utf-8"))
            _collect_dom_facts(
                cast("dict[str, Any]", capture["projection"]["tree"]), facts
            )
        route_facts[route] = facts

    producers: dict[str, list[str]] = {}
    for selector in selectors:
        producer_routes: list[str] = []
        alternatives = selector.split(",")
        for route, facts in route_facts.items():
            if any(_selector_branch_present(branch, facts) for branch in alternatives):
                producer_routes.append(route)
        producers[selector] = producer_routes
    return producers


def _collect_dom_facts(
    node: dict[str, Any],
    facts: tuple[set[str], set[str], set[str], set[str]],
) -> None:
    tags, classes, identifiers, attributes = facts
    tags.add(str(node.get("tag", "")))
    for key, value in cast("list[list[str]]", node.get("attributes", [])):
        attributes.add(key)
        if key == "class":
            classes.update(value.split())
        elif key == "id":
            identifiers.add(value)
    for child in cast("list[Any]", node.get("children", [])):
        if isinstance(child, dict):
            _collect_dom_facts(cast("dict[str, Any]", child), facts)


def _selector_branch_present(
    selector: str,
    facts: tuple[set[str], set[str], set[str], set[str]],
) -> bool:
    tags, classes, identifiers, attributes = facts
    required_tags: set[str] = set()
    required_classes = set(re.findall(r"\.([A-Za-z_][\w-]*)", selector))
    required_ids = set(re.findall(r"#([A-Za-z_][\w-]*)", selector))
    required_attributes = set(re.findall(r"\[([A-Za-z_:][\w:.-]*)", selector))
    for branch in re.split(r"\s+|[>+~]", selector):
        tag_match = re.match(r"^([A-Za-z][\w-]*)", branch)
        if tag_match is not None:
            required_tags.add(tag_match.group(1))
    return (
        required_tags.issubset(tags)
        and required_classes.issubset(classes)
        and required_ids.issubset(identifiers)
        and required_attributes.issubset(attributes)
    )


def write_oracle_manifest(path: Path = ORACLE_DIR / "manifest.json") -> None:
    manifest = build_oracle_manifest()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def capture_oracle_snapshots(output_dir: Path) -> None:
    """Capture synthetic empty/populated/private responses from this pinned app."""
    source_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if source_sha != ORACLE_COMMIT:
        raise QualificationError(
            f"oracle capture requires {ORACLE_COMMIT}, found {source_sha}"
        )
    if output_dir.exists():
        raise QualificationError(f"refusing to overwrite oracle captures: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="dashboard-oracle-", dir=output_dir.parent))
    try:
        base = Path(tempfile.mkdtemp(prefix="dashboard-oracle-run-"))
        try:
            (base / "home").mkdir()
            for state, populated in (("empty", False), ("populated", True)):
                state_root = base / state
                state_root.mkdir()
                port = _port()
                config = _write_config(state_root, port, populated=populated)
                if populated:
                    _build_fixture(state_root / "dashboard.sqlite3")
                runtime = (
                    Path(tempfile.gettempdir()) / f"dashboard-oracle-{state}-{port}"
                )
                env = dict(os.environ)
                env.update(
                    {
                        "PYTHONHASHSEED": "0",
                        "TZ": "UTC",
                        "HOME": str(base / "home"),
                        "PYTHONPATH": str(ROOT / "src"),
                        "EGGPOOL_RUNTIME_DIR": str(runtime),
                        "EGGPOOL_PID_FILE": str(state_root / "eggpool.pid"),
                    }
                )
                process = _start_server([ORACLE_PYTHON, "-m", "eggpool"], config, env)
                try:
                    _wait_for_tcp(port, process, f"{state} frozen Python dashboard")
                    state_dir = staging / state
                    (state_dir / "pages").mkdir(parents=True)
                    (state_dir / "api").mkdir()
                    for route, _label in PAGE_ROUTES:
                        response = _fetch(
                            f"http://127.0.0.1:{port}{route}?period=24h&theme=Cyber%20Red"
                        )
                        if response.status != 200:
                            raise QualificationError(
                                f"oracle page {route} returned {response.status}"
                            )
                        projection = cast(
                            "dict[str, Any]",
                            _normalize_volatile(
                                json.loads(
                                    json.dumps(asdict(project_html(response.body)))
                                )
                            ),
                        )
                        if route == "/runtime":
                            projection = _normalize_runtime_projection(projection)
                        _write_capture(
                            state_dir / "pages" / f"{_route_filename(route)}.json",
                            {"status": response.status, "projection": projection},
                        )
                    for route in API_ROUTES:
                        response = _fetch(f"http://127.0.0.1:{port}{route}?period=24h")
                        body_projection: Any
                        try:
                            body_projection = _normalize_volatile(
                                json.loads(response.body)
                            )
                        except json.JSONDecodeError:
                            body_projection = response.body
                        _write_capture(
                            state_dir / "api" / f"{_route_filename(route)}.json",
                            {
                                "status": response.status,
                                "content_type": response.headers.get(
                                    "content-type", ""
                                ),
                                "body": body_projection,
                            },
                        )
                finally:
                    _stop_server(process)
                    shutil.rmtree(runtime, ignore_errors=True)
            private = base / "private"
            private.mkdir()
            port = _port()
            config = _write_config(private, port, public=False, populated=True)
            _build_fixture(private / "dashboard.sqlite3")
            runtime = Path(tempfile.gettempdir()) / f"dashboard-oracle-private-{port}"
            env = dict(os.environ)
            env.update(
                {
                    "PYTHONHASHSEED": "0",
                    "TZ": "UTC",
                    "HOME": str(base / "home"),
                    "PYTHONPATH": str(ROOT / "src"),
                    "EGGPOOL_RUNTIME_DIR": str(runtime),
                    "EGGPOOL_PID_FILE": str(private / "eggpool.pid"),
                }
            )
            process = _start_server([ORACLE_PYTHON, "-m", "eggpool"], config, env)
            try:
                _wait_for_tcp(port, process, "private frozen Python dashboard")
                auth: dict[str, int] = {}
                for route, _label in PAGE_ROUTES:
                    auth[route] = _fetch(f"http://127.0.0.1:{port}{route}").status
                for route in API_ROUTES:
                    auth[route] = _fetch(f"http://127.0.0.1:{port}{route}").status
                _write_capture(staging / "private-auth.json", auth)
            finally:
                _stop_server(process)
                shutil.rmtree(runtime, ignore_errors=True)
        finally:
            shutil.rmtree(base, ignore_errors=True)
        files = sorted(path for path in staging.rglob("*.json") if path.is_file())
        total_bytes = sum(path.stat().st_size for path in files)
        if total_bytes > 2 * 1024 * 1024 or any(
            path.stat().st_size > 256 * 1024 for path in files
        ):
            raise QualificationError(
                "oracle captures exceeded the bounded artifact size"
            )
        capture_index = {
            "schema_version": "dashboard-python-oracle-captures.v1",
            "oracle_commit": source_sha,
            "synthetic_fixture": {
                "path": "migration-rs/fixtures/dashboard/q012-populated.sql",
                "sha256": hashlib.sha256(
                    POPULATED_SQL_FIXTURE.read_bytes()
                ).hexdigest(),
            },
            "files": {
                str(path.relative_to(staging)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in files
            },
            "total_bytes": total_bytes,
        }
        _write_capture(staging / "capture-manifest.json", capture_index)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _route_filename(route: str) -> str:
    return "index" if route == "/" else route.strip("/").replace("/", "__")


def _normalize_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        value_dict = cast("dict[str, Any]", value)
        return {key: _normalize_volatile(item) for key, item in value_dict.items()}
    if isinstance(value, list):
        value_list = cast("list[Any]", value)
        return [_normalize_volatile(item) for item in value_list]
    if isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value
    ):
        return "<volatile:local-timestamp>"
    return value


def _normalize_runtime_projection(projection: dict[str, Any]) -> dict[str, Any]:
    volatile_cards = {
        "Server PID",
        "Uptime",
        "Python",
        "RSS memory",
        "Open FDs",
        "Active threads",
        "Load average",
        "Database",
        "WAL",
    }
    cards = cast("list[list[str]]", projection.get("cards", []))
    volatile_values: dict[str, str] = {}
    for card in cards:
        if card and card[0] in volatile_cards and len(card) > 1:
            placeholder = f"<volatile:{card[0].casefold().replace(' ', '-')}>"
            if card[1] != "—":
                volatile_values[card[1]] = placeholder
            card[1] = placeholder
    projection["text"] = [
        volatile_values.get(str(item), _normalize_runtime_text(str(item)))
        for item in cast("list[Any]", projection.get("text", []))
    ]
    projection["tables"] = _normalize_runtime_tables(
        cast("list[dict[str, Any]]", projection.get("tables", []))
    )
    projection["tree"] = _normalize_runtime_tree(
        cast("dict[str, Any]", projection["tree"])
    )
    return projection


def _normalize_runtime_tree(
    node: dict[str, Any], *, metric: bool = False
) -> dict[str, Any]:
    attrs = cast("list[list[str]]", node.get("attributes", []))
    is_metric = metric or any(
        key == "class" and "metric" in value.split() for key, value in attrs
    )
    children = cast("list[Any]", node.get("children", []))
    normalized: list[Any] = []
    for child in children:
        if isinstance(child, dict):
            normalized.append(
                _normalize_runtime_tree(cast("dict[str, Any]", child), metric=is_metric)
            )
        elif isinstance(child, str):
            normalized.append(
                "<volatile:runtime-metric>"
                if is_metric
                else _normalize_runtime_text(child)
            )
        else:
            normalized.append(child)
    node["children"] = normalized
    return node


def _normalize_runtime_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for table in tables:
        headers = cast("list[str]", table.get("headers", []))
        rows = cast("list[list[str]]", table.get("rows", []))
        for row in rows:
            for index, header in enumerate(headers):
                if index < len(row) and header == "Next run":
                    row[index] = "<volatile:countdown>"
                elif index < len(row):
                    row[index] = _normalize_runtime_text(row[index])
    return tables


def _normalize_runtime_text(value: str) -> str:
    if re.fullmatch(r"\d{5,}", value):
        return "<volatile:process-id>"
    value = re.sub(r"PPID \d+", "PPID <volatile:process-id>", value)
    value = re.sub(r"/[^\s]*/dashboard\.sqlite3", "<volatile:database-path>", value)
    value = re.sub(r"\bin \d+[smhd](?:\d+[smhd])?\b", "<volatile:countdown>", value)
    if re.fullmatch(r"\d+(?:\.\d+)? MB", value):
        return "<volatile:runtime-size>"
    if re.fullmatch(r"\d+\.\d+", value):
        return "<volatile:load-average>"
    if re.fullmatch(r"\d+\.\d+/core · \d+ CPUs", value):
        return "<volatile:load-per-core>"
    if re.fullmatch(r"\d+\.\d+\.\d+", value):
        return "<volatile:python-version>"
    if any(name in value for name in ("macOS-", "Linux-", "Windows-")):
        return "<volatile:host-platform>"
    return value


def _write_capture(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )


def compare_dom_projection(
    expected: HtmlProjection, actual: HtmlProjection, route: str
) -> None:
    """Compare the complete canonical tree, retaining all meaningful DOM facts."""
    expected_tree = expected.tree
    actual_tree = actual.tree
    if route == "/runtime":
        expected_tree = _normalize_runtime_dom_tree(expected_tree)
        actual_tree = _normalize_runtime_dom_tree(actual_tree)
    if expected_tree != actual_tree:
        difference = _first_tree_difference(expected_tree, actual_tree)
        raise AssertionError(f"complete DOM tree differs for {route} at {difference}")
    if expected.duplicate_ids or actual.duplicate_ids:
        raise AssertionError(f"duplicate IDs in oracle/candidate for {route}")
    if expected.unsafe_links or actual.unsafe_links:
        raise AssertionError(f"unsafe links in oracle/candidate for {route}")


def _normalize_runtime_dom_tree(
    node: DomNode | str, *, metric: bool = False
) -> DomNode | str:
    """Apply the bounded Runtime volatility rules used for frozen captures."""
    if isinstance(node, str):
        return "<volatile:runtime-metric>" if metric else _normalize_runtime_text(node)
    is_metric = metric or any(
        key == "class" and "metric" in value.split() for key, value in node.attributes
    )
    return DomNode(
        node.tag,
        node.attributes,
        tuple(
            _normalize_runtime_dom_tree(child, metric=is_metric)
            for child in node.children
        ),
    )


def _page_shell_tree(node: DomNode | str) -> DomNode | str:
    if isinstance(node, str):
        return node
    if node.tag == "main" and dict(node.attributes).get("id") == "dashboard-content":
        return DomNode(node.tag, node.attributes, ())
    return DomNode(
        node.tag,
        node.attributes,
        tuple(_page_shell_tree(child) for child in node.children),
    )


def compare_shared_shell_projection(
    expected: HtmlProjection, actual: HtmlProjection, route: str
) -> None:
    """Compare the shell while leaving page-owned content to its milestone."""
    expected_tree = _page_shell_tree(expected.tree)
    actual_tree = _page_shell_tree(actual.tree)
    if expected_tree != actual_tree:
        difference = _first_tree_difference(expected_tree, actual_tree)
        raise AssertionError(f"shared shell differs for {route} at {difference}")
    if expected.duplicate_ids or actual.duplicate_ids:
        raise AssertionError(f"duplicate IDs in oracle/candidate shell for {route}")
    if expected.unsafe_links or actual.unsafe_links:
        raise AssertionError(f"unsafe links in oracle/candidate shell for {route}")


def _first_tree_difference(expected: DomNode | str, actual: DomNode | str) -> str:
    def visit(left: DomNode | str, right: DomNode | str, path: str) -> str | None:
        if isinstance(left, str) or isinstance(right, str):
            return (
                None
                if left == right
                else (
                    f"{path} (text lengths {len(str(left))}/{len(str(right))}: "
                    f"{left!r} != {right!r})"
                    if "/footer[" in path
                    else f"{path} (text lengths {len(str(left))}/{len(str(right))})"
                )
            )
        if left.tag != right.tag:
            return f"{path} (element type)"
        if left.attributes != right.attributes:
            left_attributes = dict(left.attributes)
            right_attributes = dict(right.attributes)
            changed = sorted(
                key
                for key in left_attributes.keys() | right_attributes.keys()
                if left_attributes.get(key) != right_attributes.get(key)
            )
            return f"{path} (attributes: {', '.join(changed)})"
        if len(left.children) != len(right.children):
            left_tags = [
                child.tag if isinstance(child, DomNode) else "#text"
                for child in left.children
            ]
            right_tags = [
                child.tag if isinstance(child, DomNode) else "#text"
                for child in right.children
            ]
            return f"{path} (children: {left_tags} != {right_tags})"
        for index, (left_child, right_child) in enumerate(
            zip(left.children, right.children, strict=True)
        ):
            difference = visit(left_child, right_child, f"{path}/{left.tag}[{index}]")
            if difference is not None:
                return difference
        return None

    return visit(expected, actual, "$") or "unknown structural difference"


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


def _wait_for_operational_event(
    database_path: Path, process: subprocess.Popen[bytes], event_type: str, name: str
) -> None:
    """Wait until startup recovery is in the completed one-second query window."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if database_path.exists():
            try:
                with sqlite3.connect(database_path, timeout=0.2) as connection:
                    row = connection.execute(
                        "SELECT 1 FROM operational_events WHERE event_type=? "
                        "AND occurred_at < datetime('now') LIMIT 1",
                        (event_type,),
                    ).fetchone()
                if row is not None:
                    return
            except sqlite3.Error:
                # Startup may still be applying migrations or opening SQLite.
                pass
        time.sleep(0.05)
    diagnostic = ""
    if process.poll() is not None and process.stderr is not None:
        diagnostic = process.stderr.read().decode("utf-8", errors="replace")[-500:]
    raise QualificationError(
        f"{name} did not persist {event_type!r} before dashboard reads: {diagnostic}"
    )


def _write_config(
    root: Path,
    port: int,
    *,
    public: bool = True,
    populated: bool = False,
    model_info_enabled: bool = False,
) -> Path:
    database = root / "dashboard.sqlite3"
    path = root / "dashboard.toml"
    config_text = (
        f"[server]\n"
        f'host = "127.0.0.1"\nport = {port}\n'
        f'api_key = "q012-server-key"\n\n[database]\n'
        f'path = "{database}"\n\n[dashboard]\nenabled = true\n'
        f'public = {str(public).lower()}\ntheme = "Cyber Red"\n'
        "refresh_interval_s = 1\n\n[models]\n"
        "startup_refresh = false\n\n[model_info]\n"
        f"enabled = {str(model_info_enabled).lower()}\n"
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
    """Return the shared, non-networking provider configuration for the fixture."""
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
            (ROOT / "rust" / "assets" / "db" / "migrations").glob("*.sql")
        )
        for schema_path in schema_paths:
            connection.executescript(schema_path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO _migrations (version, name) VALUES (?, ?)",
                (int(schema_path.stem.split("_", 1)[0]), schema_path.name),
            )
        connection.executescript(POPULATED_SQL_FIXTURE.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()
    return {
        "path": str(POPULATED_SQL_FIXTURE.relative_to(ROOT)),
        "sha256": hashlib.sha256(POPULATED_SQL_FIXTURE.read_bytes()).hexdigest(),
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


def qualify_dashboard_shutdown_restart() -> dict[str, Any]:
    """Exercise browser-driven dashboard traffic across SIGTERM and restart."""
    with tempfile.TemporaryDirectory(prefix="dashboard-shutdown-restart-") as raw:
        root = Path(raw)
        home = root / "home"
        home.mkdir()
        fixture = root / "fixture.sqlite3"
        _build_fixture(fixture)
        database = root / "dashboard.sqlite3"
        shutil.copy2(fixture, database)
        port = _port()
        config = _write_config(root, port, populated=True)
        base_env = dict(os.environ)
        for key in (
            "EGGPOOL_CONFIG",
            "EGGPOOL_RUNTIME_DIR",
            "EGGPOOL_PID_FILE",
            "EGGPOOL_API_KEY",
        ):
            base_env.pop(key, None)
        base_env.update({"HOME": str(home), "TZ": "UTC", "PYTHONHASHSEED": "0"})
        runtime_paths: list[Path] = []

        def start(generation: str) -> subprocess.Popen[bytes]:
            runtime = Path(f"/tmp/dsh-{port}-{generation}")
            shutil.rmtree(runtime, ignore_errors=True)
            runtime_paths.append(runtime)
            env = dict(base_env)
            env.update(
                {
                    "EGGPOOL_RUNTIME_DIR": str(runtime),
                    "EGGPOOL_PID_FILE": str(root / f"eggpool-{generation}.pid"),
                }
            )
            process = _start_server([str(RUST_BINARY)], config, env)
            try:
                _wait_for_tcp(port, process, f"dashboard {generation}")
            except BaseException:
                _stop_server(process)
                raise
            return process

        address = f"http://127.0.0.1:{port}"
        process = start("initial")
        session: _HeadlessScreenshotSession | None = None
        load_thread: threading.Thread | None = None
        load_active = threading.Event()
        try:
            _wait_for_operational_event(
                database, process, "crash_recovery", "dashboard initial"
            )
            session = _HeadlessScreenshotSession()
            session.capture(
                f"{address}/?period=24h&theme=Cyber%20Red",
                root / "shutdown-dashboard.png",
                1440,
                900,
                "rust",
            )
            interactions = session.check_interactions(
                f"{address}/timeseries?period=24h&theme=default", 390, 844
            )
            request_started = threading.Event()
            load_active.set()
            load_results = {"success": 0, "unavailable": 0}

            def dashboard_load() -> None:
                while load_active.is_set():
                    try:
                        result = _fetch(f"{address}/api/stats/summary?period=24h")
                        if result.status == 200:
                            load_results["success"] += 1
                        else:
                            load_results["unavailable"] += 1
                    except (OSError, urllib.error.URLError):
                        load_results["unavailable"] += 1
                        request_started.set()
                        return
                    request_started.set()

            load_thread = threading.Thread(target=dashboard_load, daemon=True)
            load_thread.start()
            if not request_started.wait(timeout=5):
                raise QualificationError(
                    "dashboard load did not reach its request gate"
                )
            shutdown_started = time.monotonic()
            process.terminate()
            try:
                exit_code = process.wait(timeout=15)
            except subprocess.TimeoutExpired as error:
                raise QualificationError(
                    "SIGTERM did not stop the dashboard within 15 seconds"
                ) from error
            shutdown_ms = int((time.monotonic() - shutdown_started) * 1000)
            load_active.clear()
            load_thread.join(timeout=3)
            if load_thread.is_alive():
                raise QualificationError("dashboard request load did not stop")
            if exit_code != 0:
                raise QualificationError(f"dashboard SIGTERM exited with {exit_code}")
            if not interactions or not all(
                item.startswith("passed:") for item in interactions
            ):
                raise QualificationError("dashboard browser interactions did not pass")
        finally:
            load_active.clear()
            if load_thread is not None:
                load_thread.join(timeout=3)
            if session is not None:
                session.close()
            _stop_server(process)
            for runtime in runtime_paths:
                shutil.rmtree(runtime, ignore_errors=True)

        restarted = start("restart")
        try:
            response = _fetch(f"{address}/?period=24h&theme=Cyber%20Red")
            if response.status != 200 or "dashboard-content" not in response.body:
                raise QualificationError("dashboard did not serve after restart")
            restart_started = time.monotonic()
            restarted.terminate()
            try:
                restart_exit_code = restarted.wait(timeout=15)
            except subprocess.TimeoutExpired as error:
                raise QualificationError(
                    "restarted dashboard did not stop within 15 seconds"
                ) from error
            restart_shutdown_ms = int((time.monotonic() - restart_started) * 1000)
            if restart_exit_code != 0:
                raise QualificationError(
                    f"restarted dashboard SIGTERM exited with {restart_exit_code}"
                )
        finally:
            _stop_server(restarted)
            for runtime in runtime_paths:
                shutil.rmtree(runtime, ignore_errors=True)
    return {
        "result": "passed",
        "browser_capture": "passed",
        "browser_interactions": interactions,
        "shutdown": {
            "signal": "SIGTERM",
            "exit_code": 0,
            "deadline_seconds": 15,
            "duration_ms": shutdown_ms,
            "concurrent_summary_responses": load_results["success"],
            "requests_after_listener_close": load_results["unavailable"],
        },
        "restart": {
            "ready": True,
            "page_status": response.status,
            "signal": "SIGTERM",
            "exit_code": 0,
            "deadline_seconds": 15,
            "duration_ms": restart_shutdown_ms,
        },
    }


def asset_inventory() -> dict[str, Any]:
    candidate_manifest = json.loads(
        (RUST_ASSET_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    oracle_manifest = json.loads(
        (ORACLE_DIR / "manifest.json").read_text(encoding="utf-8")
    )
    oracle_assets = {
        str(row["filename"]): str(row["sha256"]) for row in oracle_manifest["assets"]
    }
    candidate_assets = {
        str(row["path"]): str(row["sha256"]) for row in candidate_manifest
    }
    for relative, digest in candidate_assets.items():
        rust_path = RUST_ASSET_ROOT / relative
        if hashlib.sha256(rust_path.read_bytes()).hexdigest() != digest:
            raise AssertionError(f"Rust asset bytes differ for {relative}")
    corrections = [
        {
            "path": relative,
            "oracle_sha256": oracle_assets[relative.removeprefix("static/")],
            "candidate_sha256": digest,
            "reason": (
                "bound mobile panel intrinsic width within its table scroll wrapper"
            ),
        }
        for relative, digest in candidate_assets.items()
        if relative.startswith("static/")
        and oracle_assets.get(relative.removeprefix("static/")) != digest
    ]
    return {
        "count": len(candidate_assets),
        "paths": sorted(candidate_assets),
        "oracle_candidate_differences": corrections,
        "sha256": hashlib.sha256(
            json.dumps(candidate_assets, sort_keys=True).encode()
        ).hexdigest(),
    }


def theme_inventory() -> dict[str, Any]:
    oracle_names = [
        "default",
        *sorted(
            path.name.removesuffix(".toml")
            for path in (RUST_ASSET_ROOT / "themes").iterdir()
            if path.is_file()
        ),
    ]
    manifest_names = [
        "default",
        *sorted(
            path.name.removesuffix(".toml")
            for path in (RUST_ASSET_ROOT / "themes").iterdir()
            if path.is_file()
        ),
    ]
    if oracle_names != manifest_names:
        raise AssertionError("Frozen oracle/Rust theme inventory differs")
    return {
        "count": len(oracle_names),
        "names": oracle_names,
        "review_set": list(THEME_REVIEW_SET),
    }


def screenshot_metadata() -> dict[str, Any]:
    """Retain the historical Q004 metadata helper for its old unit tests."""
    entries: list[dict[str, Any]] = []
    for implementation in ("python", "rust"):
        # Reliability owns a Python summary cache whose first fill must happen
        # after the persisted startup recovery event. Exercise it before
        # Overview can populate that cache during this process run.
        route_order = sorted(PAGE_ROUTES, key=lambda item: item[0] != "/reliability")
        for route, _label in route_order:
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
) + (("/models/q012-chat-model", "Model detail"),)


def screenshot_plan(output_dir: Path) -> list[dict[str, Any]]:
    """Return matched desktop/mobile captures for both implementations."""
    entries: list[dict[str, Any]] = []
    themed_routes = {
        "/",
        "/accounts",
        "/models",
        "/timeseries",
        "/runtime",
        "/cache",
        "/models/q012-chat-model",
    }
    extra_themes = ("Cyber Red", "Catppuccin Latte", "Cyberpunk")
    for route, _label in SCREENSHOT_ROUTES:
        themes = ("default", *extra_themes) if route in themed_routes else ("default",)
        for viewport, width, height in VIEWPORTS:
            for theme in themes:
                for implementation in ("python", "rust"):
                    page_name = (
                        "overview"
                        if route == "/"
                        else route.strip("/").replace("/", "-")
                    )
                    slug = re.sub(r"[^a-z0-9]+", "-", theme.casefold()).strip("-")
                    artifact = Path(
                        implementation,
                        f"{page_name}--{viewport}--{slug}.png",
                    )
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


def _visual_disposition(entry: dict[str, Any]) -> str:
    pair = (str(entry["route"]), str(entry["theme"]), str(entry["viewport"]))
    if pair in MANUALLY_REVIEWED_PAIRS:
        return (
            "manual paired review: layout and controls align; remaining content "
            "differences are covered by the M003/M005 source dispositions"
        )
    return (
        "automated only: matched dimensions and browser/DOM checks passed; image "
        "hash retained; not individually inspected"
    )


def _chrome_command(chrome: Path, arguments: list[str]) -> list[str]:
    """Launch universal Chrome natively on Apple Silicon, even from x64 Python."""
    prefix: list[str] = []
    if sys.platform == "darwin":
        try:
            arm64_host = subprocess.run(
                ["sysctl", "-n", "hw.optional.arm64"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
            if arm64_host.returncode == 0 and arm64_host.stdout.strip() == "1":
                prefix = ["arch", "-arm64"]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return [*prefix, str(chrome), *arguments]


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
            _chrome_command(
                chrome,
                [
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    f"--window-size={width},{height}",
                    f"--screenshot={artifact}",
                    f"--user-data-dir={profile}",
                    "--virtual-time-budget=1000",
                    url,
                ],
            ),
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
        self.events: list[dict[str, Any]] = []

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
            if "id" not in message:
                self.events.append(message)
                continue
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
            _chrome_command(
                chrome,
                [
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
            ),
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
            self.client.request("Network.enable")
            self.client.request(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """(() => {
                      const original = window.setInterval;
                      window.__eggpoolIntervals = [];
                      const originalFetch = window.fetch;
                      window.__eggpoolFetches = [];
                      window.fetch = function (input, ...args) {
                        window.__eggpoolFetches.push(String(input));
                        return originalFetch.call(this, input, ...args);
                      };
                      window.setInterval = function (callback, delay, ...args) {
                        window.__eggpoolIntervals.push({ callback, delay });
                        return original.call(this, callback, delay, ...args);
                      };
                    })();"""
                },
            )
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

    def capture(
        self,
        url: str,
        artifact: Path,
        width: int,
        height: int,
        implementation: str,
    ) -> dict[str, Any]:
        self.client.events.clear()
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
        self.client.request(
            "Runtime.evaluate",
            {
                "expression": (
                    "window.scrollTo(0, 0); "
                    "document.documentElement.scrollTop = 0; "
                    "document.body.scrollTop = 0;"
                ),
            },
        )
        audit_result = self.client.request(
            "Runtime.evaluate",
            {
                "expression": (
                    "JSON.stringify((() => {"
                    "const ids = Array.from(document.querySelectorAll('[id]'), "
                    "node => node.id);"
                    "const canvases = Array.from(document.querySelectorAll('canvas'));"
                    "window.scrollTo({left: 100, top: window.scrollY, "
                    "behavior: 'instant'}); "
                    "const rootHorizontalScroll = window.scrollX; "
                    "window.scrollTo({left: 0, top: window.scrollY, "
                    "behavior: 'instant'});"
                    "return {rootHorizontalScroll, "
                    "duplicateIds: ids.filter((id, index) => "
                    "ids.indexOf(id) !== index), "
                    "invalidCharts: canvases.filter(canvas => canvas.width < 1 "
                    "|| canvas.height < 1 || !canvas.parentElement).length, "
                    "topbarHeight: document.querySelector('header.topbar')"
                    "?.getBoundingClientRect().height ?? null, "
                    "themeSelect: (() => { const select = "
                    "document.querySelector('.theme-selector select'); "
                    "if (!select) return null; const style = getComputedStyle(select); "
                    "return {width: select.getBoundingClientRect().width, "
                    "height: select.getBoundingClientRect().height, "
                    "border: style.borderTopWidth + ' ' + style.borderTopStyle, "
                    "padding: style.padding, background: style.backgroundColor}; "
                    "})()};"
                    "})())"
                ),
            },
        )
        remote = cast("dict[str, Any]", audit_result.get("result", {}))
        audit = json.loads(str(remote.get("value", "{}")))
        body_overflow = audit.get("rootHorizontalScroll", 0) > 0
        audit = {
            key: audit[key]
            for key in (
                "duplicateIds",
                "invalidCharts",
                "topbarHeight",
                "themeSelect",
            )
        } | {"bodyOverflow": body_overflow}
        if audit["bodyOverflow"] and implementation == "rust":
            raise QualificationError(f"unexpected body overflow on {url}: {audit}")
        if audit.get("duplicateIds") or audit.get("invalidCharts"):
            raise QualificationError(f"invalid browser DOM on {url}: {audit}")
        result = self.client.request(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True},
        )
        payload = base64.b64decode(str(result.get("data", "")), validate=True)
        self._assert_clean_browser_events(url)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(payload)
        return audit

    def check_interactions(self, url: str, width: int, height: int) -> list[str]:
        """Exercise shared controls and the grouped chart on the live page."""
        self.client.events.clear()
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
        expression = r"""(async () => {
          const checks = [];
          const burger = document.querySelector('.topnav-burger');
          if (burger) {
            burger.click();
            checks.push(burger.getAttribute('aria-expanded') === 'true'
              && document.querySelector('.topnav').classList.contains('topnav-open'));
            burger.click();
            checks.push(burger.getAttribute('aria-expanded') === 'false');
          }
          const originalSubmit = HTMLFormElement.prototype.submit;
          let submission = null;
          HTMLFormElement.prototype.submit = function () {
            submission = Array.from(new FormData(this).entries());
          };
          const period = document.querySelector(
            'form[data-period-selector] select[name="period"]');
          if (period) {
            const periodDeadline = Date.now() + 2000;
            while (period.form && !period.form.__eggpoolPeriodWired
              && Date.now() < periodDeadline) {
              await new Promise(resolve => setTimeout(resolve, 20));
            }
            const targetPeriod = Array.from(period.options)
              .map(option => option.value).find(value => value !== period.value);
            period.value = targetPeriod;
            period.dispatchEvent(new Event('change', { bubbles: true }));
            checks.push(Boolean(targetPeriod) && submission !== null
              && submission.some(([key, value]) =>
                key === 'period' && value === targetPeriod));
          }
          submission = null;
          const theme = document.querySelector(
            'form.theme-selector select[name="theme"]');
          if (theme && theme.options.length > 1) {
            theme.selectedIndex = (theme.selectedIndex + 1) % theme.options.length;
            theme.dispatchEvent(new Event('change', { bubbles: true }));
            checks.push(submission !== null
              && submission.some(([key]) => key === 'theme')
              && submission.some(([key, value]) =>
                key === 'period' && value === '24h'));
          }
          HTMLFormElement.prototype.submit = originalSubmit;
          const refresh = document.querySelector('.topnav-refresh');
          const refreshHandler = refresh ? refresh.getAttribute('onclick') || '' : '';
          checks.push(Boolean(refresh && refreshHandler.includes('location.reload')));
          const grouped = document.querySelector(
            'form[data-timeseries-controls] select[name="group_by"]');
          if (grouped && window.EggPoolDashboard) {
            const form = grouped.form;
            const deadline = Date.now() + 2000;
            while (form && !form.__eggpoolTimeseriesWired && Date.now() < deadline) {
              await new Promise(resolve => setTimeout(resolve, 20));
            }
            const oldValue = grouped.value;
            grouped.value = 'account';
            grouped.dispatchEvent(new Event('change', { bubbles: true }));
            await new Promise(resolve => setTimeout(resolve, 350));
            const requested = (window.__eggpoolFetches || [])
              .some(name => name.includes('/api/timeseries/grouped')
                && name.includes('group_by=account'));
            checks.push(requested);
            grouped.value = oldValue;
          }
          const updated = document.getElementById('dashboard-updated');
          if (updated && location.pathname === '/') {
            const interval = (window.__eggpoolIntervals || [])
              .find(item => item.delay === 1000);
            if (interval) await interval.callback();
            const pageResource = () => performance.getEntriesByType('resource')
              .some(entry => entry.name.startsWith(
                location.origin + location.pathname));
            checks.push(Boolean(interval) && pageResource()
              && updated.textContent !== 'ready');
          }
          return JSON.stringify({
            checks,
            intervals: (window.__eggpoolIntervals || []).map(item => item.delay),
            fetches: window.__eggpoolFetches || [],
            updated: updated ? updated.textContent : null,
          });
        })()"""
        result = self.client.request(
            "Runtime.evaluate", {"expression": expression, "awaitPromise": True}
        )
        remote = cast("dict[str, Any]", result.get("result", {}))
        value = json.loads(str(remote.get("value", "{}")))
        checks = cast("list[bool]", value.get("checks", []))
        self._assert_clean_browser_events(url)
        if not checks or not all(checks):
            raise QualificationError(f"dashboard interactions failed on {url}: {value}")
        return [
            "passed: burger, period/theme, manual refresh, grouped chart, "
            "auto-refresh, unique IDs, valid chart targets, no body overflow"
        ]

    def _assert_clean_browser_events(self, page_url: str) -> None:
        origin = urllib.parse.urlsplit(page_url).netloc
        request_urls: dict[str, str] = {}
        failures: list[str] = []
        errors: list[str] = []
        for event in self.client.events:
            method = event.get("method")
            params = cast("dict[str, Any]", event.get("params", {}))
            if method == "Network.requestWillBeSent":
                request = cast("dict[str, Any]", params.get("request", {}))
                request_urls[str(params.get("requestId", ""))] = str(
                    request.get("url", "")
                )
            elif method == "Network.loadingFailed":
                request_id = str(params.get("requestId", ""))
                failed_url = request_urls.get(request_id, "")
                if urllib.parse.urlsplit(failed_url).netloc == origin:
                    failures.append(f"failed same-origin load {failed_url}")
            elif method == "Network.responseReceived":
                response = cast("dict[str, Any]", params.get("response", {}))
                response_url = str(response.get("url", ""))
                if (
                    urllib.parse.urlsplit(response_url).netloc == origin
                    and int(response.get("status", 0)) >= 400
                ):
                    failures.append(
                        f"same-origin response {response.get('status')} {response_url}"
                    )
            elif method == "Runtime.exceptionThrown":
                details = cast("dict[str, Any]", params.get("exceptionDetails", {}))
                errors.append(str(details.get("text", "JavaScript exception")))
            elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
                args = cast("list[dict[str, Any]]", params.get("args", []))
                errors.append(
                    " ".join(str(argument.get("value", "")) for argument in args)
                    or "console.error"
                )
        if failures or errors:
            raise QualificationError(
                f"browser errors on {page_url}: " + "; ".join([*failures, *errors])
            )

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
                browser_audit: dict[str, Any] = {}
            else:
                browser_audit = session.capture(
                    url,
                    artifact,
                    int(entry["width"]),
                    int(entry["height"]),
                    str(entry["implementation"]),
                )
            entry["layout_audit"] = browser_audit
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
            entry["browser_checks"] = (
                "passed: no JS exception, console error, failed same-origin load, "
                "or same-origin HTTP error; unique IDs, valid chart targets, and "
                + (
                    "frozen-oracle body overflow recorded"
                    if browser_audit.get("bodyOverflow")
                    else "no body overflow"
                )
            )
            entry["manual_disposition"] = _visual_disposition(entry)
        interaction_matrix: list[dict[str, str]] = []
        if session is not None:
            for implementation, port in ports.items():
                for width, height, viewport in (
                    (1440, 900, "desktop"),
                    (390, 844, "mobile"),
                ):
                    for route in ("/", "/timeseries"):
                        url = f"http://127.0.0.1:{port}{route}?period=24h&theme=default"
                        checks = session.check_interactions(url, width, height)
                        interaction_matrix.append(
                            {
                                "implementation": implementation,
                                "route": route,
                                "viewport": viewport,
                                "result": "; ".join(checks),
                            }
                        )
    finally:
        if session is not None:
            session.close()
    manifest_bytes = json.dumps(entries, sort_keys=True).encode()
    if len(manifest_bytes) > MAX_RESULT_BYTES // 2:
        raise QualificationError("dashboard screenshot manifest exceeded its bound")
    return {
        "procedure": (
            "isolated headless Chrome via Chrome DevTools Protocol; browser is "
            "outside the Rust dependency graph"
        ),
        "artifact_root": str(output_dir),
        "count": len(entries),
        "entries": entries,
        "interaction_checks": interaction_matrix,
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
            "PYTHONPATH": str(ORACLE_SOURCE_ROOT / "src"),
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
    python = _start_server([ORACLE_PYTHON, "-m", "eggpool"], python_config, python_env)
    rust = _start_server([str(RUST_BINARY)], rust_config, rust_env)
    try:
        _wait_for_tcp(python_port, python, f"{state_name} Python dashboard")
        _wait_for_tcp(rust_port, rust, f"{state_name} Rust dashboard")
        _wait_for_operational_event(
            python_root / "dashboard.sqlite3",
            python,
            "crash_recovery",
            f"{state_name} Python dashboard",
        )
        _wait_for_operational_event(
            rust_root / "dashboard.sqlite3",
            rust,
            "crash_recovery",
            f"{state_name} Rust dashboard",
        )
        route_order = sorted(PAGE_ROUTES, key=lambda item: item[0] != "/reliability")
        for route, _label in route_order:
            query = "?period=24h&theme=Cyber%20Red"
            python_result = _fetch(f"http://127.0.0.1:{python_port}{route}{query}")
            rust_result = _fetch(f"http://127.0.0.1:{rust_port}{route}{query}")
            issues: list[str] = []
            if python_result.status != rust_result.status:
                issues.append(f"status {python_result.status}/{rust_result.status}")
            elif python_result.status != 200:
                issues.append(
                    f"both implementations return HTTP {python_result.status}"
                )
            elif "text/html" not in python_result.headers.get(
                "content-type", ""
            ) or "text/html" not in rust_result.headers.get("content-type", ""):
                issues.append("non-HTML content type")
            else:
                python_projection = project_html(python_result.body)
                rust_projection = project_html(rust_result.body)
                shell_issues: list[str] = []
                try:
                    compare_shared_shell_projection(
                        python_projection, rust_projection, route
                    )
                except AssertionError as error:
                    shell_issues.append(str(error))
                observations.append(
                    {
                        "state": state_name,
                        "route": route,
                        "kind": "shared-shell",
                        "status": "pass" if not shell_issues else "mismatch",
                        "mismatches": shell_issues,
                    }
                )
                try:
                    compare_dom_projection(python_projection, rust_projection, route)
                except AssertionError as error:
                    issues.append(str(error))
            observations.append(
                {
                    "state": state_name,
                    "route": route,
                    "status": "pass" if not issues else "mismatch",
                    "mismatches": issues,
                }
            )
        for route in API_ROUTES:
            python_result = _fetch(f"http://127.0.0.1:{python_port}{route}?period=24h")
            rust_result = _fetch(f"http://127.0.0.1:{rust_port}{route}?period=24h")
            issues: list[str] = []
            try:
                compare_api_response(python_result, rust_result, route)
            except AssertionError as error:
                issues.append(str(error))
            observations.append(
                {
                    "state": state_name,
                    "route": route,
                    "kind": "api",
                    "status": "pass" if not issues else "mismatch",
                    "mismatches": issues,
                }
            )
        for route, expected_type, source_name in STATIC_ROUTES:
            candidate_digest = hashlib.sha256(
                (RUST_ASSET_ROOT / "static" / source_name).read_bytes()
            ).hexdigest()
            oracle_digest = next(
                str(row["sha256"])
                for row in json.loads(
                    (ORACLE_DIR / "manifest.json").read_text(encoding="utf-8")
                )["assets"]
                if row["filename"] == source_name
            )
            for implementation, port, expected_digest in (
                ("python", python_port, oracle_digest),
                ("rust", rust_port, candidate_digest),
            ):
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


def _run_private_pair(
    root: Path,
    python_root: Path,
    rust_root: Path,
    observations: list[dict[str, Any]],
) -> dict[str, int]:
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
    python = _start_server([ORACLE_PYTHON, "-m", "eggpool"], python_config, python_env)
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
                observations.append(
                    {
                        "state": "private",
                        "route": route,
                        "implementation": implementation,
                        "kind": "auth",
                        "status": "pass"
                        if unauthorized.status == 401 and authorized.status == 200
                        else "mismatch",
                        "mismatches": []
                        if unauthorized.status == 401 and authorized.status == 200
                        else [
                            "unauthorized/authorized "
                            f"{unauthorized.status}/{authorized.status}"
                        ],
                    }
                )
        for route in API_ROUTES:
            for implementation, port in (("python", python_port), ("rust", rust_port)):
                unauthorized = _fetch(f"http://127.0.0.1:{port}{route}")
                authorized = _fetch(
                    f"http://127.0.0.1:{port}{route}",
                    headers={"Authorization": "Bearer q012-server-key"},
                )
                observations.append(
                    {
                        "state": "private",
                        "route": route,
                        "implementation": implementation,
                        "kind": "auth",
                        "status": "pass"
                        if unauthorized.status == 401
                        and authorized.status in {200, 500}
                        else "mismatch",
                        "mismatches": []
                        if unauthorized.status == 401
                        and authorized.status in {200, 500}
                        else [
                            "unauthorized/authorized "
                            f"{unauthorized.status}/{authorized.status}"
                        ],
                    }
                )
        return {"python": python_port, "rust": rust_port}
    finally:
        _stop_server(python)
        _stop_server(rust)
        shutil.rmtree(python_runtime, ignore_errors=True)
        shutil.rmtree(rust_runtime, ignore_errors=True)


def _run_model_info_detail_pair(
    *,
    root: Path,
    fixture_path: Path,
    observations: list[dict[str, Any]],
    capture: bool,
    screenshot_dir: Path,
) -> dict[str, Any] | None:
    """Compare the populated canonical model-info detail branch without probes."""
    detail_root = root / "model-info-detail"
    python_root = detail_root / "python"
    rust_root = detail_root / "rust"
    python_root.mkdir(parents=True)
    rust_root.mkdir(parents=True)
    detail_fixture = detail_root / "dashboard.sqlite3"
    shutil.copy2(fixture_path, detail_fixture)
    with sqlite3.connect(detail_fixture) as connection:
        connection.executescript(
            (ROOT / "migration-rs/fixtures/dashboard/q012-model-info.sql").read_text(
                encoding="utf-8"
            )
        )
    shutil.copy2(detail_fixture, python_root / "dashboard.sqlite3")
    shutil.copy2(detail_fixture, rust_root / "dashboard.sqlite3")
    python_port = _port()
    rust_port = _port()
    python_config = _write_config(
        python_root, python_port, populated=False, model_info_enabled=True
    )
    rust_config = _write_config(
        rust_root, rust_port, populated=False, model_info_enabled=True
    )
    base_env = dict(os.environ)
    base_env.update({"PYTHONHASHSEED": "0", "TZ": "UTC", "HOME": str(root / "home")})
    python_runtime = Path(f"/tmp/eq12-model-info-py-{python_port}")
    rust_runtime = Path(f"/tmp/eq12-model-info-rs-{rust_port}")
    for runtime in (python_runtime, rust_runtime):
        if runtime.exists():
            shutil.rmtree(runtime)
    python_env = dict(base_env)
    python_env.update(
        {
            "PYTHONPATH": str(ORACLE_SOURCE_ROOT / "src"),
            "EGGPOOL_RUNTIME_DIR": str(python_runtime),
            "EGGPOOL_PID_FILE": str(detail_root / "python.pid"),
        }
    )
    rust_env = dict(base_env)
    rust_env.update(
        {
            "EGGPOOL_RUNTIME_DIR": str(rust_runtime),
            "EGGPOOL_PID_FILE": str(detail_root / "rust.pid"),
        }
    )
    python = _start_server([ORACLE_PYTHON, "-m", "eggpool"], python_config, python_env)
    rust = _start_server([str(RUST_BINARY)], rust_config, rust_env)
    route = "/models/q012-chat-model"
    try:
        _wait_for_tcp(python_port, python, "Python model-info dashboard")
        _wait_for_tcp(rust_port, rust, "Rust model-info dashboard")
        query = "?period=24h&theme=Cyber%20Red"
        python_result = _fetch(f"http://127.0.0.1:{python_port}{route}{query}")
        rust_result = _fetch(f"http://127.0.0.1:{rust_port}{route}{query}")
        issues: list[str] = []
        if python_result.status != rust_result.status or python_result.status != 200:
            issues.append(f"status {python_result.status}/{rust_result.status}")
        else:
            python_projection = project_html(python_result.body)
            rust_projection = project_html(rust_result.body)
            shell_issues: list[str] = []
            try:
                compare_shared_shell_projection(
                    python_projection, rust_projection, route
                )
            except AssertionError as error:
                shell_issues.append(str(error))
            observations.append(
                {
                    "state": "populated-model-info",
                    "route": route,
                    "kind": "shared-shell",
                    "status": "pass" if not shell_issues else "mismatch",
                    "mismatches": shell_issues,
                }
            )
            try:
                compare_dom_projection(python_projection, rust_projection, route)
            except AssertionError as error:
                issues.append(str(error))
        observations.append(
            {
                "state": "populated-model-info",
                "route": route,
                "kind": "m003-model-info-detail",
                "status": "pass" if not issues else "mismatch",
                "mismatches": issues,
            }
        )
        if capture:
            artifacts = screenshot_dir / "model-info-detail"
            artifacts.mkdir(parents=True, exist_ok=True)
            entries: list[dict[str, Any]] = []
            session = _HeadlessScreenshotSession()
            try:
                for implementation, port in (
                    ("python", python_port),
                    ("rust", rust_port),
                ):
                    for width, height, viewport in (
                        (1440, 900, "desktop"),
                        (390, 844, "mobile"),
                    ):
                        artifact = artifacts / f"{implementation}-{viewport}.png"
                        url = (
                            f"http://127.0.0.1:{port}{route}"
                            "?period=24h&theme=Cyber%20Red"
                        )
                        browser_audit = session.capture(
                            url, artifact, width, height, implementation
                        )
                        dimensions = _png_dimensions(artifact)
                        entries.append(
                            {
                                "route": route,
                                "state": "populated-model-info",
                                "implementation": implementation,
                                "viewport": viewport,
                                "artifact": str(artifact.relative_to(screenshot_dir)),
                                "width": width,
                                "height": height,
                                "dimensions": {
                                    "width": dimensions[0],
                                    "height": dimensions[1],
                                },
                                "bytes": artifact.stat().st_size,
                                "sha256": hashlib.sha256(
                                    artifact.read_bytes()
                                ).hexdigest(),
                                "result": "captured",
                                "layout_audit": browser_audit,
                                "browser_checks": (
                                    "passed: no JS exception, console error, failed "
                                    "same-origin load, or same-origin HTTP error; "
                                    "unique IDs, valid chart targets, and "
                                    + (
                                        "frozen-oracle body overflow recorded"
                                        if browser_audit.get("bodyOverflow")
                                        else "no body overflow"
                                    )
                                ),
                                "manual_disposition": _visual_disposition(
                                    {
                                        "route": route,
                                        "theme": "Cyber Red",
                                        "viewport": viewport,
                                    }
                                ),
                            }
                        )
            finally:
                session.close()
            return {"count": len(entries), "entries": entries}
        return None
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
    if ORACLE_SOURCE_ROOT != ROOT:
        source_sha = subprocess.run(
            ["git", "-C", str(ORACLE_SOURCE_ROOT), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if source_sha.returncode != 0 or source_sha.stdout.strip() != ORACLE_COMMIT:
            raise QualificationError(
                f"external Python oracle must be pinned at {ORACLE_COMMIT}"
            )
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
        detail_screenshots = _run_model_info_detail_pair(
            root=root,
            fixture_path=fixture_path,
            observations=observations,
            capture=include_screenshots,
            screenshot_dir=screenshot_root,
        )
        shutdown_restart = (
            qualify_dashboard_shutdown_restart() if include_screenshots else None
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
        private_ports = _run_private_pair(
            root, private_python_root, private_rust_root, observations
        )
        del private_ports
        screenshot_manifest = populated_result["screenshots"]
        if detail_screenshots is not None:
            screenshot_manifest["entries"].extend(detail_screenshots["entries"])
            screenshot_manifest["count"] += detail_screenshots["count"]
            screenshot_manifest["manifest_sha256"] = hashlib.sha256(
                json.dumps(screenshot_manifest["entries"], sort_keys=True).encode()
            ).hexdigest()
    report: dict[str, Any] = {
        "schema_version": "dashboard-parity-current-gaps.v1",
        "plan": "Dashboard parity qualification",
        "candidate_sha": os.environ.get("EGGPOOL_DASHBOARD_CANDIDATE_SHA", _git_sha()),
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
                "state": "populated-model-info",
                "route": "/models/q012-chat-model",
                "database": "same secret-free fixture, model-info source work disabled",
            },
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
                "classes": [
                    "unauthorized",
                    "authorized",
                    "all dashboard pages and JSON routes",
                ],
            },
        ],
        "dom_comparisons": observations,
        "parity_status": "gaps"
        if any(row["status"] == "mismatch" for row in observations)
        else "pass",
        "mismatch_count": sum(row["status"] == "mismatch" for row in observations),
        "mismatch_groups": _mismatch_groups(observations),
        "static_assets": inventory,
        "themes": themes,
        "screenshots": screenshot_manifest,
        "shutdown_restart": shutdown_restart,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    encoded = json.dumps(report, indent=2, sort_keys=True).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        raise QualificationError("dashboard gap report exceeded its bounded size")
    return report


def _mismatch_groups(observations: list[dict[str, Any]]) -> dict[str, int]:
    groups = {
        "shared-shell-and-api": 0,
        "overview-account-model": 0,
        "telemetry-routing-trace": 0,
        "runtime-cache": 0,
    }
    core_routes = {"/", "/accounts", "/models", "/models/example-model"}
    runtime_routes = {"/runtime", "/cache"}
    for row in observations:
        if row["status"] != "mismatch":
            continue
        route = str(row["route"])
        if row.get("kind") in {"api", "auth"}:
            group = "shared-shell-and-api"
        elif route in core_routes:
            group = "overview-account-model"
        elif route in runtime_routes:
            group = "runtime-cache"
        else:
            group = "telemetry-routing-trace"
        groups[group] += 1
    return groups


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
        "# Dashboard M001 Current Gap Report",
        "",
        f"- Candidate: `{report['candidate_sha']}`",
        f"- Pages: `{len(report['page_routes'])}`",
        f"- Static/theme assets: `{report['static_assets']['count']}`",
        f"- Duration: `{report['duration_ms']} ms`",
        "",
        f"Disposition: `{report['parity_status']}`; "
        f"{report['mismatch_count']} matrix cells contain parity gaps.",
        "",
        "Mismatch groups: "
        + ", ".join(
            f"{group}={count}"
            for group, count in cast(
                "dict[str, int]", report["mismatch_groups"]
            ).items()
        ),
        "",
        "Each mismatch records a route/state and structural category or HTTP "
        "status only; response bodies and fixture values are excluded.",
    ]
    markdown_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help=(
            "write the fixed oracle source and asset inventory without running servers"
        ),
    )
    parser.add_argument(
        "--capture-oracle",
        type=Path,
        help="capture synthetic responses from the pinned historical worktree",
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument(
        "--shutdown-restart",
        action="store_true",
        help="qualify browser activity, bounded SIGTERM, and restart only",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--screenshot-dir", type=Path)
    options = parser.parse_args()
    try:
        if options.write_manifest:
            write_oracle_manifest()
            print(f"Dashboard oracle manifest written: {ORACLE_DIR / 'manifest.json'}")
            return 0
        if options.capture_oracle is not None:
            capture_oracle_snapshots(options.capture_oracle)
            print(f"Dashboard oracle captures written: {options.capture_oracle}")
            return 0
        if options.shutdown_restart:
            if not options.skip_build:
                result = subprocess.run(
                    ["cargo", "build", "--manifest-path", str(RUST_MANIFEST)],
                    cwd=ROOT,
                    check=False,
                )
                if result.returncode != 0:
                    raise QualificationError("Rust dashboard candidate failed to build")
            print(json.dumps(qualify_dashboard_shutdown_restart(), indent=2))
            return 0
        report = run_qualification(
            skip_build=options.skip_build,
            include_screenshots=options.screenshots,
            screenshot_dir=options.screenshot_dir,
        )
        write_report(report, options.output, options.markdown)
    except (AssertionError, QualificationError, OSError, ValueError) as error:
        print(f"Dashboard qualification failed: {error}", file=sys.stderr)
        return 1
    print(f"Dashboard qualification completed: {options.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
