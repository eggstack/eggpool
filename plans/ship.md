# PyPI release polish — `eggpool` 0.1.0

This plan turns the audit into a concrete sequence of changes. It is organized
into pre-flight blockers, asset bundling, metadata, install/UX, and release
mechanics. Each step lists the files touched, the rationale, and a validation
gate that must pass before moving on.

Source audit: see the chat session that produced this file (the "PyPI
readiness audit"). Cross-references like `(#1)` map to the numbered findings
in that audit.

---

## 0. Conventions

- Branch: cut `release/0.1.0` from `main` for this work.
- All commits in the plan are PR-quality: imperative-mood subject, body that
  references this file and the audit finding, no secrets.
- Pre-commit gate runs on every commit and on `release/0.1.0` head:

  ```bash
  uv run ruff format --check src/ tests/ scripts/
  uv run ruff check src/ tests/ scripts/
  uv run pyright src/ scripts/
  uv run pytest
  ```

- After each numbered step, the matching validation gate must pass before
  continuing.
- No new top-level directories without justification; prefer extending
  `src/eggpool/` and `tests/`.

---

## 1. Pre-flight: confirm we're clear to ship 0.1.0

### 1.1 Decide on `bugs.md` outstanding issues

`bugs.md` is git-ignored (`.gitignore:67`) and lists one substantive
multi-provider dispatch issue:

> `src/eggpool/request/coordinator.py` — `SelectedAttempt.provider_id` is
> set from `context.provider_id or "opencode-go"` rather than from the
> selected account, so an unsuffixed request can dispatch to the wrong
> upstream.

Files:
- `bugs.md` (read-only reference, not committed)
- `src/eggpool/request/coordinator.py` (if fix is needed)
- `src/eggpool/request/attempt_finalizer.py` (downstream of the fix)
- `tests/integration/test_provider_routing_e2e.py` (extend coverage)

Action:
- [ ] Read `bugs.md` end-to-end with the user.
- [ ] If the dispatch bug is still present, either fix it on
  `release/0.1.0` and add a regression test, or bump the release to 0.1.1
  / 0.2.0 and move 0.1.0 to a docs-only release.
- [ ] Once resolved, add a `CHANGELOG.md` entry noting the fix or the
  deferral.

Validation gate: explicit sign-off recorded in the PR description.

### 1.2 Clean `dist/` and any stale `egg-info`

Files:
- `dist/` (entire directory, including `.gitignore`)
- `src/*.egg-info/` (none expected, but verify)

Action:
- [ ] `git rm -rf dist/` if anything is tracked. Today nothing is tracked
  (`git ls-files dist/` is empty), so the directory just needs removing
  locally before the release tag.
- [ ] `rm -rf dist/ src/*.egg-info/`
- [ ] Confirm `git status` is clean afterwards.

Validation gate: `git status` reports a clean working tree.

---

## 2. Blockers — must land before any PyPI upload

### 2.1 Exclude dev-only directories from the sdist (#1)

The current sdist ships 5.2M compressed / 29M unpacked, with 26M of
`.opencode/node_modules/` (untracked JS, swept up by hatchling's default
include). Wheel content is unaffected; this is a tarball hygiene fix.

Files:
- `pyproject.toml` — add a `[tool.hatch.build.targets.sdist]` table

Action: append the following block under the existing
`[tool.hatch.build.targets.wheel]`:

```toml
[tool.hatch.build.targets.sdist]
exclude = [
    ".opencode",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    ".pyright",
    "dist",
    "build",
    "*.egg-info",
    "*.sqlite3",
    "*.sqlite3-wal",
    "*.sqlite3-shm",
    "coverage.xml",
    ".coverage",
    ".coverage.*",
    "htmlcov",
    "*.log",
    "bugs.md",
    "/var",
]
exclude-globs = [
    "**/__pycache__",
    "**/.DS_Store",
]
```

Why each entry:
- `.opencode`, `.venv`, `.pytest_cache`, `.ruff_cache`, `.pyright`,
  `dist`, `build`, `*.egg-info`, `htmlcov`: local tool caches / build
  outputs that should never be in a redistributable.
- `*.sqlite3*`, `*.log`, `/var`, `bugs.md`, `coverage.xml`, `.coverage*`:
  runtime artefacts and the local-only bug tracker.
- `**/__pycache__`, `**/.DS_Store`: belt-and-suspenders for the sdist
  built outside the git tree (hatch's default VCS ignore relies on
  `.gitignore`, which is unreliable when the sdist is built from a clean
  checkout or release tarball).

Validation gate:

```bash
uv run python -m build --sdist --outdir /tmp/sdist-check
mkdir -p /tmp/sdist-check/unpack && tar -xzf /tmp/sdist-check/eggpool-0.1.0.tar.gz -C /tmp/sdist-check/unpack
du -sh /tmp/sdist-check/unpack/eggpool-0.1.0
test -d /tmp/sdist-check/unpack/eggpool-0.1.0/.opencode && echo FAIL || echo OK
ls /tmp/sdist-check/unpack/eggpool-0.1.0 | sort
```

Expected: unpacked size < 2M, no `.opencode/`, no `__pycache__/`,
`src/`, `tests/`, `scripts/`, `docs/`, `deploy/`, `architecture/`,
`config-examples/`, `themes/`, `config.example.toml`,
`providers.toml`, `AGENTS.md`, `README.md`, `pyproject.toml`, `uv.lock`
all present.

### 2.2 Bundle `themes/` inside the package (#2)

The dashboard's theme selector reads from a CWD-relative `themes_dir`
(`config.example.toml:58`, `src/eggpool/dashboard/routes.py:59`,
`src/eggpool/dashboard/theme.py:433-438`). Pip-installed users have no
`themes/` directory and the theme picker only shows "default".

Files:
- `themes/` → move into `src/eggpool/dashboard/themes/`
- `src/eggpool/dashboard/theme.py` — `list_themes` and `get_theme_css` to
  fall back to package data
- `src/eggpool/dashboard/render.py` — `get_theme_css` and `get_theme`
  default to package data
- `src/eggpool/dashboard/routes.py` — `_get_theme_data` resolves from
  app state
- `src/eggpool/models/config.py:114` — `themes_dir` default becomes
  `None`; the resolver picks package data when unset
- `tests/unit/test_dashboard_theme.py` — add coverage for
  bundled-resource loading and CWD override
- `tests/integration/test_phase16_release_validation.py` — assert
  `list_themes()` returns the bundled set when no override is set

Action:
- [ ] `git mv themes/* src/eggpool/dashboard/themes/ && rmdir themes`
- [ ] Introduce a single resolver, e.g.
  `src/eggpool/dashboard/_resources.py`, exposing
  `bundled_themes_dir() -> Path` that uses
  `importlib.resources.files("eggpool.dashboard").joinpath("themes")`.
- [ ] `list_themes(themes_dir)` becomes
  `list_themes(themes_dir | None)`: if `None`, use
  `bundled_themes_dir()`.
- [ ] `get_theme_css(theme_name, themes_dir)` and
  `get_theme(theme_name, themes_dir)` get the same default.
- [ ] `AppConfig.themes_dir` defaults to `""` (or `None`); the
  dashboard's `_get_theme_data` treats empty string as "use bundled".
- [ ] Update `config.example.toml:53-58` comments to mention the bundled
  set; the `themes_dir` line stays as an opt-in override.
- [ ] Add a unit test that installs into a tmpdir, runs
  `list_themes(None)`, and asserts the bundled names are present.
- [ ] Add a unit test that `list_themes("/nonexistent")` still returns
  the bundled set (no silent failure).

Validation gate: `uv run pytest tests/unit/test_dashboard_theme.py
tests/integration/test_phase16_release_validation.py -k theme` passes,
and a fresh `uv run python -m build --wheel` contains
`eggpool/dashboard/themes/*.toml`:

```bash
unzip -l dist/eggpool-0.1.0-py3-none-any.whl | grep dashboard/themes | wc -l
```

Expected count: ≥ 53 (the bundled Halloy themes).

### 2.3 Bundle `providers.toml` inside the package (#3)

`cli.py:118, 146, 1091` and `src/eggpool/providers/connect.py:833`
default to a CWD `providers.toml`. The fallback dict at
`src/eggpool/providers/connect.py:82-100` only covers `opencode-go`;
the other 8 providers in `providers.toml:17-69` are invisible to pip
users.

Files:
- `providers.toml` → move into `src/eggpool/providers/_templates.toml`
  (or `.py` if we want to convert to a typed module — recommend `.toml`
  to keep the file format identical to the public user-facing one)
- `src/eggpool/providers/connect.py` — `load_provider_templates` accepts
  `None` and falls back to `importlib.resources.files(...)`
- `src/eggpool/cli.py:118, 146, 1091` — default `providers_path` becomes
  `None`; the resolver picks package data when unset
- `tests/unit/test_connect.py` — add coverage for the bundled fallback

Action:
- [ ] `git mv providers.toml src/eggpool/providers/_templates.toml`
- [ ] `load_provider_templates(None)` reads from
  `importlib.resources.files("eggpool.providers").joinpath("_templates.toml")`.
- [ ] The `connect` group `default="providers.toml"` becomes
  `default=None`; resolve at command entry.
- [ ] Add a unit test that asserts all nine providers in
  `_templates.toml` are visible after `load_provider_templates(None)`
  in a fresh install.
- [ ] Keep `config-examples/` and other user-facing copy untouched.

Validation gate: `uv run pytest tests/unit/test_connect.py` passes; a
fresh wheel install in a clean venv shows all nine providers via
`eggpool connect list`.

### 2.4 Drop the unused `jinja2` dependency and the empty templates dir (#4)

`jinja2` is declared at `pyproject.toml:15` but is not imported anywhere
in `src/eggpool/` or `scripts/`. `src/eggpool/dashboard/templates/` is an
empty directory. Both are dead weight and confusing.

Files:
- `pyproject.toml` — remove the `jinja2` line
- `uv.lock` — regenerated by `uv lock`
- `src/eggpool/dashboard/templates/` — `rmdir` (or leave with a
  `.gitkeep` only if jinja2 is intended for future use; the audit
  recommendation is to remove)

Action:
- [ ] Remove `"jinja2",` from `dependencies` in `pyproject.toml:15`.
- [ ] `rmdir src/eggpool/dashboard/templates/`.
- [ ] `uv lock` to refresh the lockfile.
- [ ] `uv sync --frozen --extra dev` to confirm the lockfile is still
  consistent.

Validation gate: `uv run pyright src/ scripts/` still passes; the
wheel no longer contains `jinja2` in its `requires-dist`:

```bash
unzip -p dist/eggpool-0.1.0-py3-none-any.whl \
  "eggpool-0.1.0.dist-info/METADATA" | grep jinja2
```

Expected: no output.

### 2.5 Add `LICENSE` (#5)

`pyproject.toml:7` declares `license = "MIT"` but no `LICENSE` file
exists at the repo root. `twine check` and downstream packagers
require it.

Files:
- `LICENSE` (new)

Action:
- [ ] Add `LICENSE` containing the standard MIT text with copyright
  `Copyright (c) 2026 David Bowman` and the year updated to the current
  release year.
- [ ] Confirm the SPDX expression at `pyproject.toml:7` (`"MIT"`) still
  matches — no change needed because PEP 639 accepts the SPDX form.

Validation gate: `twine check dist/*` reports no license warnings (see
step 7.3 for the `twine` install).

---

## 3. Metadata — improve the PyPI project page

### 3.1 Add classifiers, keywords, project URLs, author email (#9)

`pyproject.toml:1-18` is missing fields the PyPI project page renders
prominently.

Files:
- `pyproject.toml` — extend `[project]`

Action: replace the existing `[project]` block with:

```toml
[project]
name = "eggpool"
version = "0.1.0"
description = "A lightweight proxy that aggregates multiple LLM provider accounts behind one OpenAI-compatible endpoint"
readme = "README.md"
requires-python = ">=3.11"
license = "MIT"
authors = [{ name = "David Bowman", email = "dbowman91@proton.me" }]
keywords = [
    "llm",
    "proxy",
    "openai",
    "anthropic",
    "aggregation",
    "router",
    "multi-account",
    "opencode",
]
classifiers = [
    "Development Status :: 4 - Beta",
    "Framework :: AsyncIO",
    "Framework :: FastAPI",
    "Intended Audience :: System Administrators",
    "License :: OSI Approved :: MIT License",
    "Operating System :: POSIX :: Linux",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3 :: Only",
    "Topic :: Internet :: Proxy/Mixin",
    "Topic :: System :: Monitoring",
    "Typing :: Typed",
]
dependencies = [
    "fastapi",
    "granian",
    "httpx",
    "aiosqlite",
    "pydantic>=2.0",
    "click",
    "pproxy>=2.7.9",
]

[project.urls]
Homepage = "https://github.com/eggstack/eggpool"
Repository = "https://github.com/eggstack/eggpool"
Issues = "https://github.com/eggstack/eggpool/issues"
Documentation = "https://github.com/eggstack/eggpool/tree/main/docs"
Changelog = "https://github.com/eggstack/eggpool/blob/main/CHANGELOG.md"

[project.optional-dependencies]
dev = [
    "ruff",
    "pyright",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "respx",
    "coverage[toml]",
    "pre-commit",
    "httpx2>=2.4.0",
]

[project.scripts]
eggpool = "eggpool.cli:main"
```

Validation gate: `uv run python -c "import tomllib;
print(tomllib.loads(open('pyproject.toml').read())['project'])"` prints
the new fields without error.

### 3.2 Add `CHANGELOG.md` (#8)

Files:
- `CHANGELOG.md` (new)

Action: create `CHANGELOG.md` with a `# Changelog` heading and at least:

```markdown
# Changelog

All notable changes to EggPool are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - YYYY-MM-DD

### Added

- Multi-provider aggregation across OpenAI- and Anthropic-compatible
  upstreams with quota-aware routing.
- SQLite-backed request, token, latency, error, and cost statistics.
- Multi-page HTML dashboard (overview, accounts, models, latency, pings,
  events, timeseries, bandwidth) with 50+ Halloy themes.
- CLI commands: `serve`, `check-config`, `migrate`, `onboard`,
  `connect`, `connect list`, `logout`, `accounts list`,
  `accounts status`, `models refresh`, `db vacuum`, `dashboard public`,
  `rehash`, `restart`, `stop`, `update`, `getkey`, `newkey`, `edit`,
  `configsetup opencode`, `configsetup claude-code`, `set`, and the
  `deploy` group (`systemd`, `logrotate`, `cron`, `all`).
- Operational scripts: `install.sh`, `install_prompt.py`,
  `check_database.py`, `smoke_test.py`, `verify_upstream_auth.py`.

### Notes

- See the README and `docs/deployment.md` for install, configuration,
  and deployment.
```

The release date is filled in at tag time.

Validation gate: `CHANGELOG.md` is referenced from `pyproject.toml`
(`project.urls.Changelog`) and from `README.md` (a one-line "see
[CHANGELOG](CHANGELOG.md) for release history").

### 3.3 README badges and pip-first install instructions (#10, #11, #13, #14)

`README.md` lacks badges, and the existing install paths all assume a
git-clone flow. After publishing to PyPI, `pipx install eggpool` should
be the first option.

Files:
- `README.md`

Action:
- [ ] Insert a badge block at the top of `README.md:1-3`:

  ```markdown
  [![PyPI version](https://badge.fury.io/py/eggpool.svg)](https://pypi.org/project/eggpool/)
  [![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
  [![CI](https://github.com/eggstack/eggpool/actions/workflows/ci.yml/badge.svg)](https://github.com/eggstack/eggpool/actions/workflows/ci.yml)
  ```

- [ ] Add a Chart.js credit alongside the Halloy credit
  (`README.md:13`):

  ```markdown
  - 50+ themes from [Halloy](https://github.com/squidowl/halloy) and
    [Chart.js](https://www.chartjs.org/) v4 (MIT) for dashboard charts
  ```

- [ ] Reorder the install options so that **Option 1: pipx install**
  is first:

  ```markdown
  ### Option 1: pipx install (recommended)

  ```bash
  pipx install eggpool
  pipx run eggpool --help   # or just `eggpool --help` if PATH includes it
  ```

  `pipx` installs `eggpool` into its own venv and exposes the
  `eggpool` console script globally. The bundled themes and
  provider templates ship inside the package — no extra files
  required to start.

  Then copy and edit configuration:

  ```bash
  cp /path/to/your/eggpool-venv/lib/python*/site-packages/eggpool/_share/config.example.toml ~/.config/eggpool/config.toml
  ```

  (We'll add a `eggpool init-config` helper in 0.1.1 that writes the
  example into the current directory; tracked separately.)
  ```

  Note: this option is sketched for the plan and must be paired with
  step 4.1 (config example copy helper) or revised if the helper is
  deferred.

- [ ] Reorder the existing "Option 1: Automated install (curl-piped
  install.sh)", "Option 2: Manual install (uv sync)", and "Option 3:
  Interactive setup" so they become Options 2, 3, 4.

Validation gate: `README.md` mentions `pipx install eggpool` in the
first ~30 lines and links to `CHANGELOG.md` and `LICENSE`.

---

## 4. Install / UX — make pip installs first-class

### 4.1 Bundle `config.example.toml` inside the package

`config.example.toml` is at the repo root and is referenced from the
README, the install script, and the deployment docs. A pip-installed
user has no easy way to obtain a copy.

Files:
- `config.example.toml` → copy to `src/eggpool/_share/config.example.toml`
  (or a single `_share/` directory containing both `config.example.toml`
  and `.env.example`)
- `src/eggpool/cli.py` — add an `eggpool init-config` command that
  copies the bundled example into the current directory
- `tests/unit/test_cli.py` (or a new `tests/unit/test_init_config.py`)
  — coverage for the new command
- `README.md` — point users at `eggpool init-config` after `pipx
  install eggpool`

Action:
- [ ] Create `src/eggpool/_share/` containing `config.example.toml`
  and a minimal `.env.example` (move the existing root-level
  `.env.example` content).
- [ ] Add to `src/eggpool/cli.py`:

  ```python
  @cli.command("init-config")
  @click.argument("target", required=False, type=click.Path())
  @click.pass_context
  def init_config(ctx: click.Context, target: str | None) -> None:
      """Write config.example.toml into the current directory (or TARGET)."""
      ...
  ```

  The implementation uses
  `importlib.resources.files("eggpool._share").joinpath("config.example.toml")`
  and writes it to `target or "config.toml"` (with a `--force` flag if
  the file already exists).
- [ ] The root-level `config.example.toml` and `.env.example` stay —
  they're still the source of truth for source-checkout users and for
  the `install.sh` script.

Validation gate: `eggpool init-config` in a clean dir writes
`config.toml`; the new file is byte-identical to
`src/eggpool/_share/config.example.toml`.

### 4.2 Update `scripts/install.sh` to prefer PyPI (#11)

`scripts/install.sh` is git-clone-only. The new flow should:

1. Detect a pre-installed `eggpool` (`command -v eggpool`).
2. If present, skip the clone/sync and validate the config.
3. Otherwise, prefer `pipx install eggpool` (or fall back to the
   existing git-clone flow).

Files:
- `scripts/install.sh`

Action:
- [ ] Add at the top of the install script, after the Python check:

  ```bash
  if command -v eggpool >/dev/null 2>&1; then
      echo "Existing eggpool install detected: $(command -v eggpool)"
      echo "Using existing install. Run 'eggpool update' to upgrade."
      eggpool version
      exec eggpool accounts status
  fi

  if command -v pipx >/dev/null 2>&1; then
      echo "Installing eggpool via pipx..."
      pipx install eggpool
      echo "Installation complete. Run 'eggpool init-config' to start."
      exec pipx run eggpool accounts status
  fi
  ```

- [ ] Keep the existing `git clone` flow as the final fallback.
- [ ] Re-validate the `install_prompt.py` flow against the new branch
  in `tests/unit/test_install_prompt.py`.

Validation gate: `tests/unit/test_install_prompt.py` passes; a manual
smoke run on macOS (`./scripts/install.sh` with no `eggpool` on PATH)
ends at "Installation complete".

### 4.3 Fix `eggpool update` to use PyPI (#12)

`cli.py:1010-1085` queries GitHub releases and runs
`pip install git+https://github.com/eggstack/eggpool.git@<tag>`. For
pip-installed users this reinstalls the git source over the wheel.

Files:
- `src/eggpool/cli.py` — `update` command

Action:
- [ ] Query `https://pypi.org/pypi/eggpool/json` for the latest version
  instead of GitHub releases. Use `importlib.metadata.version("eggpool")`
  to determine the running version.
- [ ] For pip-installed users, run `pip install --upgrade eggpool` (or
  `pipx upgrade eggpool` when invoked under pipx, detected via
  `sys.prefix`).
- [ ] For source-checkout users (no installed metadata, or
  `eggpool` resolves to the in-tree module), keep the existing
  `git pull && uv sync` flow.
- [ ] Add a `--from-source` flag that forces the git path; document
  this in `--help`.
- [ ] Update `tests/unit/test_cli.py` (or `test_update.py`) to mock
  PyPI's JSON response and assert the correct install command.

Validation gate: `uv run pytest tests/unit/ -k "update or pypi"`
passes; `eggpool update --check` against a fake PyPI response reports
"update available" without doing the install.

### 4.4 Add deployment docs for the pip install path (#10)

Files:
- `docs/deployment.md`
- `docs/raspberry-pi.md`

Action:
- [ ] Prepend a "Quick install (pipx)" section to each, with a `pipx
  install eggpool` flow that:
  - Creates `/etc/eggpool` and `/var/lib/eggpool` for systemd layout
    (existing path).
  - Generates a server API key via `eggpool newkey` and writes it to
    `/etc/eggpool/config.toml` via `eggpool init-config --target
    /etc/eggpool/config.toml --force`.
  - Drops a one-line `pipx` install reminder at the top.
- [ ] Keep the git-clone path for users who want source-modifiable
  installs.

Validation gate: a manual walkthrough from a clean Debian VM
reaches "server up" in under 10 minutes from the pipx path.

### 4.5 Drop `httpx2` from dev dependencies if unused

`pyproject.toml:30` declares `"httpx2>=2.4.0"` in dev dependencies.
`httpx2` does not appear in any source/test file. It was added in
commit `0f31ddc` ("Add httpx2 to dev dependencies") with no follow-up
usage.

Files:
- `pyproject.toml`
- `uv.lock`

Action:
- [ ] Remove `"httpx2>=2.4.0",` from `[project.optional-dependencies].dev`.
- [ ] `uv lock`.
- [ ] `uv sync --frozen --extra dev` to confirm the lockfile is still
  consistent.

Validation gate: `uv run pytest` still passes after removal.

---

## 5. Housekeeping

### 5.1 Fix the CI coverage target (#6)

`./.github/workflows/ci.yml:51` runs `pytest --cov=go_aggregator`.
That module no longer exists; coverage will silently be empty.

Files:
- `.github/workflows/ci.yml`

Action:
- [ ] Change `--cov=go_aggregator` to `--cov=eggpool`.

Validation gate: a CI run on a test branch shows coverage percentages
matching the local `coverage run -m pytest && coverage report` output.

### 5.2 De-duplicate the placeholder key list (#17)

`src/eggpool/auth.py:56-64` and `src/eggpool/models/config.py:316-324`
both define a `_placeholder_keys` frozenset. Drift risk.

Files:
- `src/eggpool/constants.py` — add the canonical list
- `src/eggpool/auth.py` — import the constant
- `src/eggpool/models/config.py` — import the constant

Action:
- [ ] Move the frozenset to `src/eggpool/constants.py`:

  ```python
  PLACEHOLDER_API_KEYS: frozenset[str] = frozenset(
      {
          "your-proxy-api-key",
          "your-opencode-go-key-1",
          "your-opencode-go-key-2",
          "your-api-key-here",
          "your-local-api-key-here",
      }
  )
  ```

- [ ] Replace the two local definitions with
  `from eggpool.constants import PLACEHOLDER_API_KEYS`.
- [ ] Add `tests/unit/test_constants.py` if it doesn't exist (or extend
  an existing constants test) to assert the set contains the expected
  entries.

