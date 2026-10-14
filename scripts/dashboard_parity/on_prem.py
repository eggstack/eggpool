# ruff: noqa: F405
"""Browser qualification for the on-prem admin and member dashboard pages.

The frozen Python oracle only ever implemented the historical M001-M012
dashboard, so it cannot serve as the reference for the on-prem user-access pages
added afterwards. This module therefore keeps the gate split:

* historical pages stay owned by the pinned Python oracle in ``runner.py``;
* on-prem pages are owned by EggPool itself and are qualified against a
  Rust-owned approved reference that this module freezes and re-compares.

Everything here reuses the existing CDP harness in ``browser.py`` and the
existing loopback fixtures in ``fixtures.py``; no second visual harness is
introduced. Only a managed on-prem instance is started here, because the
on-prem pages require a bootstrapped administrator and real member sessions
that the historical fixtures do not model.
"""

from __future__ import annotations

from ._shared import *  # noqa: F403
from .browser import _HeadlessScreenshotSession, _png_dimensions
from .fixtures import _start_server, _stop_server
from .process import _fetch, _port, _wait_for_tcp

ON_PREM_DIR = ROOT / "tests" / "fixtures" / "dashboard-on-prem-browser"
ON_PREM_REFERENCE = ON_PREM_DIR / "reference.json"
ON_PREM_SCHEMA = 1

ADMIN_USERNAME = "admin"
MEMBER_USERNAME = "member"
PROFILE_ID = "team"
PROFILE_NAME = "Team"
TOKEN_LABEL = "qualification client"

# Sentinel credentials planted in the managed fixture plus the real passwords the
# administrator and member use during the run. The gate proves that none of them
# ever reaches an admin or member HTML snapshot.
ACCOUNT_SECRET = "EGGPOOL_ONPREM_SECRET_ACCOUNT_KEY_7291"
STATIC_HEADER_SECRET = "EGGPOOL_ONPREM_SECRET_HEADER_VALUE_7291"
ADMIN_PASSWORD = "M011 qualification admin passphrase 7291"
MEMBER_TEMP_PASSWORD = "M011 temporary member passphrase 7291"
MEMBER_PASSWORD = "M011 rotated member passphrase 7291"
SECRET_MARKERS = (
    ACCOUNT_SECRET,
    STATIC_HEADER_SECRET,
    ADMIN_PASSWORD,
    MEMBER_TEMP_PASSWORD,
    MEMBER_PASSWORD,
)

ADMIN_PAGES = ("/users", "/profiles", "/admin/audit")
MEMBER_PAGES = ("/my-account", "/my-account/models", "/usage")
# `/models` and `/latency` stay administrator-only, so a member must be refused
# there rather than served a member variant.
ADMIN_ONLY_PAGES = ("/users", "/profiles", "/admin/audit", "/models", "/latency")
# ``(label, width, height, device_scale_factor)``; HiDPI is qualified separately
# from the narrow viewport so a scaled desktop render is covered too.
ON_PREM_VIEWPORTS: tuple[tuple[str, int, int, float], ...] = (
    ("desktop", 1280, 800, 1.0),
    ("mobile", 390, 844, 1.0),
    ("desktop-hidpi", 1280, 800, 2.0),
)
# Every theme must render without clipping or overflow at every viewport.
ON_PREM_THEMES = ("default", "Cyber Red", "Catppuccin Latte", "Cyberpunk")

_MEMBER_ROW_EXPRESSION = """(function () {
  const rows = Array.from(document.querySelectorAll('table tbody tr'));
  return JSON.stringify(rows.map(row => {
    // The username is a row header (`<th scope="row">`), not a data cell.
    const cells = Array.from(row.querySelectorAll('th, td'));
    const stateField = row.querySelector(
      'form[action="/admin/users/state"] input[name="user_id"]');
    return {
      username: cells.length ? cells[0].textContent.trim() : '',
      role: cells.length > 1 ? cells[1].textContent.trim() : '',
      state: cells.length > 2 ? cells[2].textContent.trim() : '',
      must_change: cells.length > 3
        ? cells[3].textContent.includes('Must change password') : false,
      id: stateField ? stateField.value : '',
    };
  }));
})()"""

_ONE_TIME_TOKEN_EXPRESSION = """(function () {
  const pre = document.querySelector('pre');
  return pre ? pre.textContent.trim() : '';
})()"""

