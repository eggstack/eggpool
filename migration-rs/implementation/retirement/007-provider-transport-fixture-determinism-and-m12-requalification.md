# P007 — Provider Transport Fixture Determinism and M12 Requalification

Status: dependency-ready corrective plan

Source roadmap: `migration-rs/subsystems/python-retirement-roadmap.md`

Primary class: invariant/corrective/polish

Hard dependencies: accepted P001-P006; current `main` at or after `b29d3fc35235c7b539d72a2f235904a9b8567f27`

Historical closure preserved: `migration-rs/closure/retirement/006-status.md`

## Objective

Correct the post-P006 qualification defect exposed by hosted CI, prove that provider/account transport isolation remains deterministic under the real test environment, and re-close M12 only after the corrected tree passes the complete required Rust/retirement qualification.

P007 is intentionally narrow. The Python-retirement implementation, package authority, schema, provider semantics, retry/finalization behavior, and cross-era version contract are not reopened unless the investigation proves that the CI failure originates in production behavior rather than the test harness.

## Triggering evidence

The P006 qualification baseline reported a locally passing Rust suite, but GitHub Actions failed on both the P005-closing tree and the final P006-closing tree in:

```text
rust/tests/provider_transport.rs
identical_proxy_endpoints_keep_account_pools_isolated
```

Observed failure:

```text
second account response: ReadTimeout
```

The current fixture creates two independent clients using the same HTTP CONNECT proxy endpoint and expects two independent physical connections. The fixture server processes accepted connections serially and emits a keep-alive response, while both its per-connection read timeout and the proxied client read timeout are approximately two seconds. This creates a plausible fixture scheduling race in which the server remains blocked reading the first keep-alive connection while the second client reaches its read timeout.

This is a root-cause hypothesis, not permission to assume the production transport is correct. P007 must distinguish fixture nondeterminism from a real account-pool isolation defect before changing code.

## Required investigation

Reproduce and classify the failure using the smallest useful surface first:

1. Run `identical_proxy_endpoints_keep_account_pools_isolated` repeatedly on the current baseline.
2. Run `proxied_accounts_keep_separate_pools_even_with_identical_proxy_uris` repeatedly on the same baseline.
3. Instrument only test-observable events as needed: proxy accepts, target accepts, request arrival, response completion, client creation/drop, and connection counts. Do not add production logging solely for the test.
4. Determine whether the second request is delayed because the fixture server cannot accept the second upstream connection while it waits on the first keep-alive socket, or whether production pool/account state actually aliases clients/connections.
5. Record the classification in the P007 closure record.

If production account isolation is defective, stop treating this as fixture-only work and fix the smallest production defect necessary while preserving T004/T006 semantics. Do not hide a production defect behind fixture changes.

## Preferred fixture correction

If the current evidence is confirmed as fixture nondeterminism, make the test server capable of handling independent accepted connections without serially blocking `accept()` behind a keep-alive read on a prior connection.

A suitable correction should:

- preserve the existing HTTP/1.1 keep-alive reuse tests;
- allow multiple physical connections to be accepted and serviced independently;
- retain deterministic bounded teardown/join behavior;
- retain exact request/connection accounting used by transport tests;
- avoid background task/thread leaks;
- avoid broad replacement of the fixture framework;
- avoid adding a new async/runtime dependency.

A test-specific `Connection: close` mode is acceptable only if it still proves the intended account-isolation invariant and does not weaken the assertion from “separate pools create separate physical connections” into a trivially forced reconnect. Prefer correcting the fixture's connection-serving model when that can be done locally and clearly.

## Explicitly forbidden shortcuts

Do not close P007 by only:

- increasing `ProviderHttpConfig.read_timeout`, `connect_timeout`, `pool_timeout`, or production defaults;
- adding arbitrary sleeps;
- marking the test ignored/flaky or retrying it until green;
- reducing the expected connection count;
- deleting either identical-proxy account-isolation test;
- weakening assertions from two independent connections to “request eventually succeeded”;
- serializing production account pools to make the fixture pass;
- changing provider/routing/retry semantics unrelated to the diagnosed defect.

Timeout changes inside a test fixture are allowed only when they remove a test-harness race after the serving model is otherwise deterministic; they are not the primary fix.

## Required regression coverage

Preserve or add explicit coverage proving all of the following:

1. Two separately constructed `ProviderHttpClient` instances using the same proxy URI create independent transport/pool ownership and both complete successfully.
2. Two account clients created by one `ProviderClientPool` with identical proxy URIs remain account-isolated and create the expected independent physical connections.
3. One client still reuses a keep-alive HTTP/1.1 connection where the existing contract requires reuse.
4. Separate-account isolation does not depend on dropping the first client's response/client before the second request can progress unless that ordering is itself part of the public contract (it currently is not).
5. Fixture shutdown remains bounded after success and after an early test failure.
6. Proxy target observations and upstream connection counts remain exact.

