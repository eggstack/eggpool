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
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest \
  -m "not slow and not performance and not soak and not extended_soak and not live and not network" \
  -q --tb=short --maxfail=1
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

## Risk-Based SBC Validation

Run the target-device runtime validation when:

- Request-path, streaming, database, writer, reload, concurrency, or dependency changes: **run it**
- Documentation-only or metadata-only release: **not required**
- Uncertainty: run the short profile on representative hardware

```bash
uv run python scripts/run_dispatch_stability_soak.py \
  --profile sbc-reference \
  --duration-seconds 300 \
  --seed 42 \
  --output /tmp/eggpool-runtime-validation.json
```

`--output` names a single JSON file, not a directory. The output is written
atomically and contains:

- `passed` / `failure_reasons` for pass/fail gating
- `process.eggpool_pid`, `process.rss_start_bytes`, `process.rss_end_bytes`,
  `process.rss_peak_bytes` — RSS is measured from the Eggpool child process,
  not the runner. On Linux, `/proc/<pid>/status` VmRSS is parsed; on macOS/BSD,
  `ps -o rss=` is used. Both report bytes (KiB * 1024).
- `early` / `late` window metrics (throughput, latency percentiles,
  success counts, error counts, observed error rate)
- `gates` with structured per-criterion sections:
  - `workload` — useful-work gate (per-window attempts/successes/errors,
    configured error rate, allowed error fraction, dual-shape coverage)
  - `throughput` — late/early RPS ratio
  - `dispatch_p95` / `dispatch_p99` — direct late/early ratio caps
    (`ratio <= ratio_limit`). Recommended caps: `1.50` (p95) and `2.00`
    (p99) for short validation; `1.30` and `1.80` for longer runs.
  - `quiescence` — bounded post-load drain observation
    (drained, attempts, elapsed_seconds, pending_requests,
    active_reservations, failure_reason). Final drain state is read from
    this observation, not from `metrics[-1]`.
  - `rss` — RSS availability / required gate
  - `database_audit` — offline SQLite lifecycle invariants
- `polling` for bounded dashboard polling diagnostics

Empty latency windows, non-positive early baselines, missing runtime data,
zero attempts, and zero successes all fail closed. Zero-error profiles
reject any unexpected request error. Configured-error profiles tolerate
only `min(0.25, expected_error_rate + 0.10)`. `sbc-reference` at ≥60s
requires both streaming and non-streaming successes.

Unavailable metrics are reported as `null`, never as zero. A required metric
that is unavailable causes the run to fail with a descriptive reason.

GitHub workflow timing is diagnostic only — representative SBC output is
authoritative for target performance claims.