# Budget wording must stay truthful: enforced ceilings are hard limits, and
# cost/token ceilings are estimates until a request is priced.
_BUDGET_LABEL_EXPRESSION = """(function () {
  // Budget controls live inside collapsed `<details>`, whose text is not part of
  // `innerText` until they are open.
  document.querySelectorAll('details').forEach(node => { node.open = true; });
  const text = document.body.innerText;
  const labels = [];
  for (const needle of ['(hard)', 'estimated', 'hard', 'Hard', 'Estimated']) {
    if (text.includes(needle)) labels.push(needle);
  }
  return JSON.stringify(labels);
})()"""

_DOM_FACTS_TEMPLATE = r"""JSON.stringify((() => {
  const markers = __SECRET_MARKERS__;
  const ids = Array.from(document.querySelectorAll('[id]'), node => node.id);
  const viewportWidth = document.documentElement.clientWidth;
  const forms = Array.from(document.forms, form => ({
    action: new URL(form.action || location.href, location.href).pathname,
    method: (form.method || 'get').toUpperCase(),
    fields: Array.from(form.elements, element => element.name)
      .filter(name => Boolean(name)).sort(),
    unlabelled: Array.from(
      form.querySelectorAll('input, select, textarea'), element => {
        if (element.type === 'hidden') return null;
        if (element.getAttribute('aria-label')) return null;
        if (element.id && document.querySelector(
          'label[for="' + element.id + '"]')) return null;
        if (element.closest('label')) return null;
        return element.tagName.toLowerCase() + ':' + (element.name || '(unnamed)');
      }).filter(Boolean),
  }));
  const tables = Array.from(document.querySelectorAll('table'), table => {
    const width = Math.round(table.getBoundingClientRect().width);
    if (width <= viewportWidth) return null;
    return {wrapped: Boolean(table.closest('.table-scroll')), width: width};
  }).filter(Boolean);
  // `html { overflow-x: hidden }` makes the root a clipped scroller, so a
  // programmatic `scrollTo` can still move it and would misreport. Measure the
  // document against the viewport instead, and name the offending elements.
  const root = document.documentElement;
  const horizontalOverflow = root.scrollWidth > root.clientWidth + 1;
  const overflowing = horizontalOverflow
    ? Array.from(document.querySelectorAll('body *')).filter(element => {
        const box = element.getBoundingClientRect();
        if (box.width <= viewportWidth + 1) return false;
        return !element.closest('.table-scroll, .topnav-menu');
      }).slice(0, 5).map(element => {
        const classes = typeof element.className === 'string'
          ? element.className.trim().split(/\s+/).filter(Boolean) : [];
        return element.tagName.toLowerCase()
          + (classes.length ? '.' + classes.join('.') : '');
      })
    : [];
  const html = document.documentElement.outerHTML;
  return {
    path: location.pathname + location.search,
    title: document.title,
    h1: Array.from(document.querySelectorAll('h1'),
      node => node.textContent.trim()),
    h2: Array.from(document.querySelectorAll('h2'),
      node => node.textContent.trim()),
    viewportMeta: Boolean(document.querySelector(
      'meta[name="viewport"]')),
    duplicateIds: ids.filter((id, index) => ids.indexOf(id) !== index),
    forms: forms,
    tables: tables,
    horizontalOverflow: horizontalOverflow,
    overflowing: overflowing,
    secretMarkers: markers.filter(marker => html.includes(marker)),
  };
})())"""


def _dom_facts_expression() -> str:
    """Return the browser-side probe with the live credential markers embedded."""
    return _DOM_FACTS_TEMPLATE.replace(
        "__SECRET_MARKERS__", json.dumps(list(SECRET_MARKERS))
    )


def _on_prem_provider_config() -> str:
    """Return fixture providers carrying sentinel credentials for the leak guard."""
    return (
        f"[providers.onprem-alpha]\n"
        f'id = "onprem-alpha"\n'
        f'base_url = "https://fixture.invalid/onprem-alpha"\n'
        f'protocols = ["openai"]\n\n'
        f"[providers.onprem-alpha.auth]\n"
        f'mode = "bearer"\n\n'
        f"[providers.onprem-alpha.models_endpoint]\n"
        f'method = "GET"\n\n'
        f"[[providers.onprem-alpha.headers]]\n"
        f'name = "X-Fixture"\n'
        f'value = "{STATIC_HEADER_SECRET}"\n\n'
        f"[[providers.onprem-alpha.accounts]]\n"
        f'name = "onprem-alpha-main"\n'
        f'api_key = "{ACCOUNT_SECRET}"\n\n'
        f"[[providers.onprem-alpha.static_models]]\n"
        f'id = "onprem-chat-model"\n'
        f'protocol = "openai"\n\n'
    )


