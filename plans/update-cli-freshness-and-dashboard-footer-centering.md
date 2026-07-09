# Plan: Fresh `eggpool update` Checks and Dashboard Footer Centering

## Context

Operators have observed a small but user-visible update-flow bug: when a newer EggPool release exists, the first `eggpool update` invocation may print `Already up to date.`, while a second invocation immediately afterward detects the newer release and updates correctly.

The current CLI path does not intentionally read the dashboard/background `UpdateChecker.snapshot()` cache before deciding. `eggpool update` calls the one-shot `async_check_for_update()` helper, which resolves the installed distribution version with `importlib.metadata.version("eggpool")`, performs a PyPI JSON request against `https://pypi.org/pypi/eggpool/json`, extracts `info.version`, and returns `(current_version, latest_version, error)`. The CLI then compares `current_version == latest_version` and prints `Already up to date.` on equality.

The likely failure mode is a stale PyPI/CDN response from the CLI's first PyPI request. A second command invocation then sees a refreshed response and proceeds correctly. The fix should make the updater freshness-oriented, avoid raw equality as the decision primitive, and add regression coverage so the command cannot silently trust stale release metadata when a fresher source says an update exists.

A separate dashboard polish item should also be included in the same patch: the dashboard already correctly displays the update-available footer message, but that message should be horizontally centered.

## Goals

1. Make `eggpool update` robust against stale PyPI JSON responses.
2. Keep the dashboard/background update indicator semantics unchanged.
3. Preserve the current install-method behavior for pip, pipx, uv-tool, source checkout, and `--from-source`.
4. Replace raw string equality in the updater decision with semantic version ordering.
5. Center the existing dashboard update-available footer message without changing its content, visibility rules, or copy behavior.
6. Add targeted tests for the stale-response behavior and footer CSS/layout polish.

## Non-goals

1. Do not redesign the updater UX.
2. Do not add a long-lived local release cache for the CLI.
3. Do not change the dashboard indicator's data contract or endpoint response shape unless the implementation proves it is already inconsistent.
4. Do not change service restart behavior after a successful update.
5. Do not introduce a heavyweight dependency solely for version comparison unless the project already depends on it transitively and policy allows it.

## Current implementation areas to inspect

1. `src/eggpool/update_checker.py`
   - `PYPI_URL`
   - `_fetch_pypi_response_sync()`
   - `_latest_version_from_response()`
   - `async_check_for_update()`
   - `UpdateChecker.check_once()`
   - `UpdateChecker.snapshot()`
   - `_is_newer()` / `_pep440_key()`

2. `src/eggpool/cli_full.py`
   - `update()` command
   - `_detect_install_method()`
   - update command construction for `pip`, `pipx`, `uv-tool`, `source`, and `--from-source`

3. Dashboard rendering/CSS
   - `src/eggpool/dashboard/render.py` or equivalent layout/footer renderer
   - `src/eggpool/dashboard/static/dashboard.css` or equivalent stylesheet
   - Any tests covering `update_info`, footer rendering, sticky topbar/footer, or update indicator display

4. Tests
   - CLI tests around `eggpool update`
   - UpdateChecker unit tests
   - Dashboard render/CSS tests

## Implementation plan

### 1. Split freshness-aware CLI update lookup from background checker state

Keep `UpdateChecker` as the dashboard/background state holder. Its cached `UpdateInfo` behavior is appropriate for the dashboard and should not be used as the CLI updater's source of truth.

Introduce or refactor a CLI-specific lookup helper in `update_checker.py`, for example:

```python
check_for_update_now(
    *,
    package_name: str = "eggpool",
    timeout_s: float = _CHECK_TIMEOUT_S,
    fresh: bool = True,
) -> UpdateInfo | tuple[str, str, str]
```

The helper should still be easy to patch in tests. If retaining the existing tuple return of `async_check_for_update()` is simpler, update that helper in place and keep the public signature stable. The important behavior is that the CLI path must perform an immediate network lookup and must not read the `UpdateChecker` cached snapshot.

### 2. Add cache-avoidance to the PyPI request used by `eggpool update`

Modify the sync PyPI fetch path so the CLI can request fresh metadata. The preferred implementation is to add optional freshness parameters to `_fetch_pypi_response_sync()`:

```python
_fetch_pypi_response_sync(
    *,
    timeout_s: float,
    http_get: Callable[..., httpx.Response] | None = None,
    fresh: bool = False,
) -> httpx.Response
```

When `fresh=True`, send request headers such as:

