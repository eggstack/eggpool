# Provider Agents Milestone 001 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/provider-agents/001-agent-account-and-reverse-tunnel.md`

Source subsystem roadmap:

- `plans/subsystems/provider-agents-roadmap.md#7-milestones`

Repository baseline reviewed: `3b90434b` (Accept ADR-0016 and register the
provider agents roadmap)

Implementation commits or pull requests:

- `72ad4ce7` — Add the provider agent account kind and reverse-tunnel foundation

## 1. Executive finding

The M001 capability boundary is complete and verified. A provider account may
now declare `auth_mode = "agent"`; its endpoint is a server-local loopback
port served by an embedded Eggress `1.0.12` reverse server that the user's
agent reaches by dialling OUT over a TLS control channel, so the user's machine
needs no inbound port and no provider token reaches EggPool. A request routed
through an agent account is proven end-to-end over a real reverse tunnel for
both the finite and the streaming path. An agent account is selectable only by
its bound owner, and a 401 from an agent terminalizes the attempt with no
credential refresh, no same-account replay, and no substitution by another
account.

The milestone is **conditionally closed**, not closed, for one named reason:
the repository-wide dependency-audit gate is red on two pre-existing `russh`
advisories that M001 neither introduced nor can resolve. See §10.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| `AccountAuthMode::Agent` exists and is additive | `rust/src/config.rs` enum + serde `snake_case` | pass | `StaticKey` and `OAuth` unchanged; default unchanged |
| Agent account carries a per-account loopback endpoint | `AccountConfig.base_url`; `rust/src/providers/client_pool.rs` agent branch | pass | Authority must equal `agent.external_bind` |
| Per-account endpoint reaches the provider client | `rust/tests/provider_transport.rs` `provider_client_pool_uses_agent_account_endpoint` | pass | Account client built from the account URL; provider client untouched for other accounts |
| Static-key and OAuth accounts keep the provider base URL | same target | pass | Existing provider/proxy topology unchanged |
| One embedded reverse server per agent account | `rust/src/agents/tunnel.rs` `AgentTunnelManager::prepare_for_generation` | pass | One `ReverseServer` per enabled Agent account |
| Loopback-only external listener | `rust/src/config.rs` validation; `rust/src/agents/tunnel.rs` `allow_bind` | pass | External bind must be loopback; `allow_bind` is exactly the agent's own external bind |
| Control channel requires TLS 1.2+ with CA verification | `rust/src/config.rs` agent validation; `rust/src/agents/tunnel.rs` server TLS config | pass | TLS is mandatory for EVERY agent account, loopback or not |
| Control binds and external binds are unique across accounts | `rust/src/config.rs` bind uniqueness set | pass | Control/external collisions rejected in both directions |
| Base-URL authority identifies the actual bound address | `valid_agent_base_url` | pass | IP literal must equal the external bind IP; `localhost` only for `127.0.0.1`/`::1` |
| Agent account requires its own bearer credential | `rust/src/config.rs` agent validation | pass | Inline key or non-empty `api_key_env`; the control-channel password is a separate secret |
| Transport credential is never stored in the TOML | `AgentAccountConfig.transport_secret_env` holds a variable NAME only | pass | Value resolved from the environment at tunnel start |
| Agent account must not use the outbound proxy path | `rust/src/config.rs` validation AND `rust/src/providers/client_pool.rs` typed `AgentProxyConfiguration` | pass | Rejected at both the configuration and the runtime construction boundary |
| Owner-bound selection | `rust/src/routing/eligibility.rs` `is_active_owner` gate now covers Agent | pass | Finite and streaming share the eligibility builder |
| Agent 401 is terminal | `rust/src/coordinator/mod.rs` `agent_401_for_account`; finite + streaming terminalization | pass | Refresh/replay branches remain gated on the OAuth-only predicate |
| OAuth 401 refresh behaviour unchanged | `rust/src/coordinator/finite.rs`, `rust/src/coordinator/streaming/coordinator.rs` | pass | No OAuth branch was modified |
| Restart-required classification for agent listener/TLS/credential paths | `rust/src/config_reload_policy.rs`; `rust/tests/runtime_lifecycle_r005.rs` | pass | Dedicated agent restart path, per-category transition tests, mixed transition is wholly restart-required |
| `[agents].enabled` gate | `rust/src/config.rs`, `rust/src/accounts/registry.rs` | pass | Defaults on; disables agent accounts |
| Tunnel is generation-owned with bounded shutdown | `rust/src/runtime_lifecycle/generation.rs`, `rust/src/agents/tunnel.rs` | pass | Listeners start before publication; a failed candidate releases its tunnels |
| No second supervisor / reload loop / config source / database | `rust/src/agents/tunnel.rs` | pass | Manager holds no configuration, reload, or SQLite dependency |
| End-to-end request through a real reverse tunnel | `rust/tests/agent_upstreams.rs` | pass | Real `eggress_protocol_reverse::client::ReverseClient` stands in for Eggpass |
| Owner isolation, including the unbound case | `rust/tests/agent_upstreams.rs` | pass | An unbound third Agent is denied and receives no traffic |
| No fixed sleeps as readiness proofs | `rust/tests/agent_upstreams.rs` `wait_for_active_controls` | pass | Bounded deadline on the observed reverse-server control counter |