def _write_on_prem_config(root: Path, port: int) -> Path:
    """Write a managed-instance config with no server API key."""
    database = root / "on-prem.sqlite3"
    path = root / "on-prem.toml"
    path.write_text(
        f"[server]\n"
        f'host = "127.0.0.1"\nport = {port}\n\n[database]\n'
        f'path = "{database}"\n\n[dashboard]\nenabled = true\n'
        f'public = true\ntheme = "Cyber Red"\n'
        "refresh_interval_s = 1\n\n[models]\n"
        "startup_refresh = false\n\n[model_info]\n"
        "enabled = false\nstartup_refresh = false\n\n"
        f"{_on_prem_provider_config()}",
        encoding="utf-8",
    )
    return path


def _bootstrap_admin(config: Path, env: dict[str, str]) -> None:
    """Create the managed administrator through the supported CLI surface."""
    completed = subprocess.run(
        [
            str(RUST_BINARY),
            "--config",
            str(config),
            "users",
            "bootstrap",
            ADMIN_USERNAME,
            "--password-stdin",
        ],
        cwd=ROOT,
        env=env,
        input=f"{ADMIN_PASSWORD}\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise QualificationError(f"admin bootstrap failed: {detail}")


class _ManagedPage:
    """Drive one browser context against the managed on-prem instance."""

    def __init__(self, session: _HeadlessScreenshotSession, origin: str) -> None:
        self._session = session
        self._client = session.client
        self.origin = origin

    def viewport(self, width: int, height: int, scale: float) -> None:
        self._client.request(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": scale,
                "mobile": width < 600,
            },
        )

    def _evaluate(self, expression: str) -> Any:
        result = self._client.request(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        )
        remote = cast("dict[str, Any]", result.get("result", {}))
        if remote.get("subtype") == "error":
            raise QualificationError(f"page script failed: {remote.get('description')}")
        return remote.get("value")

    def script(self, expression: str) -> Any:
        """Evaluate one browser-side probe against the committed document."""
        return self._evaluate(expression)

    def rows(self) -> list[dict[str, Any]]:
        """Return the rendered local-account rows with their stable shape."""
        return cast(
            "list[dict[str, Any]]",
            json.loads(str(self._evaluate(_MEMBER_ROW_EXPRESSION))),
        )

    def one_time_token(self) -> str:
        return cast("str", self._evaluate(_ONE_TIME_TOKEN_EXPRESSION))

    def budget_labels(self) -> list[str]:
        return cast(
            "list[str]", json.loads(str(self._evaluate(_BUDGET_LABEL_EXPRESSION)))
        )

    def has_selector(self, selector: str) -> bool:
        return cast(
            "bool",
            self._evaluate(f"Boolean(document.querySelector({json.dumps(selector)}))"),
        )

    def text_length(self) -> int:
        return cast("int", self._evaluate("document.body.innerText.length"))

    def _mark_document(self) -> None:
        """Tag the outgoing document so a committed navigation is observable."""
        self._client.events.clear()
        self._evaluate("window.__eggpoolNavigationMark = true")

    def wait_for_navigation(self) -> None:
        """Block on a committed document rather than guessing with a sleep.

        A submitted form leaves the previous document ready until the redirect
        commits, so polling ``document.readyState`` would happily observe the
        outgoing page. The marker below only disappears when a new document has
        actually replaced it, which keeps the gate on an observable fixture
        boundary instead of a timing assumption.
        """
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            # Pumping a trivial evaluation drains buffered protocol traffic.
            self._client.request("Runtime.evaluate", {"expression": "1"})
            committed = self._evaluate(
                "typeof window.__eggpoolNavigationMark === 'undefined'"
                " && document.readyState === 'complete'"
            )
            if committed:
                self._evaluate("document.fonts ? document.fonts.ready : true")
                return
            time.sleep(0.02)
        raise QualificationError("navigation did not commit a new document")

    def goto(self, path: str) -> str:
        self._mark_document()
        self._client.request("Page.navigate", {"url": f"{self.origin}{path}"})
        self.wait_for_navigation()
        return cast("str", self._evaluate("location.pathname + location.search"))

    def facts(self) -> dict[str, Any]:
        raw = self._evaluate(_dom_facts_expression())
        return cast("dict[str, Any]", json.loads(str(raw)))

    def fill(self, selector: str, value: str) -> None:
        """Set one control's value the way typing into it would."""
        self._evaluate(
            "(function () {"
            f" const field = document.querySelector({json.dumps(selector)});"
            " if (!field) throw new Error('missing field ' + "
            f"{json.dumps(selector)});"
            f" field.value = {json.dumps(value)};"
            " field.dispatchEvent(new Event('input', {bubbles: true}));"
            " field.dispatchEvent(new Event('change', {bubbles: true}));"
            " return true; })()"
        )

    def csrf(self, action: str) -> str:
        """Return the CSRF token the page already minted for one form action."""
        escaped = json.dumps(action)
        value = self._evaluate(
            "(function () {"
            f" const form = document.querySelector('form[action={escaped}]');"
            " if (!form) return null;"
            " const field = form.querySelector('input[name=\"csrf\"]');"
            " return field ? field.value : null; })()"
        )
        if not value:
            raise QualificationError(f"no CSRF-fenced form for {action}")
        return cast("str", value)

    def submit(self, action: str, fields: dict[str, str]) -> str:
        """Fill and submit one server-rendered form exactly as a browser would."""
        escaped = json.dumps(action)
        self._mark_document()
        self._evaluate(
            "(function () {"
            f" const form = document.querySelector('form[action={escaped}]');"
            " if (!form) throw new Error('missing form ' + " + escaped + ");"
            f" const values = {json.dumps(fields)};"
            " for (const [name, value] of Object.entries(values)) {"
            "   const field = form.elements.namedItem(name);"
            "   if (!field) throw new Error('missing field ' + name);"
            "   field.value = value;"
            "   field.dispatchEvent(new Event('input', {bubbles: true}));"
            "   field.dispatchEvent(new Event('change', {bubbles: true}));"
            " }"
            " form.requestSubmit();"
            " return true; })()"
        )
        self.wait_for_navigation()
        return cast("str", self._evaluate("location.pathname + location.search"))

    def screenshot(self, artifact: Path) -> tuple[int, int, str]:
        result = self._client.request(
            "Page.captureScreenshot", {"format": "png", "fromSurface": True}
        )
        payload = base64.b64decode(str(result.get("data", "")), validate=True)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(payload)
        return (*_png_dimensions(artifact), hashlib.sha256(payload).hexdigest())

    def submit_button_point(self, action: str) -> tuple[float, float]:
        """Return the viewport point of a form's submit button.

        Measured while scripting is still enabled so the button can then be
        activated by a real mouse event after scripts are switched off.
        """
        escaped = json.dumps(action)
        point = self.script(
            "(function () {"
            f" const form = document.querySelector('form[action={escaped}]');"
            " if (!form) throw new Error('missing form ' + " + escaped + ");"
            " const button = form.querySelector("
            " 'button[type=submit], button:not([type])');"
            " if (!button) throw new Error('missing submit button');"
            " button.scrollIntoView({block: 'center', behavior: 'instant'});"
            " const box = button.getBoundingClientRect();"
            " return [box.left + box.width / 2, box.top + box.height / 2];"
            " })()"
        )
        coordinates = cast("list[float]", point)
        return (coordinates[0], coordinates[1])

    def click_at(self, x: float, y: float) -> None:
        """Dispatch a real trusted-style mouse click, not a scripted call.

        `buttons` has to describe the held button state or Chrome drops the
        press and no click is ever synthesised for the element under the point.
        """
        for event_type, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            self._client.request(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": x,
                    "y": y,
                    "button": "left",
                    "buttons": buttons,
                    "clickCount": 1,
                },
            )

    def sign_in_without_scripts(
        self, username: str, password: str
    ) -> tuple[str, dict[str, Any]]:
        """Fill and submit the login form with page scripting switched off.

        The button is activated by a real mouse event, so nothing in this path
        depends on the page's own JavaScript running.
        """
        self.goto("/login")
        self.fill("input[name=username]", username)
        self.fill("input[name=password]", password)
        point = self.submit_button_point("/login")
        # Tag the outgoing document before scripting is switched off: the marker
        # itself is page script, and a disabled page would never set it, leaving
        # the wait below to return before the click had navigated.
        self._mark_document()
        self.disable_scripts(True)
        try:
            self.click_at(*point)
            self.wait_for_navigation()
            landed = cast("str", self._evaluate("location.pathname + location.search"))
            notice = cast(
                "str",
                self.script(
                    "(function () { const alert ="
                    " document.querySelector('[role=alert]');"
                    " return alert ? alert.textContent : ''; })()"
                ),
            ).strip()
            if landed.startswith("/login"):
                raise QualificationError(
                    f"{username} could not sign in with scripting disabled: {notice!r}"
                )
        finally:
            self.disable_scripts(False)
        return landed, {"sign_in": landed, "scripts_required": False}

    def sign_in(self, username: str, password: str) -> str:
        """Sign in through the real form and prove the session took effect."""
        self.goto("/login")
        landed = self.submit("/login", {"username": username, "password": password})
        if landed.startswith("/login"):
            notice = cast(
                "str",
                self.script(
                    "(function () { const alert ="
                    " document.querySelector('[role=alert]');"
                    " return alert ? alert.textContent : ''; })()"
                ),
            ).strip()
            raise QualificationError(
                f"{username} was not signed in and stayed on the login form: {notice!r}"
            )
        return landed

    def sign_out(self) -> None:
        if self.has_selector('form[action="/logout"]'):
            self.submit("/logout", {})

    def disable_scripts(self, disabled: bool) -> None:
        self._client.request(
            "Emulation.setScriptExecutionDisabled", {"value": disabled}
        )


