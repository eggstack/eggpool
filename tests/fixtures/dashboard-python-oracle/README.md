# Frozen Python dashboard oracle

`manifest.json` pins the final Python dashboard authority to
`c23a70961f4b7858fdb0264cfb27b7ea26a8a334`. It records all fourteen page
routes, dashboard JSON routes, JavaScript selectors/API references, static
asset hashes, and embedded themes. The four historical static asset Git blobs
must match the source plan's identities.

`captures/` contains the complete normalized DOM and JSON projection for all
fourteen pages and eight dashboard JSON routes in empty and populated states,
plus private-auth status results. `capture-manifest.json` records the fixed
source commit, sanitized synthetic SQL fixture hash, and SHA-256 for each
capture. The populated fixture exercises Unicode/HTML escaping, errors,
retries, and missing model information. The historical cache-observability
endpoint's populated-state HTTP 500 is preserved as oracle evidence.

`current-gap-report.json` and `.md` compare the strict oracle against Rust
candidate `454b60c008cc6234d9c3db3b637402cfe8d2f1d8` using the same empty,
populated, and private fixture setup. The local run recorded 50 mismatch cells
across the shared shell/API, core pages, telemetry pages, and Runtime/Cache.
Static assets and themes matched. This is diagnostic evidence for M002-M005,
not a parity claim.

`scripts/qualification_dashboard_parity.py --write-manifest` regenerates the
source and asset inventory without starting either implementation. The stable
command facade is `scripts/qualification_dashboard_parity.py`; its internal
owners are under `scripts/dashboard_parity/`: `projection.py` owns strict
projection/comparison, `oracle.py` owns manifest/capture generation,
`process.py` owns readiness and HTTP/process helpers, `fixtures.py` owns
synthetic fixture and lifecycle orchestration, `browser.py` owns screenshots
and browser checks, `report.py` owns mismatch grouping and output, and
`runner.py` composes a qualification run. `cli.py` owns argument parsing and
top-level command flow. The package uses only repository tooling dependencies
and remains outside the Rust runtime and release dependency graph.

The command writes the manifest to a temporary file and atomically replaces
the checked-in copy. Review any manifest change as an explicit oracle change;
do not run this command to bless candidate Rust behavior.

DOM projection retains ordered elements, element types, all attributes,
controls, and non-whitespace text. Insignificant whitespace runs and class
token order are normalized. JSON object keys are sorted and array order is
retained. Standalone timestamps and the labeled Runtime process/host/memory/
load/database-path/countdown values use explicit placeholders; no other
dynamic-value normalization is permitted.

The historical app is not part of current runtime or ordinary qualification.
To reproduce source-backed response captures, use an isolated detached
worktree at the pinned commit and its frozen historical development lockfile:

```sh
git worktree add --detach /tmp/eggpool-dashboard-oracle c23a70961f4b7858fdb0264cfb27b7ea26a8a334
cd /tmp/eggpool-dashboard-oracle
uv sync --frozen
cp /path/to/current/gorouter/scripts/qualification_dashboard_parity.py scripts/
.venv/bin/python scripts/qualification_dashboard_parity.py --capture-oracle /tmp/dashboard-oracle-captures
```

The output path must not exist; capture writes to a staging directory and
renames only after the complete set passes bounds. Compare the result with the
checked-in `captures/` before an explicit reviewed replacement. Do not use live
databases, provider credentials, or real request content.

These captures establish the oracle only; they do not establish parity for the
current Rust implementation. The current-gap report and projection tests are
the implementation substrate for M002-M006.

The Python source is deliberately kept out of the active repository. To run
the current candidate against it, keep the detached oracle worktree at the
pinned commit and use its frozen environment:

```sh
EGGPOOL_DASHBOARD_ORACLE_ROOT=/tmp/eggpool-dashboard-oracle \
EGGPOOL_DASHBOARD_ORACLE_PYTHON=/tmp/eggpool-dashboard-oracle/.venv/bin/python \
uv run python scripts/qualification_dashboard_parity.py
```

The populated qualification database is constructed from the canonical Rust
migration chain plus the checked-in, secret-free Q012 SQL fixture. Oracle
asset hashes come from the checked-in Rust asset manifest captured from the
pinned source.