Validation gate: `uv run pytest tests/unit/ -k "placeholder or auth"`
passes; pyright remains clean.

### 5.3 Drop the stale `plans/` reference in the phase-17 test docstring (#19)

`tests/integration/test_phase17_deployment_readiness_matrix.py:4`
references `plans/phase-17-deployment-readiness-corrections.md`. The
`plans/` directory exists but is empty.

Files:
- `tests/integration/test_phase17_deployment_readiness_matrix.py`

Action:
- [ ] Reword the docstring to remove the path reference and instead
  cite the commit hash or the audit document, e.g.:

  ```python
  """Phase 17 deployment-readiness regression matrix.

  This file exercises the cross-cutting scenarios called out in the
  Phase 17 deployment-readiness audit. Each test class is named after
  a matrix letter so the matrix itself is auditable from a single
  file.

  The matrix:
  ...
  """
  ```

Validation gate: `grep -rE "plans/phase-17" tests/` returns no results.

---

## 6. Final validation

Run, in order, with the working tree at `release/0.1.0` head:

```bash
# 1. Lint, format, type-check
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/

# 2. Full test suite with coverage
uv run pytest --cov=eggpool --cov-report=term-missing

# 3. Clean build
rm -rf dist/ src/*.egg-info/
uv run python -m build --outdir dist/
ls -la dist/

# 4. twine sanity check
uv run twine check dist/*

# 5. Wheel content audit
unzip -l dist/eggpool-0.1.0-py3-none-any.whl | grep -E "dashboard/themes|providers/_templates|LICENSE" || echo "WARN: missing bundled asset"
unzip -p dist/eggpool-0.1.0-py3-none-any.whl "eggpool-0.1.0.dist-info/METADATA" | head -40

# 6. sdist content audit
mkdir -p /tmp/sdist-final && tar -xzf dist/eggpool-0.1.0.tar.gz -C /tmp/sdist-final
du -sh /tmp/sdist-final/eggpool-0.1.0
test -d /tmp/sdist-final/eggpool-0.1.0/.opencode && echo FAIL || echo OK
ls /tmp/sdist-final/eggpool-0.1.0

# 7. Install in a clean venv
python3.11 -m venv /tmp/eggpool-install
/tmp/eggpool-install/bin/pip install --upgrade pip
/tmp/eggpool-install/bin/pip install dist/eggpool-0.1.0-py3-none-any.whl
/tmp/eggpool-install/bin/eggpool version
/tmp/eggpool-install/bin/eggpool --help
/tmp/eggpool-install/bin/eggpool connect list   # expects 9 providers
/tmp/eggpool-install/bin/eggpool accounts status   # expects "no accounts configured" (clean install)
```

