# ruff: noqa: F405
from __future__ import annotations

from ._shared import *  # noqa: F403
from .browser import *  # noqa: F403
from .process import *  # noqa: F403


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


__all__ = [
    "_build_fixture",
    "_fixture_provider_config",
    "_start_server",
    "_stop_server",
    "_write_config",
    "qualify_dashboard_shutdown_restart",
]
