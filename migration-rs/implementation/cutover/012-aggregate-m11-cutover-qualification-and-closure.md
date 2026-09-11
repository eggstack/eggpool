# K012 — Aggregate M11 Cutover Qualification and Closure

Status: blocked; closure review recorded 2026-09-11; pending accepted K011

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: invariant/polish

Hard dependency: accepted K011.

## Objective

Perform the final M11 acceptance review after the first public Rust-backed release. Aggregate K001-K011 and fresh M10 evidence, verify that the public package/install/update/deploy behavior is internally coherent, and decide whether Rust can now be called EggPool's canonical runtime while the Python implementation remains only as a rollback/oracle reference for M12.

K012 is not Python retirement. It must not delete `src/eggpool`, root Python test/oracle tooling, historical packaging evidence, or migration fixtures needed to verify the cutover.

## Closure question

M11 closes only if an ordinary supported user can:

1. install `eggpool` through the documented public package channel and receive the native Rust runtime;
2. use existing config/database/client/operator workflows without migration rewrite;
3. update to latest or an exact supported version without package-manager ownership corruption;
4. downgrade across the Rust/Python boundary to a catalogued compatible release;
5. return to Rust;
6. operate a deployed service through that cycle;
7. recover safely from failed installation/restart/health transitions;
8. understand unsupported targets and rollback limits from current docs.

## Part A — canonical release identity audit

Verify the accepted public release is identical across:

- Cargo version;
- installed Rust `eggpool version`;
- PyPI project/release metadata;
- Git tag and GitHub release;
- release manifest;
- K001 installable release catalog;
- README/changelog/current docs;
- updater latest target.

No stale Python package version may be presented as the canonical current release authority.

## Part B — public artifact audit

Re-fetch public metadata for the K011 release and verify:

- expected Linux x86_64 wheel exists;
- expected Linux aarch64 wheel exists;
- expected macOS arm64 wheel exists;
- no unqualified Windows/other-platform wheel exists;
- no Rust sdist exists;
- wheel hashes/tags/sizes match K011 manifest;
- wheel metadata contains no Python application dependencies;
- wheel installs native executable;
- Trusted Publishing/attestation state matches closure claim;
- GitHub raw executable set/hashes match manifest.

PyPI/GitHub source archives are not treated as supported runtime artifacts.

## Part C — public package-manager matrix

Re-run or freshness-verify public-index installation for:

- uv tool;
- pipx;
- ordinary isolated pip/venv.

Required results:

- fresh Rust install;
- package metadata version == CLI version;
- native Rust executable;
- `help`, config check, serve/health;
- uninstall/reinstall;
- latest no-op/current behavior;
- exact version behavior;
- wheel-managed install cannot invoke raw O008 replacement.

At least Linux x86_64 must exercise all three managers. Linux aarch64 and macOS arm64 must exercise the canonical manager used by public docs, plus any manager-specific target issue found during K009/K011.

## Part D — cross-era rollback requalification

Use K011's real public rollback drill as primary evidence, then run a focused independent recheck or inspect the exact artifacts/logical state to guard against a closure-record-only error.

Required invariant:

```text
Python package manager metadata/version
  -> Rust package manager metadata/version
  -> Python package manager metadata/version
  -> Rust package manager metadata/version
```

At every leg:

- config path preserved;
- DB path/integrity/migrations preserved;
- known provider/account/model/request facts retained;
- installed manager/provenance coherent;
- service state coherent if deployed;
- no stale update lock/transition journal;
- target-era executable matches catalog.

If K011 relied on a historical fallback rather than a PyPI wheel, K012 must explicitly decide whether the public promise remains acceptable and documentation accurately describes it.

## Part E — existing install migration

Qualify the most likely real user path from the public historical Python package:

- current public Python-era install using old quick-start method;
- existing config/database populated;
- run new installer or `eggpool update` as documented;
- manager remains same/adopted deterministically;
- Rust wheel becomes active without a second conflicting executable;
- systemd/cron references continue to resolve correctly;
- rollback remains available.

The transition must not require `rm -rf ~/.config/eggpool`, DB reset, or manual package-manager cleanup.

## Part F — standalone path isolation

Verify raw GitHub installation remains a secondary explicit channel and does not interfere with PyPI-managed installs.

- standalone O008 latest/exact works against public release assets;
- package-managed `eggpool update` never uses raw replacement;
- standalone-to-wheel adoption is documented/tested;
- ambiguous PATH collisions fail closed;
- production system deployment authority from K007 is consistent with docs.

## Part G — M10 regression/freshness audit

M11 changes packaging/update/installer/deployment/docs, not request semantics intentionally. Still review source changes from the accepted Q012 candidate to K011 public release and decide which M10 evidence must be rerun.

At minimum run fresh:

- Q001 manifest/contract validators affected by install/release claims;
- Q002 deterministic aggregate;
- Q003 DB compatibility/rollback if transition code changed DB handling;
- Q005 target portability build/runtime on the actual release artifacts;
- Q006 rootful deployment if service/install paths changed;
- Q012 dashboard semantic smoke from the public wheel on one supported target;
- full Rust tests;
- migration oracle tests;
- smoke tests.