Expected:
- All linters clean.
- All tests pass; coverage is non-zero on `eggpool/`.
- `dist/` contains exactly `eggpool-0.1.0-py3-none-any.whl` and
  `eggpool-0.1.0.tar.gz`.
- `twine check` reports no errors.
- Wheel contains `dashboard/themes/*.toml` and `providers/_templates.toml`.
- Sdist is < 2M unpacked and has no `.opencode/` or `__pycache__/`.
- `eggpool version` prints `0.1.0`; `eggpool connect list` shows all
  nine providers; `eggpool accounts status` exits 0 with the
  "no accounts configured" message.

---

## 7. Tag and publish

### 7.1 Cut the release commit and tag

Files: none (commit only).

Action:
- [ ] `git checkout -b release/0.1.0`
- [ ] Bump the version if it changed during polish:
  `grep '^version' pyproject.toml` and `src/eggpool/__init__.py:5`
  must agree on `0.1.0`.
- [ ] `git commit --allow-empty -m "chore: release 0.1.0"` (only if
  the version bump is needed; otherwise the previous commit is the
  release).
- [ ] `git tag -a v0.1.0 -m "EggPool 0.1.0"` (use the release date in
  `CHANGELOG.md`).
- [ ] `git push origin release/0.1.0` and open a PR for review.

