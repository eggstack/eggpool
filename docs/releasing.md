# Manual Release Procedure

EggPool uses manual release publication. There is no automated release workflow.

## Preconditions

- Clean working tree on `main`
- Current `main` fetched from remote
- Version set in `pyproject.toml` is greater than the latest published version
- Changelog or release notes prepared if applicable
- Before-push check passes (see below)

## Before-Push Check

```bash
uv lock --check
uv sync --frozen --extra ci
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1

# Keep the richer local developer environment healthy too.
uv sync --frozen --extra dev
uv build
```

## Build

```bash
rm -rf dist/
uv build
```

Verify wheel and source distribution exist in `dist/`.

## Clean-Artifact Smoke

Test the built wheel in an isolated environment:

```bash
TMP_VENV="$(mktemp -d)/venv"
uv venv "$TMP_VENV"
uv pip install --python "$TMP_VENV/bin/python" dist/*.whl
cd "$(mktemp -d)"
"$TMP_VENV/bin/python" -c "import eggpool"
"$TMP_VENV/bin/eggpool" --help
```

Create a minimal valid config file and run:

```bash
"$TMP_VENV/bin/eggpool" check-config --config /path/to/minimal-config.toml
```

The smoke must prove import and CLI execution from the built wheel outside the repository directory.

## Publish

```bash
uv publish
```

Use token or keyring configuration. Publishing must be an explicit operator action.

## Tag and GitHub Release

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

Then create a GitHub release from the tag.

**Important:**
- Package index releases are immutable
- A failed or incomplete release requires a new version bump
- Never force-reuse an already published version
- Command failures must stop the process — do not mask with `|| true`

## Optional target-device validation

For request-path, streaming, database, reload, concurrency, or dependency
changes, a short run on representative SBC hardware is useful when available.
Exercise one ordinary request, one canonical stream, one premature-EOF stream,
and confirm that active reservations return to zero after the run. Use the
focused smoke suite first; do not treat a fixed-duration soak, timing
percentage, JSON evidence file, or unavailable metric as a release gate.

For a stream-specific reproducer, use:

```bash
uv run python scripts/repro_high_concurrency_streams.py --help
```
