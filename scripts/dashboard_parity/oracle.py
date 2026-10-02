# ruff: noqa: F405
from __future__ import annotations

from ._shared import *  # noqa: F403
from .fixtures import *  # noqa: F403
from .process import *  # noqa: F403
from .projection import *  # noqa: F403


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


def _write_capture(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )


__all__ = [
    "_collect_dom_facts",
    "_route_filename",
    "_selector_branch_present",
    "_selector_producers",
    "_write_capture",
    "build_oracle_manifest",
    "capture_oracle_snapshots",
    "write_oracle_manifest",
]
