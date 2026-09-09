"""Run the deterministic M10 Q004 dashboard qualification.

The runner deliberately keeps browser work outside the Rust dependency graph.
Its normal run compares isolated Python and Rust HTTP servers, while
``--screenshots`` emits a deterministic route/theme/viewport manifest for the
local browser review procedure documented in ``docs/rust-dashboard-qualification.md``.

Usage::

    uv run python scripts/qualification_dashboard.py --skip-build
    uv run python scripts/qualification_dashboard.py --skip-build --screenshots
"""

from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import os
import re
import shutil
import socket
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
DEFAULT_JSON = ROOT / "migration-rs" / "closure" / "qualification" / "004-run.json"
DEFAULT_MARKDOWN = ROOT / "migration-rs" / "closure" / "qualification" / "004-run.md"
MAX_RESULT_BYTES = 256 * 1024

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


class QualificationError(RuntimeError):
    """Raised when a mandatory Q004 contract fails."""


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
        self._title_depth = 0
        self._heading_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if tag == "title":
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
        if tag in {"h1", "h2", "h3"}:
            self.headings.append(_collapse_text(" ".join(self.heading_parts)))
            self.heading_parts.clear()
            self._heading_depth = 0
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
    )


def compare_dom_projection(
    expected: HtmlProjection, actual: HtmlProjection, route: str
) -> None:
    """Compare the stable page contract without discarding semantic content."""
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
    if set(expected.nav_paths) != set(actual.nav_paths):
        mismatches.append(f"navigation: {expected.nav_paths!r} != {actual.nav_paths!r}")
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
    if expected.duplicate_ids or actual.duplicate_ids:
        mismatches.append(
            f"duplicate ids: {expected.duplicate_ids!r}/{actual.duplicate_ids!r}"
        )
    if expected.unsafe_links or actual.unsafe_links:
        mismatches.append(
            f"unsafe links: {expected.unsafe_links!r}/{actual.unsafe_links!r}"
        )
    if mismatches:
        raise AssertionError(f"Q004 DOM mismatch for {route}: {'; '.join(mismatches)}")