Validation gate: CI passes on the `release/0.1.0` branch head.

### 7.2 Build the final artefacts

Action:
- [ ] On the tagged commit, run `uv run python -m build` in a clean
  clone and keep the resulting `dist/` directory for upload.

### 7.3 Upload to PyPI

Action:
- [ ] `uv pip install twine` if not already installed.
- [ ] `twine check dist/*` (the same gate as step 6).
- [ ] Test PyPI first (recommended for the first upload):
  `twine upload --repository testpypi dist/*` and verify the project
  page renders correctly (badges, classifiers, README, project URLs).
- [ ] If test looks good, `twine upload dist/*`.
- [ ] Capture the project URL
  (`https://pypi.org/project/eggpool/`) and the new version's URL
  (`https://pypi.org/project/eggpool/0.1.0/`) for the GitHub release.

Validation gate: `pip install eggpool` from a clean venv installs
0.1.0; `pip install --upgrade eggpool` is a no-op; the PyPI project
page renders the README, classifiers, and project URLs.

### 7.4 Cut the GitHub release

Files: none (GitHub UI).

Action:
- [ ] Create a GitHub release for tag `v0.1.0` on
  `eggstack/eggpool`.
- [ ] Title: `EggPool 0.1.0`.
- [ ] Body: paste the `## [0.1.0] - …` section from `CHANGELOG.md`.
- [ ] Attach `dist/eggpool-0.1.0-py3-none-any.whl` and
  `dist/eggpool-0.1.0.tar.gz`.