```text
Cache-Control: no-cache, max-age=0
Pragma: no-cache
Accept: application/json
User-Agent: eggpool/<current-or-unknown> update-check
```

Also consider appending a cache-busting query parameter to the PyPI JSON URL for CLI update checks only, for example `?eggpool_update_check=<unix_ns>`. The helper should use `httpx` parameter handling rather than manual string concatenation where practical.

Keep the background `UpdateChecker` daily check conservative. It may use the normal URL and existing shared async client, since the dashboard is informational and not an install decision point. If it is simpler to also send no-cache headers for background checks, verify this does not materially increase traffic or create needless request uniqueness.

### 3. Use semantic version comparison in the CLI decision

Change `cli_full.update()` so it does not treat raw string equality as the sole decision boundary.

Current behavior:

```python
if current_version == latest_version:
    click.echo("Already up to date.")
    return
```

Target behavior:

```python
if not UpdateChecker._is_newer(current_version, latest_version):
    click.echo("Already up to date.")
    return
```

Prefer extracting `_is_newer()` into a module-level public-ish helper such as `is_newer_version(current, latest)` instead of calling a static private method from CLI code. Preserve the existing simple local PEP 440 subset implementation unless a project-approved dependency is already available.

This avoids false positives/negatives with tags such as `v0.5.10`, `0.5.10.post1`, or normalized version strings. It also aligns the CLI with the dashboard checker's decision semantics.

### 4. Add a stale-response guard for the CLI update path

Implement one of these patterns, in preference order:

Preferred: two-source verification when PyPI says no update.

If the fresh PyPI response says `latest <= current`, optionally query GitHub releases/tags as a secondary source. This should only happen in the CLI update command, and only when the PyPI result is not newer. It should be bounded by the same timeout discipline and must fail gracefully.

Reasonable lighter alternative: one retry with fresh cache-busting if PyPI says no update.

If the first PyPI response says `latest <= current`, immediately perform one second PyPI fetch with a different cache-busting value and no-cache headers before concluding `Already up to date.`. This directly addresses the observed first-run stale response without introducing a GitHub dependency.

The plan should prefer the lighter alternative if maintainers want minimal change. The acceptance criteria below assume at least a second fresh PyPI request when the first result is not newer.

Pseudo-flow:

```python
current = installed_version()
latest, error = fetch_pypi_latest(fresh=True)
if error: abort

if not is_newer_version(current, latest):
    second_latest, second_error = fetch_pypi_latest(fresh=True, cache_bust=True)
    if not second_error and is_newer_version(current, second_latest):
        latest = second_latest
    else:
        print("Already up to date.")
        return

run installer for latest
```

Do not retry indefinitely. A single second fresh request is enough to remove the observed footgun without making the update command slow or noisy.

### 5. Improve operator diagnostics without adding noise

When an update is found after the second fresh request, consider printing a concise diagnostic only in verbose/debug mode if such a mode exists. If not, avoid extra output unless it is actionable.

Do not print raw response headers by default. Do not show implementation details such as CDN cache status unless a debug flag already exists.

If PyPI returns an older version than the installed version, keep the output as `Already up to date.` or a slightly clearer message such as `Already up to date. PyPI latest: X; installed: Y.` Avoid treating installed newer-than-PyPI as an error.

### 6. Preserve install command behavior

Do not alter these command-generation branches except where necessary to pass the semantically selected `latest_version`:

1. `--from-source` + source checkout: `uv sync --no-dev`
2. `--from-source` + pipx: install Git URL target
3. `--from-source` + uv-tool: install Git URL target
4. `--from-source` + pip: pip install Git URL target
5. source checkout without `--from-source`: `uv sync --no-dev --directory <repo_root>`
6. pipx: `pipx upgrade eggpool`
7. uv-tool: `uv tool install eggpool==<latest_version>`
8. pip: `python -m pip install --upgrade eggpool`

One possible improvement, if tests confirm current behavior is fragile, is adding `--force` or reinstall semantics for `uv tool install eggpool==<latest_version>` when uv reports the tool already installed. Only include that in the patch if it is needed for correctness; otherwise keep the patch constrained.

### 7. Center the dashboard update footer message

Find the existing footer/update indicator markup and CSS. The dashboard already correctly displays the message when `update_available` is true; preserve that condition and text.

Update the footer/update indicator layout so the message is horizontally centered. Prefer CSS over renderer changes unless the markup prevents reliable centering.

Candidate CSS pattern:

```css
.footer-update-indicator,
.update-indicator {
  text-align: center;
  justify-content: center;
  margin-left: auto;
  margin-right: auto;
}
```