Retain live-provider/Q008/Q009 evidence only with explicit source-freshness analysis; if runtime/provider/coordinator behavior changed during M11, rerun the affected bounded evidence rather than assuming it.

## Part H — dependency/runtime footprint check

Compare first Rust wheel install with final Python-era wheel environment as characterization:

- installed distribution count;
- wheel size;
- native binary size;
- process tree at serve;
- runtime Python process absence;
- idle RSS/startup characterization where easily measured.

Do not invent performance gates or delay closure over harmless percentage differences. A new Python application dependency in the Rust wheel or Python child process required for normal server operation is a correctness finding.

## Part I — security review

Review cutover-specific attack/failure surface:

- package-manager command construction/injection;
- provenance spoofing/manager ambiguity;
- direct URL/catalog mutability;
- release workflow permissions;
- Trusted Publisher scope;
- wheel/raw hash integrity;
- partial publish recovery;
- installer curl/bootstrap assumptions;
- root/systemd package-manager environment;
- update subprocess secret inheritance/logging;
- PATH collision/hijack;
- rollback to incompatible DB release;
- arbitrary version/source injection.

No high/medium security finding may remain open.

## Part J — docs/public state consistency

Verify public README, PyPI page, install script, deployment/upgrading docs and release notes all agree on:

- Rust is canonical on supported targets;
- PyPI remains primary package channel;
- supported target list;
- exact update/downgrade syntax;
- rollback compatibility window/catalog;
- standalone binary distinction;
- Python source remains reference only;
- no Windows claim;
- no source-build fallback claim.

## Part K — no Python retirement by accident

Check that K011 did not make M12 impossible:

- root Python source remains intact;
- Python test/oracle environments still run;
- final Python release/tag/commit is recorded in K001 catalog;
- differential tests can still execute the final Python reference;
- historical Python wheel/build instructions needed for rollback are retained;
- no cleanup removed migration evidence needed to investigate a cutover defect.

## Part L — finding triage

Aggregate every K001-K011 finding and classify:

- closed;
- accepted informational/characterization;
- low-risk follow-up eligible for M12/later;
- high/medium blocker.

High/medium categories include:

- package ownership corruption;
- version mismatch;
- public target missing/broken wheel;
- failed Python/Rust rollback;
- DB/config loss;
- service transition failure requiring manual repair under normal path;
- release provenance/integrity failure;
- unsupported target silently source-building;
- command injection/provenance ambiguity causing mutation;
- docs sending normal users to a broken path.

A blocker creates a new K013+ corrective plan; do not rewrite K011/K012 history to declare success.

## Required verification

Run and record exact commands for:

```text
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run pyright src/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
# K001-K011 focused aggregate
# public artifact/index verifier
# public package-manager install/rollback matrix
# applicable Q002/Q003/Q005/Q006/Q012 qualification commands
git diff --check
```

External/public evidence must identify the candidate/tag/version and sanitized target environment.

## Closure record

Write `migration-rs/closure/cutover/012-status.md` containing:

- final implementation/public release commits and tag/version;
- canonical PyPI/GitHub artifact inventory;
- public fresh-install matrix;
- exact cross-era rollback matrix;
- existing-install adoption result;
- deployed-service result;
- standalone-path isolation result;
- M10 freshness/rerun results;
- package/runtime footprint characterization;
- supply-chain/security review;
- documentation consistency review;
- complete unresolved-finding table;
- registry transition.

## Registry transition

Only an accepted K012 may:

- mark M11 closed;
- describe Rust as canonical public EggPool runtime on supported targets;
- leave dependency-ready M11 table empty;
- make M12 eligible for a **separate planning review**.

Do not auto-create or implement M12 Python removal in the K012 closure commit.

## Acceptance criteria

K012 closes M11 only when all of the following are true:

- production PyPI resolves the Rust binary wheel for every required supported target;
- public GitHub raw assets match the release manifest;
- fresh public installs execute Rust without Python runtime dependency;
- uv/pipx/pip manager ownership is coherent;
- exact Python -> Rust -> Python -> Rust switching is proven for the frozen rollback window;
- existing user config/DB/deployment state survives cutover and rollback;
- public installer/docs are Rust-default and accurate;
- unsupported targets fail cleanly with no Rust sdist fallback;
- standalone raw updates remain isolated and verified;
- Trusted Publishing/provenance/integrity controls are accepted;
- affected M10 qualification remains green/fresh;
- Python reference/oracle remains available for M12;
- no unresolved high/medium cutover, packaging, data-loss, lifecycle, security or compatibility finding remains.

Accepted K012 closes M11 and makes M12 eligible for separate planning only.

The 2026-09-11 closure review is recorded in
[`../../closure/cutover/012-status.md`](../../closure/cutover/012-status.md).
It is blocked because K011 has no public PyPI wheel publication or
package-managed public rollback evidence; the review does not authorize M11
closure or M12 planning.
