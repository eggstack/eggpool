# Tool Integration M003 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/tool-integration/003-authenticated-mcp-gateway.md`

Source subsystem roadmap:

- `plans/subsystems/tool-integration-roadmap.md#7-milestones`

Repository baseline reviewed: `27f72ce9` (gateway activation)

Implementation commits or pull requests:

- `c61a31a` — disabled-by-default authenticated Eggsearch MCP route, config and restart classification, authorization/session boundary, docs and initial integration qualification.
- `5a24eeb` — compact response projection, backend/body boundary tests, cancellation tests, and the dedicated gateway security/protocol guide.

## 1. Executive finding

The authenticated tools-only MCP capability is implemented and functionally
qualified. EggPool serves the route only when both tool configuration gates
are enabled, requires a managed personal token, filters discovery and
reauthorizes calls against current grants, and opens a fresh bounded Eggsearch
0.4.3 session for each list/call operation. A real local Eggsearch 0.4.3
initialize/list/call run passed. The closure is conditional because this
x86_64 host cannot run the repository's physical-SBC qualifier; the default-off
SBC memory/startup comparison and physical small-team contention measurement
remain outstanding.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Separate disabled-by-default route | `config::ToolBackendConfig` defaults; `server::routes`; `disabled_gateway_route_is_not_registered` | pass | Both `[tools].enabled` and `gateway_enabled` are required. Config changes require restart. |
| Managed-user auth, scopes and default-deny grants | `mcp_sessions_filter_per_user_and_reauthorize_direct_calls`; `tool_grants`; `server_auth` | pass | Two users; discovery filter, direct-call denial, token revocation and current policy recheck are covered. |
| Explicit MCP subset and protocol rejection | `mcp_gateway` fixture suite | pass | Protocol `2025-03-26`; initialize, initialized notification, list and call only; batches, wrong version and other methods reject. |
| Backend identity and catalog integrity | `Gateway::open_backend` / `BackendSession::list`; live Eggsearch route test | pass | Requires Eggsearch 0.4.3 and the exact canonical ten-name catalog; schema and catalog drift fail closed. |
| Bounded input, schema, result and concurrency | `mcp_gateway` | pass | 128 KiB ingress, 32 KiB arguments, 64 KiB schemas, depth 32, 1 MiB backend response, 16 permits and 1,024 expiring sessions. Oversized ingress/backend response and wrong backend MIME reject. |
| Structured result and trust preservation | fixture call assertions; live `provider_status` call | pass | `content`, `structuredContent`, `isError`, source identifiers and trust labels are preserved. `response_detail=compact` is selected only when the live schema advertises it and the caller omits it. |
| Cancellation and backend failure isolation | fixture cancellation/drop notification; `unavailable_backend_is_isolated_from_model_health` | pass | Aborting the client drops the backend future; unavailable Eggsearch returns a tool error while `/v1/healthz` remains 200. |
| Configuration/restart ownership and documentation | `config_reload_policy`; docs and architecture updates | pass | Tool config is restart-required; sessions are process-local and are not persisted. |
| Physical-SBC disabled/enabled footprint and team contention | Not available on this x86_64 host; physical runner refuses hosted/non-device-tree hosts | partial | Exact remaining evidence and risk are recorded in §10. A two-user concurrent request fixture passes, but it is not a capacity benchmark. |

## 3. Production implementation evidence

`rust/src/server/tools.rs` owns the narrow MCP adapter and bounded backend
client. It accepts only the configured literal-loopback Eggsearch URL, never
forwards downstream credentials, filters tool discovery through
`ToolPolicyService`, and rechecks invocation authorization. Sessions are
random, process-local, credential/user/auth-epoch bound, and expire after 30
minutes idle. A per-session catalog fingerprint prevents a call against a
schema different from the last discovery. Backend calls do not enter the model
coordinator and cannot affect provider health.

`tools.gateway_enabled` defaults false and is classified restart-required.
No database migration, listener, background task, or new runtime dependency
was added. Existing inference routes are unchanged.

## 4. Verification executed

### Commands run

```bash
rtk cargo fmt --manifest-path rust/Cargo.toml --all -- --check
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
rtk cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
rtk cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
CARGO_PROFILE_TEST_DEBUG=0 CARGO_BUILD_JOBS=1 rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
CARGO_PROFILE_TEST_DEBUG=0 CARGO_BUILD_JOBS=1 rtk cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
CARGO_PROFILE_TEST_DEBUG=0 CARGO_BUILD_JOBS=1 EGGPOOL_TEST_EGGSEARCH_URL=http://127.0.0.1:11320/mcp rtk cargo test --manifest-path rust/Cargo.toml --test mcp_gateway live_eggsearch_gateway_initialize_list_and_call -- --ignored --exact --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test mcp_gateway -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test server_auth -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test tool_grants -- --test-threads=1
rtk cargo test --manifest-path rust/Cargo.toml --test build_manifest -- --test-threads=1
rtk git diff --check
```

