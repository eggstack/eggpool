# P006 — Rust-Only Qualification and M12 Closure

Status: dependency-ready after accepted P005 closure

Source roadmap: `migration-rs/subsystems/python-retirement-roadmap.md`

Primary class: invariant/polish

Hard dependencies: accepted P001-P005

## Objective

Prove that the post-retirement repository, package, installer, updater, deployed runtime and retained historical-version contract work without the Python application source or live Python oracle, then close M12 only if no high/medium retirement finding remains.

P006 is qualification/closure, not another cleanup implementation tranche. Any defect discovered here receives a new corrective P-plan.

## Required source-tree assertions

The accepted post-retirement tree must prove:

- no `src/eggpool/` current application tree exists;
- no current production/release path imports or launches the historical application;
- `packaging/pypi/pyproject.toml` is the sole current publication manifest;
- retained Python files are tooling/fixtures only and are named by P001/P004 disposition evidence;
- all runtime migrations/assets have Rust-owned or neutral retained authorities;
- historical source identity is recoverable from immutable Git history and recorded in the M12 reference manifest.

## Rust build and package qualification

Build the current Rust binary and the complete supported production wheel/raw artifact set from the post-retirement tree using the same target policy as M11:

- Linux x86_64;
- Linux aarch64;
- macOS arm64.

Use hosted target builders or an equivalent existing release `validate` workflow when the local host cannot build a target natively. No public publish is required.

For every wheel prove:

- correct package/version/target metadata;
- native `eggpool` executable present;
- no Python application module/package payload;
- no current sdist fallback;
- hashes/manifest generated deterministically;
- installed `eggpool version`, `help`, `check-config`, foreground health/readiness and bounded dashboard checks pass;
- normal process tree contains no Python child created by EggPool.

## Database/config/recovery qualification

Use a representative preserved state to prove:

- schema-54 database opens and passes integrity checks;
- no new migration/checksum drift was introduced by retirement;
- config bytes/paths remain stable;
- backup and recover remain valid;
- stop/restart/rehash/runtime-status remain operational;
- abrupt restart reconciliation remains bounded and does not replay provider work.

Reuse fresh M9/M10 evidence where source-freshness is defensible, but rerun any gate whose owner/path changed during P002-P005.

## Historical exact-version qualification

M12 closure must deliberately prove that pure-Rust current source retirement did not break the user-requested cross-era version feature.

At minimum on Linux x86_64 with a package-managed installation:

```text
public compatible Python historical release
  -> post-retirement Rust candidate wheel
  -> same compatible Python historical release
  -> post-retirement Rust candidate wheel
```

Use an accepted schema-54-compatible historical target such as `0.7.4` unless the catalog at execution time identifies a newer final Python reference. Exercise uv-tool and at least one of pipx or isolated pip. Preserve config and DB integrity throughout.

Also prove:

- `eggpool update` latest/current selection is Rust-only;
- an incompatible older Python target fails before mutation;
- standalone Rust rejects Python target;
- manager metadata/ownership remains coherent after every leg;
- no repository-local Python source is used to perform the transition.

Historical public wheels may be downloaded from PyPI; they are external immutable evidence, not a retained current implementation.

## Release and installer integrity

Run the production release validators and a non-publishing complete artifact rehearsal. Verify Trusted Publishing/recovery workflow structure remains valid after Python cleanup and that current docs/install commands resolve the Rust package channel.

Do not publish a new release merely to close M12. If maintainers independently choose to publish the first post-retirement release during P006, record it as additional evidence but do not make external publication a hidden prerequisite unless the registry is explicitly updated first.

## Security and dependency review

Confirm:

- no production secret or provider credential is introduced into retained fixtures;
- no deleted Python dependency is still implicitly required by runtime/service startup;
- package-manager commands remain fixed argv with bounded output/timeouts;
- unsupported platform behavior remains fail-closed;
- current wheel dependency metadata contains no Python application dependencies;
- release OIDC permissions remain narrowly scoped;
- no unsafe source-build fallback or hidden Python runtime fallback exists.

## Required verification

At minimum record exact versions/results for:

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk cargo build --manifest-path rust/Cargo.toml --locked --release
rtk uv run pytest <retained-python-tooling-tests> -q --tb=short --maxfail=1
rtk uv run python scripts/check_cutover_catalog.py
rtk uv run python scripts/validate_cutover_docs.py
rtk uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
rtk git diff --check
```

If the known M11 Clippy baseline remains non-zero, P006 must either fix it or explicitly distinguish pre-existing unrelated findings from retirement-introduced warnings with a reviewed disposition. Do not silently drop `-D warnings` from the required gate.

Run the post-retirement package/artifact and cross-era transition qualification described above and store bounded machine-readable reports under `migration-rs/closure/retirement/`.

## Closure review

The P006 closure record must summarize:

- final source commit and P001 reference commit;
- files/classes removed and retained tooling footprint;
- supported production artifact matrix;
- package-manager historical transition results;
- DB/config/backup/restart results;
- source-freshness decisions for retained M10/M11 evidence;
- release/installer/workflow verification;
- security/dependency findings;
- unresolved low-severity maintenance items;
- explicit registry transition.

## M12 acceptance criteria

M12 closes only when all of the following are true:

1. Current EggPool production/runtime and publication source are Rust-only.
2. The Python application source and live oracle are absent from the active production/test dependency graph.
3. Useful historical fixtures/provenance remain auditable without duplicating the full application.
4. Current supported wheels build/install/run from the post-retirement tree.
5. Config/database/migrations/backups/restart semantics remain compatible.
6. Explicit compatible historical Python exact-version transitions still work through package-manager ownership, while latest/default behavior remains Rust-only.
7. No supported current command/workflow silently invokes the retired Python application.
8. No unresolved high/medium packaging, compatibility, security, lifecycle, evidence-loss or data-loss finding remains.
9. Registry/roadmap/closure records agree on the final state.

On acceptance, update `migration-rs/registry.md` to mark P001-P006 closed and M12 closed. No further migration milestone is auto-created; subsequent work returns to ordinary project roadmaps/maintenance.

## Non-goals

- no feature redesign;
- no target expansion;
- no requirement to eliminate Python as a repository tooling language;
- no deletion/yanking of historical public packages;
- no database schema change;
- no broad performance optimization unrelated to retirement.