## 3. Production implementation evidence

Landed ownership:

- `rust/src/agents/mod.rs`, `rust/src/agents/tunnel.rs` — the new
  `AgentTunnelManager`. It is a process-owned registry holding WEAK tunnel
  references; each generation holds a lease that owns the strong references, so
  an unchanged listener survives a live rehash and the last lease release shuts
  the listener down under a bounded timeout. Configuration is fingerprinted, so
  an incompatible live change is refused rather than silently reused.
- `rust/src/config.rs` — `AccountAuthMode::Agent`,
  `AgentAccountConfig`, `AgentTlsConfig`, `AccountConfig.base_url`,
  `AccountConfig.agent`, `AccountConfig.label`, `[agents]`, and the agent
  validation rules.
- `rust/src/config_reload_policy.rs` — the agent restart-required
  classification and the `agents.enabled` disposition.
- `rust/src/providers/client_pool.rs` — the per-account Agent client and the
  runtime proxy refusal. The immutable nested `ArcSwapOption` topology and the
  atomic close are unchanged; `oauth_eggfetch_client()` is untouched.
- `rust/src/accounts/registry.rs`, `rust/src/catalog/refresh.rs` — explicit
  Agent branches.
- `rust/src/coordinator/finite.rs`, `rust/src/coordinator/mod.rs`,
  `rust/src/coordinator/streaming/coordinator.rs` — Agent credential
  resolution and the terminal Agent 401.
- `rust/src/routing/eligibility.rs` — the owner gate extended to Agent.
- `rust/src/runtime_lifecycle/generation.rs`, `rust/src/runtime_lifecycle/process.rs` —
  generation-owned tunnel preparation, publication, and close.

Dependency change: exactly one package, `eggress-protocol-reverse = "=1.0.12"`,
the narrowest crate that provides the reverse server and client. `eggress-embed`
and `eggress-runtime` were deliberately NOT adopted: they are a second
supervisor that also registers its own process signal handling, which EggPool
already owns. The `eggress = 1.0.12`, `eggfetch-core = 0.4.0`, and
`eggserve-server = 0.4.0` pins are unchanged, and no advisory waiver was
added.

## 4. Verification executed

### Commands run