### Results

- Format check, strict Clippy in both feature configurations, no-default check,
  and `git diff --check` passed.
- Final focused gateway suite: 3 passed, 1 live-only test ignored by default.
- Final default workspace: 1,230 passed, 2 ignored across 93 suites.
- Final no-default workspace: 1,231 passed, 2 ignored across 93 suites.
- The explicit live test passed (1 passed, 2 filtered out) against the running
  Eggsearch 0.4.3 service at loopback port 11320. It initialized, listed the
  granted `provider_status` tool, and called it successfully using MCP
  `2025-03-26`.
- Earlier focused results on this candidate: `runtime_lifecycle_r005` 12
  passed; `server_auth` 2 passed; `tool_grants` 4 passed; `build_manifest` 4
  passed.
- The first final default rerun failed because the shared filesystem was full
  (202 passed, 33 temp-file/database setup failures). Only this repository's
  generated Cargo incremental cache was removed; the rerun above passed.

## 5. Invariant review

- Ordinary Chat Completions, Responses, Messages, and compact inference remain
  unchanged and do not use Eggsearch.
- Only an operator-configured loopback backend can be contacted; callers
  cannot choose a URL or backend.
- Every list/call checks current user policy and the appropriate credential
  scope. A previous list result is not invocation authority.
- Tool results remain explicitly untrusted where Eggsearch marks them so.
- No prompts, arguments, backend URLs, credentials, document/source text, or
  tool results are persisted by the gateway.
- There is one EggPool listener, no second SQLite writer, and no background
  work when disabled.

## 6. Failure and recovery review

Malformed requests, unsupported protocol/methods, incompatible catalog,
schema drift, timeout/connection failure, wrong MIME, oversized responses,
and authorization failures map to bounded MCP/HTTP errors. The integration
fixture proves direct-call denial, backend failure isolation, client-abort
cancellation, and permit/future release. Sessions are memory-only and vanish
on process restart. No retry is made for a potentially side-effectful tool.

## 7. Migration and compatibility review

M003 adds no schema migration. The existing M002 default-deny grants and token
scopes remain authoritative. The route is not an OpenAI/Anthropic execution
promise and does not claim generic Streamable HTTP parity. GET/SSE, resources,
prompts, pagination, JSON-RPC batches, and session resumption are explicitly
unsupported.

## 8. Security review

Managed personal-token authentication is mandatory even with a public
dashboard. Session IDs are random and bound to the authenticated principal and
auth epoch. The backend sees no downstream Authorization header. Catalog and
tool arguments are bounded and validated against the current schema. Tool
outputs are not logged or persisted. Backend failure does not feed model
provider health or account backoff.

## 9. Documentation and operations

Updated `docs/api-reference.md`, `docs/agent-configuration.md`,
`docs/cli-reference.md`, `docs/configuration.md`, `docs/deployment.md`,
`docs/firewall.md`, `docs/tool-gateway.md`, `architecture/overview.md`, and
`architecture/deep-dive-on-prem-user-access.md`. The dedicated guide records
the supported protocol subset, threat boundary, credential setup, trust
handling, and operational limits.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | Physical SBC default-off/enabled startup and RSS comparison was not run; the x86_64 environment has no device-tree board identity. | No SBC memory or startup-impact claim is supported. The feature remains off by default and adds no backend connection/task at startup. | On a physical Raspberry Pi/SBC release candidate, run paired disabled/enabled `scripts/qualification_sbc.py` functional and benchmark cells, then repeat the representative team-host contention profile. Record binary size, startup/RSS scalars, p95/p99 where sample counts permit, and no content or credentials. |
| low | Gateway session loss across a full process restart is established by process-local storage but has no dedicated restart integration test. | No resumability is claimed; clients must initialize again after restart. | Add a restart test if the session ownership implementation changes or before claiming restart qualification. |

## 11. Roadmap disposition

M003 is conditionally closed: all production and software-level capability
criteria pass, with physical footprint/contention evidence remaining. The
condition does not widen supported deployment claims. M004's software
dependency audit may proceed against the closed M003 route and policy
interfaces; M004 must still qualify its own coordinator/IR and hosted-loop
contracts before implementation is promoted.

## 12. Registry updates

The tool-integration roadmap records M003 conditionally closed and links this
record. The implementation plan status is `implemented`. The registry moves
M003 from active work to recently closed and retains M004 as blocked pending
its coordinator/IR interface audit.