Use the actual class names from the dashboard stylesheet. If the footer contains multiple items, avoid centering the entire footer in a way that breaks other footer controls. Center only the update-available message container or make the footer a grid with a centered update area while preserving left/right utility text.

Validate desktop and narrow/mobile widths. The message should not overlap the ready/status span or copy button, and wrapping should remain visually coherent.

## Tests

### Unit tests for update freshness

Add/update tests around the CLI helper and `eggpool update` command.

Required cases:

1. `async_check_for_update()` or its replacement sends no-cache headers in CLI fresh mode.
2. The helper appends or otherwise supplies a cache-busting request only for fresh CLI checks if that approach is used.
3. When the first PyPI response returns the installed/current version and the second fresh response returns a newer version, `eggpool update --check` reports that an update is available.
4. When both fresh responses return the installed/current version, `eggpool update --check` reports `Already up to date.`.
5. When the first response returns a newer version, the command does not perform the second request.
6. When the first response fails, preserve existing error behavior unless the implementation intentionally retries failures.
7. Version comparison uses semantic ordering: `0.5.10` must compare newer than `0.5.9`; `0.5.9.post1` should compare newer than `0.5.9`; `0.5.9rc1` should not compare newer than final `0.5.9`.
8. The CLI path does not read or depend on `UpdateChecker.snapshot()` before making its fresh lookup.

### CLI command tests

Patch `subprocess.run()` so tests never install anything. For non-`--check` tests, assert the command selection still matches the detected install method. At minimum, cover `pip`, `pipx`, and `uv-tool`; source checkout behavior can be covered if existing tests already mock `Path(__file__)` safely.

Required cases:

1. `eggpool update --check` with stale first response and fresh second response exits zero and prints `An update is available.`.
2. `eggpool update --check` with no update exits zero and prints `Already up to date.`.
3. `eggpool update` with a newer version calls the expected installer command once.
4. `eggpool update` does not call installer commands when no update is found.

### Dashboard footer tests

Add or update render/CSS tests:

1. Existing update-available footer message still renders only when `update_available` is true.
2. Existing message content remains unchanged.
3. The relevant CSS selector includes centering behavior, such as `text-align: center`, `justify-content: center`, grid center placement, or equivalent.
4. If the renderer uses a wrapper class, assert that the update indicator is inside that wrapper and can be targeted independently from other footer/status content.

Avoid screenshot-based tests unless the project already uses them. CSS text/assertion tests should be sufficient for this minor polish item.

## Manual validation

Run these locally after patching:

```bash
uv run pytest tests/test_update_checker.py tests/test_cli.py tests/test_dashboard_render.py
uv run ruff check src tests
uv run pyright
```

Then manually validate with a local monkeypatch or temporary test hook that simulates stale PyPI metadata:

1. Installed/current version is `0.5.9`.
2. First PyPI response says latest is `0.5.9`.
3. Second fresh PyPI response says latest is `0.5.10`.
4. `eggpool update --check` reports an update.
5. `eggpool update` proceeds to the update command rather than printing `Already up to date.`.

For dashboard polish, start the server with a mocked update-available state or test fixture and verify the footer message is centered on desktop and mobile-width windows.

## Acceptance criteria

1. `eggpool update` does not read stale dashboard/background `UpdateInfo` state before deciding whether an update exists.
2. CLI update checks send cache-avoidance headers and/or cache-busting parameters for the install-decision request.
3. If a first fresh PyPI response says no update but a second fresh response says a newer version exists, `eggpool update` detects the newer version on that same invocation.
4. `eggpool update --check` and `eggpool update` both use semantic version ordering rather than raw string equality.
5. Existing dashboard update indicator behavior remains unchanged except for horizontal centering.
6. Installer command selection for pip, pipx, uv-tool, source checkout, and `--from-source` is not regressed.
7. Tests cover stale-first-response behavior, semantic version comparison, no-update behavior, and dashboard footer centering.
8. Full targeted test suite passes locally.

## Suggested commit message

```text
Harden update freshness checks and center footer notice
```

## Risk notes

The main risk is overcorrecting by making every background update check bypass caches, which could create unnecessary traffic to PyPI. Keep freshness bypass narrowly scoped to `eggpool update`, where a stale result causes a wrong install decision.

The second risk is coupling the CLI to private `UpdateChecker` internals. Prefer a module-level `is_newer_version()` helper so both CLI and dashboard use the same comparison logic without importing a private static method.

The dashboard CSS change should be scoped to the update notice. Centering the whole footer may disturb status text or copy controls, so target the update message container specifically unless the current footer is already a single-purpose update message row.
