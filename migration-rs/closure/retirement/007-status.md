# M12-P007 Closure — Provider Transport Fixture Determinism and M12 Requalification

Status: accepted/closed 2026-09-11

Plan: [P007 — Provider transport fixture determinism and M12 requalification](../../implementation/retirement/007-provider-transport-fixture-determinism-and-m12-requalification.md)

## Decision

P007 is accepted and restores current M12 closure authority. P006 remains
append-only historical evidence for its local qualification; this record
supersedes it only as the final M12 closure authority because it adds the
hosted-CI correction and successful hosted qualification required after P006.

## Implementation and root cause

The baseline was `03f022dfe5aa55cea1e8d7a078afe194eff9ed0a`. Corrective
implementation commits were:

- `b0ad7e55210f8117576fafb8fb6d44afce59491a` — make the HTTP fixture accept
  and serve connections independently, retain exact request/connection
  accounting, and add bounded-shutdown coverage;
- `252341e5af0d012d7852a5ff02f90d1cf6e8ea0a` — make the bounded-output test
  producer deterministic on Linux;
- `967184bb7b7a8d84358000b87638c8b0138bea25` — preserve bounded manager-output
  errors when child completion wins the async selection race.

The P007 triggering defect is classified as a **fixture defect**. The
provider transport fixture accepted one upstream socket and synchronously
read its keep-alive connection before returning to `accept()`. A second
account connection using the same proxy URI could therefore remain pending
until the client read timeout. The fixture now gives each accepted socket an
independent bounded worker while retaining the existing keep-alive request
loop, exact observations, deterministic worker joins, and bounded idle/read
teardown. The focused pool regression keeps the first account client alive
while the second account request progresses, so it would fail under the old
serial accept/keep-alive model.

The first hosted retry also exposed a separate pre-existing Linux-only
manager-output test race. The test producer and the narrow updater error
propagation path were corrected; no provider, routing, retry, schema,
packaging, or release semantics were changed. The known strict-Clippy
baseline remains unchanged.

Exact implementation files changed:

- `rust/tests/provider_transport.rs`
- `rust/src/operations/update.rs`

## Qualification evidence

The two isolation regressions passed the required bounded stress gate: 20/20
repetitions each, zero failures, with no assertion retries. The complete local
`provider_transport` suite passed 30 tests sequentially. The new
`fixture_shutdown_is_bounded_when_request_never_arrives` regression passed,
and the existing keep-alive reuse test remained green. Proxy target
observations and upstream connection counts remained exact in the account
isolation tests.

Final local verification passed:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
  30 passed
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
  469 passed across 52 suites
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
  reviewed pre-existing baseline: 66 errors and 1 warning; no P007 delta
rtk uv sync --frozen
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
  76 passed
rtk uv run pytest tests/tooling/test_k005_transitions.py tests/tooling/test_k007_deployed_transition.py -q --tb=short --maxfail=1
  6 passed
rtk uv run ruff format --check scripts/ tests/tooling/
  42 files already formatted
rtk uv run ruff check scripts/ tests/tooling/
  all checks passed
rtk uv run pyright scripts/
  0 errors, 0 warnings, 0 informations
rtk uv run python scripts/check_cutover_catalog.py
  K001 catalog valid: 57 releases; 8 rollback-compatible
rtk uv run python scripts/validate_cutover_docs.py
  pass; 7 docs; 3 targets
rtk uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
  pass; 35 actions; 9 jobs; 3 targets
rtk uv run python scripts/validate_m12_package_boundary.py --workflow .github/workflows/release.yml
  pass; Rust current runtime; packaging/pypi/pyproject.toml authority
rtk uv run python scripts/validate_m12_retirement.py
  pass; 7 assets; 54 migrations
rtk git diff --check
  pass
```

Because the follow-up touched the manager-transition implementation, the
corresponding P006 boundary was refreshed with the final all-target Rust run
and the retained K005/K007 transition tests. P006 artifact, schema, runtime-
asset, and cross-era evidence remains reusable where its ownership paths were
unchanged; no new artifact matrix or public release is claimed by P007.

## Hosted CI gate

Successful hosted GitHub Actions run:

- run ID: `34638923259`
- URL: [main CI run 34638923259](https://github.com/eggstack/eggpool/actions/runs/34638923259)
- head SHA: `967184bb7b7a8d84358000b87638c8b0138bea25`
- conclusion: `success`
- completed: `2026-09-11T19:34:38Z`
- `cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1`: passed;
  the `provider_transport` target ran 30 tests with 0 failures;
- frozen uv sync, Ruff format/check, Pyright, and `tests/tooling/` pytest:
  all passed (`73 passed, 3 skipped` in hosted pytest).

The two earlier hosted runs on the same corrective lineage are retained as
diagnostic evidence: run `34637886330` identified the Linux manager-output
race, and run `34638337597` confirmed it was deterministic before the updater
correction. Neither is used as acceptance evidence.

## Findings and future-plan audit

No unresolved high- or medium-severity transport, account-isolation,
packaging, retirement, security, lifecycle, or data-loss finding remains.
Known low-severity maintenance items are unchanged: the repository-wide
strict-Clippy baseline (66 errors and one warning outside P007’s scope), the
hosted action Node.js 20 deprecation annotation, and the hosted Pyright
available-version notice. They do not affect M12 acceptance and are not
silently reclassified as P007 work.

The future-plan audit found no later migration implementation plan to unblock.
P007 is removed from the dependency-ready queue; M12 is closed after accepted
P007; no M13 plan is created. Subsequent work returns to ordinary product and
maintenance roadmaps.

## Registry transition

The registry, M12 roadmap, implementation index/handoff, retirement plan index,
and closure index now agree that P007 is accepted/closed and is the current
M12 closure authority. P001-P006 remain accepted append-only evidence, with
P006 explicitly historical rather than rewritten.
