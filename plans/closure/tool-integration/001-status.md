# Tool Integration M001 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/tool-integration/001-eggsearch-backend-config-and-readiness.md`

Source subsystem roadmap:

- `plans/subsystems/tool-integration-roadmap.md#7-milestones`

Repository baseline reviewed: `e31654fae2c95d40554efa5356306ffb22a36ee7`

Implementation commits or pull requests:

- `86d2568f` — optional local backend config, loopback validation, bounded Eggfetch health/MCP negotiation, doctor CLI, tests and documentation.
- `3ee6b94` — refreshed locked runtime-asset hashes for the updated config examples.

## 1. Executive finding

M001 is complete. EggPool accepts one disabled-by-default operator configuration, performs no connection or process work during ordinary inference, and exposes an explicit `eggpool tools doctor` command. The command distinguishes disabled, unreachable, wrong service, incompatible protocol/catalog, malformed response and ready states. It verifies Eggsearch identity and version across `/healthz` and MCP initialization, negotiates protocol `2025-03-26`, checks `tools/list`, rejects redirects, and caps each response at 1 MiB. This milestone does not add a public MCP route or tool invocation.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Optional config and old config compatibility | `rust/src/config.rs`; config unit test; full workspace matrices | pass | Omitted config defaults disabled. URL and timeout bounds are validated. |
| Explicit bounded probe | `rust/src/operations/tools.rs`; `rust/tests/tool_backend_config.rs` | pass | Existing Eggfetch native HTTP transport; no subprocess, redirect following, or retained client. |
| Identity/protocol/catalog | Live Eggsearch `0.4.3` test and local MCP fixtures | pass | Live identity was `eggsearch`, protocol `streamable-http` / `2025-03-26`, catalog had ten expected tools. |
| Negative states | `tool_backend_config` fixture tests | pass | Disabled, unreachable, wrong service, wrong protocol/catalog, redirect, timeout, oversize response. |
| Reload classification | `runtime_lifecycle_r005::operator_tool_backend_config_is_ignored_by_runtime_reload` | pass | Operator-only configuration is `Ignored`; generations do not consume it. |
| CLI and JSON status | CLI contract tests and release binary `tools doctor --json` | pass | Stable `eggpool.tools.doctor/v1`; disabled output contains status/version/count only. |
| Feature parity | Default and no-default full workspace suites; no-default check and Clippy | pass | Both suites used serial tests. Local test debug symbols were disabled after the normal debug target exhausted available disk. |
| Disabled-path footprint | Baseline/current release binaries and timed config check | pass | Same x86_64 host and release profile; see §4. |

## 3. Production implementation evidence

`Config.tools` is typed and disabled by default. Validation accepts only literal `127.0.0.1` or `::1` HTTP URLs with an explicit nonzero port and exact `/mcp` path. The doctor is the only consumer. It uses Eggfetch's native transport with bounded connect/read/total timeouts and no redirect following. It prints no tool schemas, arguments, bodies, or credentials.

Both config examples and `rust/assets/runtime-manifest.json` are updated. The CLI contract fixture includes the new command paths.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo test --manifest-path rust/Cargo.toml --test tool_backend_config --test cli_contract --test runtime_lifecycle_r005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib config::tests::tool_backend_config_is_loopback_only_bounded_and_disabled_by_default -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
CARGO_PROFILE_TEST_DEBUG=0 CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
CARGO_PROFILE_TEST_DEBUG=0 CARGO_BUILD_JOBS=1 cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
EGGPOOL_TEST_EGGSEARCH_URL=http://127.0.0.1:11320/mcp cargo test --manifest-path rust/Cargo.toml --test tool_backend_config live_installed_eggsearch_qualification_when_explicitly_requested -- --exact --test-threads=1
```

### Results

- Formatting, focused tests, default Clippy, no-default check/Clippy, and both serial workspace suites passed. The existing manually qualified dashboard timing test remains ignored by its own annotation.
- The first default workspace build stopped before test execution because its debug target tree exhausted 26 GiB available space. I removed generated build artifacts and the temporary baseline worktree, then reran both matrices with test debug symbols disabled and one Cargo build job. No source was removed.
- Live qualification used Eggsearch `0.4.3`, explicitly started on `127.0.0.1:11320`, and stopped after the probe. The live check passed.
- Baseline/current release binary sizes were 37.7 MB and 37.8 MB (about +0.1 MB). On Linux x86_64 / Intel i9-9900K, `check-config` took 0.00 s in both runs; peak RSS was 11,136 KiB baseline and 11,008 KiB current. `--version` also took 0.00 s in both. These RSS measurements are subject to process-level noise. No Eggsearch process was running during disabled-path measurements.

## 5. Invariant review

- Omitted/disabled config causes no connection, spawn, polling, listener setup, background task, or database write.
- Only literal loopback URLs and a fixed MCP path are accepted. Redirects are not followed and clients cannot choose the backend.
- Probe state is local to the explicit CLI future; the client is dropped on return.
- Diagnostics contain only a schema tag, finite status, bounded version, and tool count. Response bodies are never printed.
- Runtime reload marks `tools` ignored because runtime generations do not consume this config.
- Existing inference routes do not depend on Eggsearch availability.

## 6. Failure and recovery review

Connect/read/total deadlines bound failure. The body cap rejects oversized responses. Identity, protocol, catalog and redirect mismatches do not fall through to a generic proxy. Cancellation drops the native response; there is no detached task or retained session. Service failure has no provider-health/readiness effect, and restart does not start Eggsearch.

## 7. Migration and compatibility review

No database migration, token scope, public route, listener, or runtime dependency was added. Old TOML parses with the disabled default. The config example hashes are protected by `rust/tests/build_manifest.rs`. The `--no-default-features` build and suite pass.

## 8. Security review

The endpoint accepts only literal loopback HTTP authority and exact `/mcp`; it disallows userinfo and untrusted name resolution. The native request path does not follow redirects. No downstream credential is sent. Responses are parsed only for identity, protocol and tool-name facts and are not logged or returned. Timeouts, response-size limits, and an exact expected tool-name set constrain resource use.

## 9. Documentation and operations

Updated `docs/cli-reference.md`, `docs/configuration.md`, `docs/deployment.md`, `architecture/overview.md`, both Rust config examples, and the CLI command contract fixture. The deployment guide links Eggsearch's service lifecycle and states that EggPool does not install/start Eggsearch or expose a gateway.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| Low | Footprint comparison used an x86_64 host; no physical SBC measurement was performed. | No hardware-specific RSS/startup claim is supported. The integration remains disabled and connection-free by default. | Gather physical SBC and representative team-host measurements during gateway operational qualification. |

## 11. Roadmap disposition

Milestone closed. M001's operator-only readiness contract is complete. M002 may proceed after ADR-0007 acceptance. M001 does not claim a public MCP gateway or user tool execution.

## 12. Registry updates

The closure commit moves M001 from active to implemented/closed and audits its dependents. M002 remains blocked because ADR-0007 is still proposed at this closure point. M003 and M004 remain blocked on their own prerequisites. Accepting ADR-0007 must separately promote M002 to ready.