def _report_path(path: Path) -> str:
    """Render a path for the run summary without failing outside the repo."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _stable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop per-run identifiers so the approved reference stays comparable."""
    return [{key: value for key, value in row.items() if key != "id"} for row in rows]


def _canonical(value: Any) -> str:
    """Render one observation as stable JSON for the approved reference."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _record(observations: dict[str, str], key: str, value: Any) -> dict[str, Any]:
    observations[key] = _canonical(value)
    return cast("dict[str, Any]", value)


def _assert_no_defects(key: str, facts: dict[str, Any], label: str) -> None:
    """Fail on the bounded defect classes this plan is allowed to fix."""
    problems: list[str] = []
    if facts["duplicateIds"]:
        problems.append(f"duplicate ids {facts['duplicateIds']}")
    if facts["horizontalOverflow"]:
        problems.append(
            "page overflows the viewport"
            + (f" at {facts['overflowing']}" if facts.get("overflowing") else "")
        )
    if facts["tables"]:
        unwrapped = [item["width"] for item in facts["tables"] if not item["wrapped"]]
        if unwrapped:
            problems.append(f"unbounded tables wider than viewport: {unwrapped}")
    unlabelled = [
        f"{form['action']}:{name}"
        for form in facts["forms"]
        for name in form["unlabelled"]
    ]
    if unlabelled:
        problems.append(f"unlabelled controls {unlabelled}")
    if facts["secretMarkers"]:
        problems.append(f"secret markers rendered {sorted(facts['secretMarkers'])}")
    if not facts["viewportMeta"]:
        problems.append("missing viewport meta")
    if problems:
        raise QualificationError(f"{label} {key}: " + "; ".join(problems))


def _collect_page(
    page: _ManagedPage,
    observations: dict[str, str],
    *,
    role: str,
    path: str,
    viewport: tuple[str, int, int, float],
    screenshot_dir: Path | None,
    screenshots: list[dict[str, Any]],
    theme: str = "",
) -> None:
    name, width, height, scale = viewport
    page.viewport(width, height, scale)
    suffix = f"?theme={theme.replace(' ', '%20')}" if theme else ""
    page.goto(f"{path}{suffix}")
    key = f"page.{role}.{path}.{name}" + (f".{theme}" if theme else "")
    facts = _record(observations, key, page.facts())
    _assert_no_defects(key, facts, role)
    if screenshot_dir is not None:
        width_px, height_px, digest = page.screenshot(
            screenshot_dir / f"{role}{path.replace('/', '-')}-{name}"
            f"{'-' + theme.replace(' ', '-') if theme else ''}.png"
        )
        screenshots.append(
            {
                "key": key,
                "width": width_px,
                "height": height_px,
                "sha256": digest,
            }
        )


def qualify_on_prem_browser(
    *,
    root: Path,
    write_reference: bool = False,
    screenshot_dir: Path | None = None,
) -> dict[str, Any]:
    """Qualify the managed admin/member pages against the approved reference."""
    instance = root / "on-prem"
    instance.mkdir(parents=True)
    home = instance / "home"
    home.mkdir(exist_ok=True)
    port = _port()
    # The control socket has a hard path-length ceiling, so the runtime directory
    # stays short like the historical fixtures instead of living under the
    # already-deep temporary instance root.
    runtime = Path(f"/tmp/eq11-onprem-{port}")
    config = _write_on_prem_config(instance, port)
    env = dict(os.environ)
    for key in (
        "EGGPOOL_CONFIG",
        "EGGPOOL_RUNTIME_DIR",
        "EGGPOOL_PID_FILE",
        "EGGPOOL_API_KEY",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(home),
            "TZ": "UTC",
            "EGGPOOL_RUNTIME_DIR": str(runtime),
            "EGGPOOL_PID_FILE": str(instance / "on-prem.pid"),
        }
    )
    observations: dict[str, str] = {}
    screenshots: list[dict[str, Any]] = []
    _bootstrap_admin(config, env)
    server = _start_server([str(RUST_BINARY)], config, env)
    session: _HeadlessScreenshotSession | None = None
    try:
        _wait_for_tcp(port, server, "managed on-prem dashboard")
        origin = f"http://127.0.0.1:{port}"
        session = _HeadlessScreenshotSession()
        page = _ManagedPage(session, origin)

        # Anonymous browsers must never reach any managed page.
        for path in (*ADMIN_ONLY_PAGES, "/my-account", "/usage"):
            page.viewport(1280, 800, 1.0)
            landed = page.goto(path)
            _record(observations, f"anonymous.{path}", landed)

        # Sign in as the administrator with scripting disabled to prove the login
        # and every later form work without JavaScript.
        landed, no_js = page.sign_in_without_scripts(ADMIN_USERNAME, ADMIN_PASSWORD)
        _record(observations, "admin.sign_in", landed)
        _record(observations, "admin.no_js_login", no_js)

        page.goto("/profiles")
        csrf = page.csrf("/admin/profiles/create")
        _record(
            observations,
            "admin.profile_created",
            page.submit(
                "/admin/profiles/create",
                {
                    "profile_id": PROFILE_ID,
                    "name": PROFILE_NAME,
                    "default_effect": "deny",
                    "csrf": csrf,
                },
            ),
        )
        page.goto("/users")
        csrf = page.csrf("/admin/users/create")
        _record(
            observations,
            "admin.member_created",
            page.submit(
                "/admin/users/create",
                {
                    "username": MEMBER_USERNAME,
                    "password": MEMBER_TEMP_PASSWORD,
                    "profile_id": PROFILE_ID,
                    "csrf": csrf,
                },
            ),
        )
        issued = page.goto("/users")
        _record(observations, "admin.after_member_created", issued)
        member_state = page.rows()
        _record(observations, "admin.member_row", _stable_rows(member_state))
        member = next(
            (row for row in member_state if row["username"] == MEMBER_USERNAME), None
        )
        if member is None or member["must_change"] is not True:
            raise QualificationError(
                f"member was not created as a forced password change: {member_state}"
            )

        page.goto("/users")
        csrf = page.csrf("/admin/users/state")
        _record(
            observations,
            "admin.member_suspended",
            page.submit(
                "/admin/users/state",
                {"user_id": member["id"], "enabled": "0", "csrf": csrf},
            ),
        )
        page.goto("/users")
        csrf = page.csrf("/admin/users/state")
        _record(
            observations,
            "admin.member_reactivated",
            page.submit(
                "/admin/users/state",
                {"user_id": member["id"], "enabled": "1", "csrf": csrf},
            ),
        )

        for path in ADMIN_PAGES:
            for viewport in ON_PREM_VIEWPORTS:
                _collect_page(
                    page,
                    observations,
                    role="admin",
                    path=path,
                    viewport=viewport,
                    screenshot_dir=screenshot_dir,
                    screenshots=screenshots,
                )
        for theme in ON_PREM_THEMES:
            _collect_page(
                page,
                observations,
                role="admin",
                path="/users",
                viewport=ON_PREM_VIEWPORTS[1],
                screenshot_dir=screenshot_dir,
                screenshots=screenshots,
                theme=theme,
            )

        page.goto("/my-account")
        csrf = page.csrf("/account/tokens")
        reveal = page.submit("/account/tokens", {"label": TOKEN_LABEL, "csrf": csrf})
        _record(observations, "admin.token_reveal", reveal)
        plaintext = page.one_time_token()
        if len(plaintext) < 16:
            raise QualificationError("one-time token reveal did not expose a token")
        _record(observations, "admin.token_revealed_once", True)
        # The reveal is the only place the plaintext may ever appear: returning to
        # the account page must show the credential label, never the credential.
        page.goto("/my-account")
        _record(
            observations,
            "admin.token_absent_on_return",
            plaintext
            not in cast("str", page.script("document.documentElement.outerHTML")),
        )
        csrf = page.csrf("/account/tokens")
        second = page.submit(
            "/account/tokens", {"label": f"{TOKEN_LABEL} again", "csrf": csrf}
        )
        _record(observations, "admin.token_second_reveal", second)
        second_plaintext = page.one_time_token()
        _record(
            observations,
            "admin.token_reveal_is_fresh_each_time",
            second_plaintext.startswith("epk1_")
            and second_plaintext != plaintext
            and plaintext not in second_plaintext,
        )

        page.goto("/users")
        csrf = page.csrf("/admin/users/override")
        _record(
            observations,
            "admin.override_saved",
            page.submit(
                "/admin/users/override",
                {
                    "user_id": member["id"],
                    "model_id": "onprem-chat-model",
                    "effect": "allow",
                    "csrf": csrf,
                },
            ),
        )
        page.goto("/users")
        csrf = page.csrf("/admin/users/budget")
        _record(
            observations,
            "admin.budget_saved",
            page.submit(
                "/admin/users/budget",
                {
                    "user_id": member["id"],
                    "model_id": "",
                    "window": "month",
                    "max_requests": "100",
                    "max_tokens": "100000",
                    "max_microdollars": "50000",
                    "max_concurrent": "4",
                    "csrf": csrf,
                },
            ),
        )
        _record(
            observations,
            "admin.budget_labels",
            page.budget_labels(),
        )
        page.goto("/users")
        csrf = page.csrf("/admin/users/profile")
        _record(
            observations,
            "admin.profile_conflict",
            page.submit(
                "/admin/users/profile",
                {
                    "user_id": member["id"],
                    "profile_id": "missing-profile",
                    "csrf": csrf,
                },
            ),
        )

        page.sign_out()
        landed = page.sign_in(MEMBER_USERNAME, MEMBER_TEMP_PASSWORD)
        _record(observations, "member.forced_password_landing", landed)
        csrf = page.csrf("/account/password")
        _record(
            observations,
            "member.password_changed",
            page.submit(
                "/account/password",
                {
                    "current_password": MEMBER_TEMP_PASSWORD,
                    "new_password": MEMBER_PASSWORD,
                    "csrf": csrf,
                },
            ),
        )
        # Changing a password bumps the account auth epoch, so the browser
        # session is invalidated on purpose and the member has to sign in again.
        _record(
            observations,
            "member.reauthenticated_after_password_change",
            page.sign_in(MEMBER_USERNAME, MEMBER_PASSWORD),
        )
        for path in MEMBER_PAGES:
            for viewport in ON_PREM_VIEWPORTS:
                _collect_page(
                    page,
                    observations,
                    role="member",
                    path=path,
                    viewport=viewport,
                    screenshot_dir=screenshot_dir,
                    screenshots=screenshots,
                )
        for path in ADMIN_ONLY_PAGES:
            page.viewport(1280, 800, 1.0)
            _record(observations, f"member_denied.{path}", page.goto(path))
            _record(
                observations,
                f"member_denied.{path}.message",
                cast("str", page.script("document.body.innerText")).strip(),
            )
        page.goto("/usage")
        _record(
            observations,
            "member.budget_labelling",
            page.budget_labels(),
        )

        page.sign_out()
        for path in ADMIN_ONLY_PAGES:
            page.viewport(1280, 800, 1.0)
            _record(observations, f"after_logout.{path}", page.goto(path))

        agent_token = _issue_agent_token(config, env)
        for path in ("/users", "/my-account"):
            status = _fetch(
                f"{origin}{path}", headers={"Authorization": f"Bearer {agent_token}"}
            )
            _record(observations, f"agent_token.{path}", status.status)
        _stop_server(server)
        server = _start_server([str(RUST_BINARY)], config, env)
        _wait_for_tcp(port, server, "restarted managed on-prem dashboard")
        # The signed-in browser session and every managed record live in the
        # database, so a restart must not lose the account or its policy state.
        _record(
            observations,
            "restart.session_survives_restart",
            page.goto("/users"),
        )
        page.goto("/profiles")
        _record(
            observations,
            "restart.profiles_survive_restart",
            PROFILE_ID in cast("str", page.script("document.body.innerText")),
        )
        page.goto("/users")
        _record(
            observations,
            "restart.member_row",
            _stable_rows(
                [
                    row
                    for row in page.rows()
                    if row["username"] in {ADMIN_USERNAME, MEMBER_USERNAME}
                ]
            ),
        )
    finally:
        if session is not None:
            session.close()
        _stop_server(server)
        shutil.rmtree(runtime, ignore_errors=True)

    return _reconcile_reference(observations, screenshots, write_reference)


def _issue_agent_token(config: Path, env: dict[str, str]) -> str:
    """Issue a personal agent token to prove agent credentials stay off the UI."""
    completed = subprocess.run(
        [
            str(RUST_BINARY),
            "--config",
            str(config),
            "users",
            "token",
            ADMIN_USERNAME,
            "--label",
            "agent probe",
            "--show-secret",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise QualificationError(
            f"agent token issue failed: {completed.stderr.strip()}"
        )
    for line in completed.stdout.splitlines():
        candidate = line.strip()
        if candidate.startswith("epk1_"):
            return candidate
    raise QualificationError(f"agent token plaintext not found: {completed.stdout}")


def _reconcile_reference(
    observations: dict[str, str],
    screenshots: list[dict[str, Any]],
    write_reference: bool,
) -> dict[str, Any]:
    """Freeze or compare this run against the Rust-owned approved reference."""
    candidate = {
        "schema": ON_PREM_SCHEMA,
        "observations": observations,
        "screenshots": screenshots,
    }
    if write_reference:
        ON_PREM_DIR.mkdir(parents=True, exist_ok=True)
        ON_PREM_REFERENCE.write_text(
            json.dumps(candidate, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return {
            "result": "reference-written",
            "reference": _report_path(ON_PREM_REFERENCE),
            "observations": len(observations),
            "screenshots": len(screenshots),
        }
    if not ON_PREM_REFERENCE.is_file():
        raise QualificationError(
            f"missing approved on-prem reference "
            f"{_report_path(ON_PREM_REFERENCE)}; re-run with "
            "--write-on-prem-reference after reviewing the diff"
        )
    approved = json.loads(ON_PREM_REFERENCE.read_text(encoding="utf-8"))
    if approved.get("schema") != ON_PREM_SCHEMA:
        raise QualificationError(
            f"approved on-prem reference schema {approved.get('schema')} "
            f"does not match {ON_PREM_SCHEMA}"
        )
    expected = cast("dict[str, str]", approved.get("observations", {}))
    differences: list[str] = []
    for key in sorted(set(expected) | set(observations)):
        if key not in observations:
            differences.append(f"{key}: absent from this run")
        elif key not in expected:
            differences.append(
                f"{key}: {observations[key]} is not in the approved reference"
            )
        elif observations[key] != expected[key]:
            differences.append(f"{key}: {expected[key]} -> {observations[key]}")
    if differences:
        raise QualificationError(
            "on-prem browser drift from the approved reference: "
            + "; ".join(differences[:20])
            + (f"; and {len(differences) - 20} more" if len(differences) > 20 else "")
        )
    return {
        "result": "passed",
        "reference": _report_path(ON_PREM_REFERENCE),
        "observations": len(observations),
        "screenshots": len(screenshots),
    }


__all__ = [
    "ON_PREM_DIR",
    "ON_PREM_REFERENCE",
    "ON_PREM_SCHEMA",
    "ON_PREM_THEMES",
    "ON_PREM_VIEWPORTS",
    "QualificationError",
    "qualify_on_prem_browser",
]