Validation gate: the release page renders; downloads succeed.

---

## 8. Post-release follow-ups (deferred — do not block 0.1.0)

These are improvements that would polish the next release but are out
of scope for shipping 0.1.0:

- [ ] `eggpool init-config` improvements: read bundled templates via
  `importlib.resources`, support `--force`, support `--target
  /etc/eggpool/config.toml` for the systemd layout.
- [ ] `eggpool update` UX: progress bar, `--dry-run`, `pipx upgrade`
  detection.
- [ ] Move `bugs.md` from a local file to GitHub Issues and drop the
  file from the working tree entirely.
- [ ] Add a `docs/migrating-from-gorouter.md` for users coming from
  the pre-rename `opencode-go-aggregator` or `gorouter` packages.
- [ ] Sign releases with `sigstore` (`uv run python -m build` +
  `gh attestation`).

---

## 9. Open questions for the user

- [ ] Is the multi-provider dispatch bug in `bugs.md` fixed on `main`
  or still pending? (#18 — gates 0.1.0.)
- [ ] `eggpool init-config` in step 4.1: ship in 0.1.0, or defer to
  0.1.1 and keep the README instructing users to copy the bundled
  template via a one-liner that uses `python -c "import
  importlib.resources; …"`? The plan above ships the helper.
- [ ] Author email to publish: `dbowman91@proton.me` is in git
  history; confirm or supply a public-facing email for the PyPI
  author field.
- [ ] Should the deploy service file at `deploy/eggpool.service`
  continue to assume a `/opt/eggpool/.venv/bin/eggpool` path, or
  should the systemd unit be updated for a pipx install? (Current
  unit only works for source-checkout users.)