If the fixture is made concurrent, add a focused regression that would fail under the former serial-accept/keepalive behavior rather than relying only on timing margins.

## Stress/repetition gate

Before running the broad suite, exercise the two isolation regressions repeatedly in one process/environment. Use a bounded repetition count sufficient to expose the former race; default to 20 repetitions each unless the implementation introduces a deterministic barrier test that makes repetition redundant.

The repetition gate must have zero failures. Do not implement an automatic retry wrapper around assertions.

## Required verification

Run at minimum:

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo test --manifest-path rust/Cargo.toml --test provider_transport identical_proxy_endpoints_keep_account_pools_isolated -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test provider_transport proxied_accounts_keep_separate_pools_even_with_identical_proxy_uris -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
rtk uv sync --frozen
rtk uv run pytest tests/tooling/ -q --tb=short --maxfail=1
rtk uv run ruff format --check scripts/ tests/tooling/
rtk uv run ruff check scripts/ tests/tooling/
rtk uv run pyright scripts/
rtk uv run python scripts/check_cutover_catalog.py
rtk uv run python scripts/validate_cutover_docs.py
rtk uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
rtk uv run python scripts/validate_m12_package_boundary.py --workflow .github/workflows/release.yml
rtk uv run python scripts/validate_m12_retirement.py
rtk git diff --check
```

Reuse P006 artifact/cross-era package evidence unless P007 changes packaging, update, catalog, runtime assets, schema, or manager-transition code. If any of those surfaces change, rerun the corresponding P006 gate instead of claiming source freshness.

The pre-existing strict-Clippy disposition from P006 remains unchanged unless P007 touches code implicated by those warnings. Do not broaden P007 into a general Clippy cleanup.

## Hosted CI gate

Local success is insufficient because hosted CI is the evidence that invalidated the P006 closure claim.

Before P007 can close:

- push the implementation/corrective tree;
- obtain a completed successful GitHub Actions `CI` run for that exact implementation tree or a descendant containing no additional production changes;
- confirm the `cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1` step passes, including `provider_transport`;
- record the workflow run ID, head SHA, conclusion, and the provider-transport result in `migration-rs/closure/retirement/007-status.md`.

If the hosted CI run fails in the same test, P007 remains open. If it fails in a different substantive test, classify that finding before closure; do not declare M12 closed solely because the original test turned green.

## Closure record

On success create:

`migration-rs/closure/retirement/007-status.md`

The record must include:

- baseline SHA and corrective implementation SHA(s);
- root-cause classification: fixture defect, production defect, or mixed;
- exact files changed;
- why the fix preserves provider/account isolation and keep-alive semantics;
- targeted repetition results;
- complete `provider_transport` and all-target Rust results;
- retained Python-tooling/retirement validator results;
- source-freshness disposition for P006 artifact/cross-era evidence;
- hosted GitHub Actions run ID/head SHA/conclusion;
- unresolved findings, including any low-severity maintenance item;
- explicit registry transition restoring M12 closure authority to accepted P007.

Do not modify `006-status.md`; it remains append-only historical evidence describing the local P006 qualification that was later superseded for final closure authority by the hosted-CI finding.

## P007 acceptance criteria

P007 is accepted only when all of the following are true:

1. The CI `ReadTimeout` is root-caused rather than masked.
2. The two identical-proxy account-isolation tests are deterministic and preserve their original behavioral assertions.
3. Keep-alive reuse behavior remains covered and passing.
4. No production timeout/default is loosened merely to satisfy the test.
5. The complete `provider_transport` suite passes.
6. The complete Rust all-target suite passes sequentially.
7. Retirement/tooling validators remain green or are rerun where P007 changed their ownership surface.
8. A successful hosted GitHub Actions CI run exists for the corrected tree.
9. No unresolved high/medium transport, account-isolation, packaging, retirement, security, lifecycle, or data-loss finding remains.
10. Registry, retirement roadmap, implementation index/handoff, and closure index agree that P007 is the current corrective closure authority.

On acceptance, M12 may be marked closed after accepted P007. P006 remains historical accepted closure evidence, but P007 becomes the current closure authority because it incorporates the hosted-CI correction.

## Non-goals

- no reopening Python application retirement;
- no provider feature expansion;
- no retry/finalization/routing redesign;
- no SQLite schema or migration changes;
- no packaging/version-catalog changes unless investigation proves they are implicated;
- no new CI matrix or repeated broad CI campaign;
- no performance tuning unrelated to the failing transport qualification;
- no M13 milestone.
