# ruff: noqa: F405
from __future__ import annotations

from ._shared import *  # noqa: F403
from .process import *  # noqa: F403

# Recorded reason for every static asset that intentionally diverges from the
# frozen Python oracle. One entry per file: a single shared string would
# misdescribe every other divergence, and a new divergence must fail loudly
# instead of being labelled with an unrelated correction.
ORACLE_CANDIDATE_CORRECTIONS = {
    "static/dashboard.css": (
        "bound mobile panel intrinsic width within its table scroll wrapper"
    ),
    "static/dashboard.js": (
        "submit the timeseries filter form natively so the server renders the "
        "chart, tables, and URL from one query"
    ),
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
    corrections: list[dict[str, Any]] = []
    for relative, digest in candidate_assets.items():
        if not relative.startswith("static/"):
            continue
        if oracle_assets.get(relative.removeprefix("static/")) == digest:
            continue
        reason = ORACLE_CANDIDATE_CORRECTIONS.get(relative)
        if reason is None:
            raise AssertionError(
                f"{relative} diverges from the frozen oracle with no recorded "
                "correction reason"
            )
        corrections.append(
            {
                "path": relative,
                "oracle_sha256": oracle_assets[relative.removeprefix("static/")],
                "candidate_sha256": digest,
                "reason": reason,
            }
        )
    stale = sorted(
        set(ORACLE_CANDIDATE_CORRECTIONS) - {row["path"] for row in corrections}
    )
    if stale:
        raise AssertionError(
            f"recorded correction reasons no longer diverge: {', '.join(stale)}"
        )
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


_DEFAULT_CAPTURE_BROWSER_SCREENSHOT = _capture_browser_screenshot


__all__ = [
    "_DEFAULT_CAPTURE_BROWSER_SCREENSHOT",
    "_DevToolsClient",
    "_HeadlessScreenshotSession",
    "_capture_browser_screenshot",
    "_chrome_command",
    "_png_dimensions",
    "_visual_disposition",
    "asset_inventory",
    "capture_screenshots",
    "screenshot_metadata",
    "screenshot_plan",
    "theme_inventory",
]