All commands below were run LOCALLY on 2026-11-24 in the authorized worktree.
No commit was pushed and no hosted CI run was executed for this change, so no
CI evidence is claimed.

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test agent_upstreams -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test build_manifest -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
git diff --check
```

### Results

| Check | Result |
|---|---|
| `cargo fmt --check` | pass |
| strict Clippy, default features | pass, zero warnings |
| `cargo check --no-default-features` | pass |
| strict Clippy, `--no-default-features` | pass, zero warnings |
| `agent_upstreams` | 2 passed, 0 failed |
| `runtime_lifecycle_r005` | 13 passed, 0 failed |
| `provider_transport` | 37 passed, 0 failed |
| `routing_domain` | 16 passed, 0 failed |
| `build_manifest` | 4 passed, 0 failed |
| full serial workspace suite, default features | 1365 passed, 0 failed, 2 ignored (97 result blocks) |
| full serial workspace suite, `--no-default-features` | 1366 passed, 0 failed, 2 ignored (97 result blocks) |
| `cargo build --locked --release` | pass |
| `cargo deny check` | **advisories FAILED**; bans ok, licenses ok, sources ok — see §10 |
| `cargo tree -e features` | reviewed; the added reverse crate enables `tokio/rt-multi-thread` transitively. EggPool's runtime stays `current_thread` |
| `cargo tree --duplicates` | reviewed; the reported duplicate versions are the pre-existing SSH/crypto/Rustls graph, unchanged in kind by this milestone |
| tooling suite | 267 passed, 1 skipped |
| `scripts/validate_release_docs.py` | pass |
| `git diff --check` | clean |

The `cargo deny` result is a genuine failure of the advisory gate and is not
reported as a pass. Its scope is established in §10.

### Independent review

Two independent reviewers audited the uncommitted implementation before
closure, one over configuration/validation/reload/security and one over the
runtime path. The first review returned one HIGH, two MEDIUM, and two LOW
findings; the second returned one MEDIUM and one LOW finding. All seven were
fixed in the same implementation commit `72ad4ce7` and re-verified by the
matrix above:

- HIGH — a loopback control listener could run without TLS, contrary to
  ADR-0016. TLS is now mandatory for every agent account.
- MEDIUM — duplicate control binds were not rejected. Now rejected.
- MEDIUM — a `localhost` base-URL authority could match a loopback external
  bind IP it does not resolve to. Now exact.
- MEDIUM — the runtime construction path did not independently refuse an agent
  account carrying proxy settings. Now a typed runtime rejection.
- LOW — no-agent configuration compatibility was not demonstrated. Now a test.
- LOW — R005 had no agent transition test. Now one per configuration category.
- LOW — the integration test did not prove the unbound-agent deny case. Now a
  third, unbound Agent that receives no traffic.

## 5. Invariant review

- **No provider token on the server.** An agent account's credential is the
  agent's own local bearer key; the control-channel password is a separate
  secret held in the environment. No access token, refresh token, ID token, or
  PKCE material is read, stored, or logged. Owner-bound provider OAuth
  custody is untouched by this milestone.
- **Owner-only selection, no pooling.** `rust/src/routing/eligibility.rs`
  requires an active owner binding for both OAuth and Agent accounts. The
  integration test proves an unbound Agent is denied and receives no traffic,
  and that owner A's traffic never reaches owner B's Agent.
- **Loopback only.** The external listener is validated as a loopback socket
  and the reverse server's `allow_bind` is exactly that one address, so the
  reverse client cannot request any other bind. The reverse client cannot
  become a general port forwarder.
- **TLS mandatory on the control channel.** Enforced in configuration for every
  agent account; there is no code path that builds a reverse server without
  TLS material.
- **Transport policy unchanged.** The Eggfetch `0.4.0` pin, the Eggress
  `1.0.12` pin, the provider pool's immutable topology, and the single
  generation model are unchanged.
- **OAuth behaviour unchanged.** No OAuth branch was modified. The refresh and
  replay branches remain gated on the OAuth-only predicate; only the
  terminalization call gained an Agent predicate.
- **Reload authority unchanged.** `classify_transition` remains the only
  reload-versus-restart authority; no independent key list was introduced.
- **Performance boundaries unchanged.** The single SQLite gate, the streaming
  mpsc bridge, the `current_thread` Tokio runtime, and the routing selection
  lock are untouched.

## 6. Failure and recovery review

- **Bind conflict.** A listener that cannot bind fails the candidate
  generation, so the previous generation keeps serving. The candidate failure
  path releases the tunnels it had already prepared.
- **Dropped control connection.** The reverse server refuses new streams while
  the control channel is down; the in-flight request fails with a transport
  error. The account is not silently substituted. M003 projects the tunnel
  state into routing exclusion and the operator surface.
- **Generation replacement.** The previous generation retires through the
  existing lease and finalization boundary; the tunnel is released only when
  the last lease owner goes away, under a bounded shutdown timeout.
- **Incompatible live change.** A generation whose agent configuration
  fingerprint differs from the live one is refused rather than silently
  reusing a listener with the wrong credential or TLS material.
- **Cancellation.** The tunnel manager's shutdown uses the reverse server's
  cancellation token and a bounded join; a timed-out release is reported in
  the close report rather than ignored.

## 7. Migration and compatibility review

- Existing configurations load unchanged. `Agent` is additive, every new
  field defaults, and a configuration with no agent accounts and no `[agents]`
  section loads and round-trips with its semantic content preserved.
- No storage migration. No schema change. The persisted model, quota, health,
  and binding tables are untouched.
- `[oauth]`, `AccountAuthMode::OAuth`, the OAuth credential store, the
  OAuth routes, the `oauth-login` CLI command, and the ADR-0015 refresh path
  all remain fully in force. Direct OAuth custody is retired only in Provider
  Agents M004.
- The `rust/assets/runtime-manifest.json` digest for the changed
  `rust/config.example.toml` was refreshed; `build_manifest` passes.

## 8. Security review

- **Transport.** The control channel is TLS with operator-supplied material
  and a client CA. The external listener is loopback-only and plaintext, which
  is the intended boundary: it never leaves the EggPool host.
- **Authentication.** The reverse server authenticates the agent with a
  per-account transport secret. The agent's own bearer key authorises the
  HTTP request inside the tunnel.
- **Secret hygiene.** No transport secret, bearer key, or TLS private key
  appears in a `Debug`, `Display`, error string, status value, or
  diagnostic. The tunnel error variants carry provider and account identifiers
  and a generic reason only.
- **Unbind allowance.** `allow_bind` is exactly one entry: the agent's own
  external bind. This is the control that prevents the reverse client from
  becoming a remote forwarder.
- **Privilege bounds.** The owner is taken from the verified principal only.
  The operator gate `[agents].enabled` blocks agent accounts.
- **Audit.** No new security event type is introduced in this milestone;
  enrollment-time security events belong to M002.

## 9. Documentation and operations

- `rust/config.example.toml` — an Agent account example including the
  mandatory control-channel TLS material and the `[agents]` gate.
- `docs/providers.md` — an Agent-accounts section describing the account
  kind, the loopback endpoint rule, the mandatory control-channel TLS, and the
  fact that no provider token reaches EggPool.
- `architecture/overview.md` — the new `rust/src/agents/` module is named in
  the review index with its ownership boundary.
- `AGENTS.md` — the Layout section names `agents/` as a top-level runtime
  module.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| high | The repository-wide dependency-audit gate is red: `cargo deny check` reports advisories FAILED for RUSTSEC-2026-0492 and RUSTSEC-2026-0495 against `russh 0.62.7`, reached through `eggress-transport-ssh 1.0.12` → `eggress-outbound 1.0.12`, which the default `ssh` feature enables. | The `dependency-audit` workflow gates on `rust/Cargo.toml`/`rust/Cargo.lock`/`deny.toml`, so it will fail for this branch. The condition is a pre-existing SSH-transport advisory, not an Agent-path defect. | Owned by Security hardening M008. Resolution requires an Eggress release permitting `russh >= 0.65`, or an accepted ADR for a vendored patched `0.62.x`, or an accepted ADR removing the default `ssh` feature. No advisory waiver is acceptable and none was added. |
| medium | The integration fixture proves server-side TLS with CA pinning, but not mutual TLS: it does not exercise `require_client_cert`. | The mTLS branch of the reverse server's client-certificate handling is configured and validated but not proven end-to-end by an automated test. | Prove the mTLS branch when the enrollment flow (M002) issues real per-agent client certificates. Named in M002's credential-issuance decision point. |
| low | `cargo tree --duplicates` reports multiple versions in the pre-existing SSH/crypto/Rustls graph. | No new duplicate is introduced by this milestone. | No action; `deny.toml` treats these as inventory. |

Scope evidence for the high finding: the identical `cargo deny check` run
against the PRE-M001 commit `3b90434b` in a detached worktree fails with the
same two advisories. The `rust/Cargo.lock` diff for this milestone adds
exactly one package, `eggress-protocol-reverse 1.0.12`, which carries no
advisory. The finding is therefore pre-existing, repo-wide, and not
introduced, worsened, or resolvable by M001.

## 11. Roadmap disposition

Conditionally closed with a named outstanding condition: the dependency-audit
gate must report `advisories ok` again before this branch lands on `main`,
which requires the Security hardening M008 decision. The condition is a
repo-wide gate on a pre-existing SSH dependency, not an M001 interface, so it
does not block the next milestone's implementation; M002 is promoted and the
promotion is recorded with that reasoning.

M001's own capability boundary is complete: no partially implemented Agent
path, no unrun required test, no unimplemented security or migration
requirement, and intact `--no-default-features` parity.

## 12. Registry updates

Applied in the same commit as this record:

- `plans/registry.md` — the Provider agents roadmap row records M001 as
  conditionally closed and M002 as ready; the M001 row moves from the
  dependency-ready table to the recently-closed table; the blocked-work row for
  M002 is restated; a dated audit note records the closure, the condition, and
  the promotion reasoning.
- `plans/subsystems/provider-agents-roadmap.md` — the milestone status table
  records M001 as conditionally closed and M002 as ready.
- `plans/implementation/provider-agents/001-agent-account-and-reverse-tunnel.md` —
  header status becomes `implemented`.
- `plans/implementation/provider-agents/002-owner-enrollment-and-revocation.md` —
  header status becomes `ready`.