def compare_required_page_contract(
    projection: HtmlProjection, route: str, label: str
) -> None:
    """Check the Rust page has the expected page-specific semantic anchors."""
    if projection.title != label and route != "/models/example-model":
        raise AssertionError(f"{route}: unexpected title {projection.title!r}")
    if not any(
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


def _write_config(root: Path, port: int, *, public: bool = True) -> Path:
    database = root / "dashboard.sqlite3"
    path = root / "dashboard.toml"
    path.write_text(
        f"[server]\n"
        f'host = "127.0.0.1"\nport = {port}\n'
        f'api_key = "q004-server-key"\n\n[database]\n'
        f'path = "{database}"\n\n[dashboard]\nenabled = true\n'
        f'public = {str(public).lower()}\ntheme = "Cyber Red"\n\n[models]\n'
        "startup_refresh = false\n\n[model_info]\nenabled = false\n"
        "startup_refresh = false\n",
        encoding="utf-8",
    )
    return path


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


def run_qualification(*, skip_build: bool, include_screenshots: bool) -> dict[str, Any]:
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
    with tempfile.TemporaryDirectory(prefix="eggpool-q004-") as temporary:
        root = Path(temporary)
        base_env = dict(os.environ)
        base_env.update(
            {"PYTHONHASHSEED": "0", "TZ": "UTC", "HOME": str(root / "home")}
        )
        (root / "home").mkdir()
        python_port = _port()
        rust_port = _port()
        python_root = root / "python"
        rust_root = root / "rust"
        python_root.mkdir()
        rust_root.mkdir()
        python_config = _write_config(python_root, python_port)
        rust_config = _write_config(rust_root, rust_port)
        python_env = dict(base_env)
        python_env.update(
            {
                "PYTHONPATH": str(ROOT / "src"),
                "EGGPOOL_RUNTIME_DIR": str(root / "python-runtime"),
                "EGGPOOL_PID_FILE": str(root / "python.pid"),
            }
        )
        rust_env = dict(base_env)
        rust_env["EGGPOOL_RUNTIME_DIR"] = str(root / "rust-runtime")
        rust_env["EGGPOOL_PID_FILE"] = str(root / "rust.pid")
        python = _start_server(
            [sys.executable, "-m", "eggpool"], python_config, python_env
        )
        rust = _start_server([str(RUST_BINARY)], rust_config, rust_env)
        try:
            _wait_for_tcp(python_port, python, "Python dashboard")
            _wait_for_tcp(rust_port, rust, "Rust dashboard")
            for route, label in PAGE_ROUTES:
                python_result = _fetch(
                    f"http://127.0.0.1:{python_port}{route}?period=24h&theme=Cyber%20Red"
                )
                rust_result = _fetch(
                    f"http://127.0.0.1:{rust_port}{route}?period=24h&theme=Cyber%20Red"
                )
                if python_result.status != 200 or rust_result.status != 200:
                    raise AssertionError(
                        f"{route}: status {python_result.status}/{rust_result.status}"
                    )
                if "text/html" not in python_result.headers.get(
                    "content-type", ""
                ) or "text/html" not in rust_result.headers.get("content-type", ""):
                    raise AssertionError(f"{route}: non-HTML content type")
                python_projection = project_html(python_result.body)
                rust_projection = project_html(rust_result.body)
                compare_dom_projection(python_projection, rust_projection, route)
                compare_required_page_contract(rust_projection, route, label)
                observations.append(
                    {
                        "route": route,
                        "status": "pass",
                        "title": rust_projection.title,
                        "active_nav": rust_projection.active_nav,
                    }
                )
            for implementation, port in (("python", python_port), ("rust", rust_port)):
                special = _fetch(
                    f"http://127.0.0.1:{port}/models/%3Cmodel%20%26%20%22x%22%3E"
                )
                if (
                    "&lt;model &amp; &quot;x&quot;&gt;" not in special.body
                    or "<model &" in special.body
                ):
                    raise AssertionError(
                        f"{implementation} model detail does not escape special "
                        "HTML characters"
                    )
            for route, expected_type, source_name in STATIC_ROUTES:
                python_result = _fetch(f"http://127.0.0.1:{python_port}{route}")
                rust_result = _fetch(f"http://127.0.0.1:{rust_port}{route}")
                expected_digest = hashlib.sha256(
                    (STATIC_ROOT / source_name).read_bytes()
                ).hexdigest()
                for implementation, result in (
                    ("python", python_result),
                    ("rust", rust_result),
                ):
                    if result.status != 200 or expected_type not in result.headers.get(
                        "content-type", ""
                    ):
                        raise AssertionError(
                            f"{implementation} {route}: bad status/content type"
                        )
                    digest = hashlib.sha256(result.body.encode()).hexdigest()
                    if digest != expected_digest:
                        raise AssertionError(
                            f"{implementation} {route}: asset bytes differ"
                        )
            theme_result = _fetch(
                f"http://127.0.0.1:{rust_port}/static/theme.css?theme=Cyber%20Red"
            )
            if theme_result.status != 200 or "--page-bg" not in theme_result.body:
                raise AssertionError("Rust theme selector did not return CSS variables")
            observations.append(
                {
                    "route": "static-assets",
                    "status": "pass",
                    "inventory": inventory["count"],
                }
            )
            private_root = root / "private"
            private_root.mkdir()
            private_python_root = private_root / "python"
            private_rust_root = private_root / "rust"
            private_python_root.mkdir()
            private_rust_root.mkdir()
            private_python_port = _port()
            private_rust_port = _port()
            private_python_config = _write_config(
                private_python_root, private_python_port, public=False
            )
            private_rust_config = _write_config(
                private_rust_root, private_rust_port, public=False
            )
            private_python_env = dict(python_env)
            private_python_runtime = Path(f"/tmp/eq4-py-{private_python_port}")
            private_python_env["EGGPOOL_RUNTIME_DIR"] = str(private_python_runtime)
            private_python_env["EGGPOOL_PID_FILE"] = str(
                private_python_root / "eggpool.pid"
            )
            private_rust_env = dict(rust_env)
            private_rust_runtime = Path(f"/tmp/eq4-rs-{private_rust_port}")
            private_rust_env["EGGPOOL_RUNTIME_DIR"] = str(private_rust_runtime)
            private_rust_env["EGGPOOL_PID_FILE"] = str(
                private_rust_root / "eggpool.pid"
            )
            for runtime in (private_python_runtime, private_rust_runtime):
                if runtime.exists():
                    shutil.rmtree(runtime)
            private_python = _start_server(
                [sys.executable, "-m", "eggpool"],
                private_python_config,
                private_python_env,
            )
            private_rust = _start_server(
                [str(RUST_BINARY)], private_rust_config, private_rust_env
            )
            try:
                _wait_for_tcp(
                    private_python_port, private_python, "private Python dashboard"
                )
                _wait_for_tcp(private_rust_port, private_rust, "private Rust dashboard")
                for implementation, port in (
                    ("python", private_python_port),
                    ("rust", private_rust_port),
                ):
                    unauthorized = _fetch(f"http://127.0.0.1:{port}/")
                    authorized = _fetch(
                        f"http://127.0.0.1:{port}/",
                        headers={"Authorization": "Bearer q004-server-key"},
                    )
                    if unauthorized.status != 401 or authorized.status != 200:
                        raise AssertionError(
                            f"{implementation} private/public dashboard auth mismatch"
                        )
                observations.append({"route": "private-auth", "status": "pass"})
            finally:
                _stop_server(private_python)
                _stop_server(private_rust)
                shutil.rmtree(private_python_runtime, ignore_errors=True)
                shutil.rmtree(private_rust_runtime, ignore_errors=True)
        finally:
            _stop_server(python)
            _stop_server(rust)
    report: dict[str, Any] = {
        "schema_version": "m10-q004.v1",
        "plan": "Q004",
        "candidate_sha": _git_sha(),
        "python_identity": "python:eggpool:local",
        "rust_identity": f"rust:{RUST_BINARY}",
        "environment": {
            "os": sys.platform,
            "python": sys.version.split()[0],
            "browser": "manual-local",
        },
        "page_routes": [route for route, _ in PAGE_ROUTES],
        "fixture_matrix": [
            "empty first-run database",
            "normal populated state (reserved deterministic fixture shape)",
            "multiple providers/accounts/models (reserved deterministic fixture shape)",
            "special HTML and Unicode model detail",
            "public dashboard and private unauthorized gate",
            "missing optional cost/model-info data",
        ],
        "dom_comparisons": observations,
        "static_assets": inventory,
        "themes": themes,
        "screenshots": screenshot_metadata()
        if include_screenshots
        else {"procedure": "run with --screenshots"},
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    encoded = json.dumps(report, indent=2, sort_keys=True).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        raise QualificationError("Q004 report exceeded its bounded artifact size")
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
        "# Q004 Dashboard Qualification Run",
        "",
        f"- Candidate: `{report['candidate_sha']}`",
        f"- Pages: `{len(report['page_routes'])}`",
        f"- Static/theme assets: `{report['static_assets']['count']}`",
        f"- Duration: `{report['duration_ms']} ms`",
        "",
        "All deterministic page, DOM, escaping, static-asset, and theme checks passed.",
        "",
        "Screenshot metadata is a browser-review coverage manifest; PNG captures "
        "remain external to the repository.",
    ]
    markdown_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    options = parser.parse_args()
    try:
        report = run_qualification(
            skip_build=options.skip_build, include_screenshots=options.screenshots
        )
        write_report(report, options.output, options.markdown)
    except (AssertionError, QualificationError, OSError, ValueError) as error:
        print(f"Q004 qualification failed: {error}", file=sys.stderr)
        return 1
    print(f"Q004 qualification passed: {options.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
