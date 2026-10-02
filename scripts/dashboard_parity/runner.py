# ruff: noqa: F405
from __future__ import annotations

from ._shared import *  # noqa: F403
from .browser import *  # noqa: F403
from .fixtures import *  # noqa: F403
from .process import *  # noqa: F403
from .projection import *  # noqa: F403
from .report import *  # noqa: F403


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
                                "theme": "Cyber Red",
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


__all__ = [
    "_run_model_info_detail_pair",
    "_run_pair",
    "_run_private_pair",
    "run_qualification",
]
