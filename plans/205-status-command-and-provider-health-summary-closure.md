# Plan 205: `status` command and provider health summary closure

> **Status:** complete
>
> **Closes:** `plans/202-status-command-and-provider-health-summary.md`
>
> **Scope:** add a top-level `eggpool status` command reporting concise proxy health plus exactly one human-readable line per configured upstream provider, from authoritative cached/live Eggpool state with no outbound provider probes.

## What was built

- `rust/src/operations/status.rs` (new aggregation boundary): typed `ProxyStatusSnapshot` (`schema_version: 1`), `ProxyStatus` (`ready`/`degraded`/`unready` plus CLI-only `unavailable`), `ProviderStatus` (`ready`/`degraded`/`unavailable`/`disabled`/`unknown`), `ProviderObservation` (`verified`/`failed`/`stale`/`never`), bounded reason codes (no raw `provider_pings.error`, credential, prompt, or body text). Provider precedence is `disabled` (no enabled accounts) > `unavailable` (enabled but none routable) > `degraded` (partial failure, model quarantine, or fresh failed probe with a viable route) > `ready` (routable plus request or fresh-probe evidence) > `unknown` (routable by gating but unverified; freshly registered health entries never count as verified). `evaluate_readiness()` is the single readiness authority shared by `readyz` and status. Observation freshness defaults to `models.stale_after_s` (fallback 7200s).
- `rust/src/db/repositories.rs`: `PingRepository::latest_grouped()` returns the latest ping per `(provider_id, account_name)` pair in one bounded read (at most one row per account).
- `rust/src/routing/router.rs`: read-only status accessors (`health_manager()`, `health_snapshots()`, `catalog_snapshot()`, `account_identities()`, `all_account_identities()`); no claim, probe, or breaker mutation.
- `rust/src/health/health_manager.rs`: `now()` made public so status can evaluate cooldowns in the manager's clock domain without duplicating health logic.
- `rust/src/server/health.rs`: new authenticated `GET /api/status` returning one 200 snapshot per request (deterministic provider ordering, bounded body, empty-providers unready snapshot when no generation is acquirable, degraded `db` flag instead of a crash on database failure). `readyz` now consumes the shared `evaluate_readiness()` result; its wire contract is unchanged. No outbound provider transport.
- `rust/src/server/middleware.rs` + `rust/src/server/mod.rs`: `/api/status` requires API-key auth even when the dashboard is public; route registered.
- `rust/src/cli.rs` + `rust/src/runtime.rs`: top-level `eggpool status [--json]` with a shared bounded local-control fetch helper (`fetch_local_json`, wildcard host resolves to loopback, `STATUS_TIMEOUT`, body cap, strict JSON). Human output is one proxy header line, one row per configured provider, and one runtime footer. Exit codes are `0` for ready/degraded, `1` for reachable-but-unready, `3` for unreachable (offline fallback still lists locally configured providers as `unknown`/`disabled`; `--json` emits the same-schema offline wrapper with `proxy.available=false`).
- `rust/tests/status_command.rs` (new, 10 tests): CLI parsing, deterministic ordering, ready/degraded/unready proxy aggregation, disabled-provider neutrality, raw-error redaction, auth-terminal mapping, latest-ping grouping, and endpoint auth/schema/boundedness/secret-freedom.
- `rust/src/operations/status.rs` unit tests (9 tests): all provider precedence branches, quarantine, stale evidence, timestamp parsing/rejection.
- `tests/fixtures/cli/contract-matrix.json` + `rust/tests/cli_contract.rs`: `status --json` added (63 to 64 commands).
- `runtime-status` unchanged and remains the detailed diagnostic command.

## Evidence

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::status -- --test-threads=1
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```

Focused targets pass locally (`cli_contract` 3, `status_command` 10, `operations::status` lib 9). Full serial workspace suite passes with no failures. No-default feature guard passes (`check` + `clippy`; no feature changes). Python tooling (`ruff`, `pyright`, `pytest` 75 passed, 1 skipped) passes. Release doc/boundary validators pass. Live binary verified: `eggpool status` against an unreachable server prints locally configured providers as `unknown` and exits 3; `status --json` emits the same-schema offline wrapper.

## Docs updated

- `README.md`: `status` vs `runtime-status` command rows.
- `docs/deployment.md`: `status` usage, semantics, exit codes, offline behavior; deploy commands reference rows.
- `docs/api-reference.md`: `GET /api/status` row in Health & Readiness.
- `architecture/overview.md`: CLI tree, health routes, operations/status ownership, observability wording, capabilities, review index.
- `architecture/README.md`: HTTP adapter + operations ownership rows.
- `architecture/deep-dive-dashboard.md`, `deep-dive-runtime.md`, `deep-dive-deployment.md`: status endpoint ownership and CLI wording.
- `AGENTS.md`: `operations/status.rs` convention bullet.
- `.opencode/skills/architecture/SKILL.md`: `status.rs` in operations pointers.
- `.opencode/skills/development/SKILL.md`: `status_command` target and thin-handler rule.

## Acceptance mapping (Plan 202 criteria 1-16)

1. `eggpool status` exists as a top-level command (`cli.rs`, `status_command` parse test).
2. Default output is one proxy header plus exactly one provider row per configured provider, including all-disabled providers (`runtime.rs`, ordering test).
3. `status --json` emits the versioned server snapshot (`schema_version: 1` endpoint test).
4. No outbound provider requests on the status path (handler reads generation/DB only; no transport call).
5. Aggregation uses active-generation identity plus live health plus cached probe/catalog evidence (server handler wiring).
6. Freshly registered health entries without success observations report `unknown`, never verified-ready (unit + integration tests).
7. Partial failures report `degraded` while a route remains (unit + proxy tests).
8. No routable enabled account reports provider `unavailable` (precedence unit tests).
9. Disabled providers show `disabled` and do not degrade the proxy (unit test).
10. Overall `degraded` with readiness passing exits 0 (proxy aggregation + exit mapping).
11. Reachable-but-unready exits 1; unreachable exits 3 (runtime exit mapping, live binary check).
12. Unreachable servers still list locally configured providers as `unknown`/`disabled` (offline snapshot + live binary check).
13. `readyz` and status share `evaluate_readiness()` (single authority, unchanged `readyz` wire contract).
14. No credentials, prompts, bodies, tool payloads, or raw ping errors in output (sentinel-secret tests on both human and JSON paths).
15. `runtime-status` preserved for detailed diagnostics (untouched handler, still tested by `operations_o003`).
16. No new production HTTP/TUI/monitoring dependency (`Cargo.toml`/`Cargo.lock` untouched).
