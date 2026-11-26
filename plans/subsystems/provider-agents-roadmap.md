# Provider Agents Roadmap

Status: closing

Long-term references:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Related ADRs:

- `plans/adrs/ADR-0016-provider-agent-upstreams.md` (accepted)
- `plans/adrs/ADR-0010-personal-upstream-account-custody.md` (accepted, ownership model reused)
- `plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md` (superseded on M004 closure)
- `plans/adrs/ADR-0014-provider-oauth-web-ui-and-desktop-flow-boundary.md` (superseded on M004 closure)
- `plans/adrs/ADR-0015-provider-oauth-owner-scoped-credential-refresh-retry.md` (superseded on M004 closure)

## 1. Purpose and ownership boundary

This subsystem replaces server-side provider OAuth custody for personal
accounts with **user-owned agents**.

Each managed user runs their own Eggpass on their own machine. Eggpass owns the
provider OAuth grant, token refresh, and the local OpenAI-compatible server.
EggPool owns account configuration, the per-user binding, the reverse-tunnel
endpoint, routing eligibility, quota, budgets, accounting, model discovery, and
the operator/member surfaces.

Ownership at the code level:

- **EggPool, new:** `rust/src/agents/` (tunnel manager, enrollment, ticket
  decoding, per-agent credentials), `rust/src/config.rs` (agent account shape),
  `rust/src/providers/` (per-account endpoint), `rust/src/catalog/` (origin
  identity), `rust/src/server/` (enrollment and revocation routes), and the
  dashboard/status projections.
- **EggPool, reused unchanged:** `rust/src/personal_upstream.rs` bindings and
  grants, `rust/src/routing/`, `rust/src/quota/`, `rust/src/user_budget.rs`,
  `rust/src/coordinator/`.
- **EggPool, removed in M004:** `rust/src/oauth.rs`,
  `rust/src/server/oauth_login.rs`, `rust/src/server/oauth_credentials.rs`,
  `rust/src/operations/oauth_login.rs`, the `[oauth]` configuration, and the
  ADR-0015 retry path.
- **Not EggPool's:** provider token refresh, provider consent, the local HTTP
  server, and the reverse client. Those belong to Eggpass.

This subsystem must not own provider transport policy, routing decisions, or
the binding model; it consumes them.

## 2. Work classification

### Invariants

- No provider access token, refresh token, ID token, or PKCE material is stored
  on the EggPool server for a personal account.
- An agent account is usable only by its bound owner; there is no cross-owner
  selection, fallback, or pooling.
- The reverse-server external listener is bound to `127.0.0.1` only, and an
  agent account's `base_url` must be loopback.
- TLS verification is required and cannot be disabled in a release build.
- Enrollment tickets are single-use, short-lived, bounded, and never logged.
- The reverse client cannot perform general port forwarding; only the fixed
  local HTTP target is reachable through the tunnel.

### Capabilities

- A signed-in user attaches their own Eggpass by pasting one ticket from
  `eggpass eggpool`, without an inbound port, public hostname, VPN, or Tailscale.
- The user's agent models appear in their own model surface, labelled with their
  origin and deterministically disambiguated when names collide.
- The user and an operator can see when an agent is offline and why, without
  retrying into a misleading failure.
- Revocation is immediate and local: the user can disconnect; the operator can
  revoke an agent.
- Direct OAuth custody is removed once the agent path is qualified, with a
  documented migration for existing accounts.

### Infrastructure

- An embedded Eggress `1.0.12` reverse-server supervisor with generation-scoped
  configuration and bounded shutdown.
- A per-agent port and transport-credential lifecycle, with atomic config and
  binding publication and rollback.
- A bounded, memory-only pending-enrollment registry and ticket decoder.
- Per-account endpoint support in the provider client pool.
- Tunnel state projected into health, status, and routing eligibility.

### Polish

- Origin labels and collision suffixes in the model list.
- Dashboard and `/api/status` agent state.
- Operator documentation, migration notes, and troubleshooting.

## 3. Non-goals

- Public HTTPS callback or OpenAI partner registration.
- Hosted relay or rendezvous service.
- Direct LAN dialing of a user-supplied address.
- General-purpose remote port forwarding.
- Sharing one agent or provider subscription across users.
- Changing provider API-key accounts, quotas, or budgets.
- Tailscale/WireGuard integration; the tunnel is transport-independent.
- Replacing or forking Eggpass inside this repository.

## 4. Current state

- `AccountAuthMode` is `StaticKey | OAuth`; `AccountConfig` has no per-account
  `base_url`; `ProviderConfig.base_url` is provider-scoped
  (`rust/src/config.rs`).
- OAuth custody, refresh, and the 401 retry are implemented in
  `rust/src/oauth.rs`, `rust/src/coordinator/mod.rs`,
  `rust/src/coordinator/finite.rs`, and
  `rust/src/coordinator/streaming/coordinator.rs`.
- The Web UI xAI device-code flow is `rust/src/server/oauth_login.rs`; the
  self-only upload route is `rust/src/server/oauth_credentials.rs`; the CLI
  loopback flow is `rust/src/operations/oauth_login.rs`.
- The catalog keys models by `model_id` with provider rows and account support
  in `rust/src/catalog/cache.rs`; duplicate model names from two accounts of the
  same provider already merge.
- Eggpass runs a loopback OpenAI-compatible server with a client key and daemon
  state (`src/server/mod.rs`, `src/config.rs`, `src/daemon.rs` in the pinned
  Eggpass reference revision).
- Eggress `1.0.12` publishes the reverse tunnel (`eggress-runtime`,
  `eggress-protocol-reverse`, `eggress-embed`) with TLS and client
  certificates. EggPool does not depend on those crates yet.
- EggServe `0.4.0`/`0.5.0` has no TLS terminator and no reverse listener.
- Upstream tracking: `eggstack/eggpass#10` (ticket, reverse client, enrollment).

## 5. Target architecture

```text
Eggpass host                                  EggPool host
┌───────────────────────────────┐             ┌────────────────────────────────────┐
│ eggpass HTTP 127.0.0.1:8484   │             │ AgentTunnelManager                 │
│         ▲                     │    mTLS     │   reverse server per agent         │
│ reverse client ───────────────┼────────────▶│     control_bind  <configured>     │
│   fixed target 127.0.0.1:8484 │  control    │     external_bind 127.0.0.1:<port> │
└───────────────────────────────┘             │         ▲                          │
                                              │ provider client → 127.0.0.1:<port> │
                                              │ account auth_mode = "agent"        │
                                              └────────────────────────────────────┘
```

Configuration shape (illustrative, not a schema commitment):

```toml
[providers.openai.accounts.laptop]
auth_mode = "agent"
api_key_env = "EGGPOOL_AGENT_LAPTOP_KEY"
base_url = "http://127.0.0.1:19101"
[providers.openai.accounts.laptop.agent]
control_bind = "0.0.0.0:18443"
tls_cert = "/etc/eggpool/agents/laptop.crt"
tls_key = "/etc/eggpool/agents/laptop.key"
client_ca = "/etc/eggpool/agents/ca.crt"
require_client_cert = true
```

Ownership:

- `AgentTunnelManager` is process-owned, configured from the active generation,
  and replaced atomically on reload. It must not become a second config-reload
  authority.
- The account, binding, and routing model is unchanged; only the transport
  endpoint and credential source differ.
- An agent account has no OAuth credential and no ADR-0015 refresh path. A 401
  from the agent is the agent's problem; EggPool records the failure and never
  substitutes another account.

## 6. Dependency graph

| Edge | Kind | State |
|---|---|---|
| ADR-0016 acceptance → M001 | hard | satisfied |
| Eggress `1.0.12` reverse server/client availability → M001 | hard | available on the pinned line |
| M001 → M002 | hard | satisfied |
| `eggstack/eggpass#10` ticket producer → M002 end-to-end acceptance | interface (fixture) / operational (live) | fixture satisfied; live producer outstanding |
| M001, M002 → M003 | hard | satisfied |
| M002, M003, migration evidence → M004 | hard | satisfied; the migration evidence is M004's own deliverable |
| Provider OAuth roadmap → superseded | soft | on M004 closure |

## 7. Milestones

### Milestone 1 — Agent account and reverse-tunnel foundation

Class: infrastructure

Objective: Add the `agent` account kind, a per-account loopback endpoint, and the
embedded reverse-server supervisor, with a fixture tunnel proving a request can
be served end-to-end.

Dependencies: ADR-0016 accepted (2026-11-24); Eggress `1.0.12` reverse runtime available.

Deliverable boundary: configuration, client pool, tunnel manager, routing and
401 semantics for agent accounts, and a test fixture. No enrollment UI, no
removal of OAuth.

User or operator value: an operator can configure an agent account and route a
request through it.

Exit conditions: a fixture reverse tunnel serves `/v1/models` and a chat
completion through an agent account; routing selects only the bound owner; a 401
never triggers the OAuth refresh path; default and no-default suites pass.

Deferred work: enrollment, UI, model origin labels, removal.

### Milestone 2 — One-time enrollment and owner attach

Class: capability

Objective: Let a signed-in user paste one `eggpass eggpool` ticket and attach
their agent to their own account, with a per-agent transport credential and a
revocation path.

Dependencies: M001 closed; `eggstack/eggpass#10` ticket format agreed (a fixture
ticket producer is sufficient for implementation).

Deliverable boundary: ticket decode/validation, bounded pending enrollment,
credential issuance, atomic account and binding publication, CSRF-protected
attachments, revocation, audit events. No OAuth removal.

User or operator value: the full "connect my own subscription" journey without
an inbound port, VPN, or server-side token.

Exit conditions: tamper/replay/expiry matrix passes; a valid ticket creates the
account, binding, and tunnel; revocation closes the tunnel; no ticket or
credential appears in logs or diagnostics.

Deferred work: certificate issuance beyond the selected v1 credential shape,
operator bulk tooling.

### Milestone 3 — Agent model origin, discovery, and health

Class: capability

Objective: Make agent models discoverable and honestly labelled per origin, and
make offline agents visible and route-excluded.

Dependencies: M001 and M002 closed.

Deliverable boundary: catalog discovery through the tunnel, origin labels with a
deterministic numeric suffix on collision, per-user model projection, offline
state in status and dashboard, routing exclusion.

User or operator value: a user can tell where a model comes from and why an
account is unavailable.

Exit conditions: two agents exposing the same model render as distinct, stable
labels; an offline agent is excluded without fallback; status surfaces are
secret-free and bounded.

Deferred work: account-qualified request addressing (only if evidence requires
it).

### Milestone 4 — Retire direct OAuth custody

Class: capability

Objective: Remove the server-held OAuth credential path once agents can replace
it, with a documented migration and no silent credential loss.

Dependencies: M002 and M003 closed; a migration path for existing OAuth accounts;
evidence that agent accounts cover the capability.

Deliverable boundary: removal of `[oauth]`, `AccountAuthMode::OAuth`, the
credential store, the Web UI/CLI login surfaces, the backup members, and the
ADR-0015 coordinator path; configuration, backup, and documentation migration.

User or operator value: one account model, no server-side provider tokens, and a
clear path for existing users.

Exit conditions: no OAuth route, config key, credential file, or refresh path
remains; legacy files and backups have a documented disposition; default and
no-default suites pass; the provider-oauth roadmap is `superseded`.

Deferred work: none; this is the subsystem's terminal milestone.

## 8. Cross-cutting requirements

- **Storage/migration.** Reuse `personal_upstream_bindings`. Agent credentials
  live in an owner-only file or the config-adjacent `.env`, never SQLite. M004
  adds a backup format version without OAuth members and an explicit cleanup
  path for `.eggpool-oauth/`.
- **Protocol/ticket.** Versioned prefix, bounded size, base64url, no provider
  tokens, refusal on unknown version. One-time token only.
- **Security/auth.** TLS 1.2+, CA pin plus SNI, mTLS or per-agent password,
  loopback-only `external_bind` and `base_url`, no redirects, no arbitrary
  header forwarding, no general port forwarding.
- **Concurrency/cancellation/recovery.** Tunnel supervisor restart, reconnect
  backoff, bounded enrollment registry, generation replacement without dropping
  in-flight requests, atomic config publication with rollback.
- **Observability.** Control-connection state, tunnel errors, and origin labels;
  no tickets, credentials, provider tokens, or request bodies.
- **Performance.** One tunnel per agent, bounded streams; no active probing of
  sleeping machines.
- **Docs/ops.** Provider docs, CLI reference (Eggpass side), backup/restore,
  architecture deep dives, AGENTS.md ownership notes.

## 9. Verification strategy

- Tunnel fixture in `rust/tests/` using the real Eggress reverse server and
  client in-process, plus a fake agent HTTP server.
- Enrollment tamper/replay/expiry and revocation matrices in the server tests.
- Coordinator tests for owner-only selection, offline exclusion, and the absence
  of the OAuth refresh path for agent accounts.
- Catalog projection tests for deterministic origin labels and suffixes.
- Backup/migration tests for the M004 format change.
- Full serial workspace suite (default and no-default), strict Clippy, locked
  release build, and `cargo deny` after the dependency addition.

## 10. Risks and decision points

- **Upstream slip.** Eggpass ticket/client work is tracked in
  `eggstack/eggpass#10`; M002 can be implemented against a fixture but cannot be
  closed as a capability without the real producer.
- **Supervisor conflict.** `eggress-runtime` is a supervisor. If it cannot be
  embedded narrowly, M001 stops for an ADR revision rather than adopting a second
  config-reload authority.
- **Port allocation.** One reverse-server entry per agent implies a control and
  external port per agent. Operator-assigned ports are preferred over dynamic
  allocation for restart stability.
- **Credential lifecycle.** Certificate issuance and rotation are the largest
  security cost; v1 may use a per-agent password over pinned TLS.
- **Model identity.** Display-only labels must not leak into dispatch or policy;
  collision suffixes must be deterministic across reloads.
- **Migration.** Removing OAuth without a clear user path is a stop condition.
- **Availability.** Sleeping laptops are a product property; the UI must say so.

## 11. Completion definition

- Agent accounts are creatable, enrollable, routable, discoverable, and
  revocable without a server-held provider token.
- Direct OAuth custody, its configuration, its backup members, and its retry
  path are removed with a documented migration.
- The provider-oauth roadmap is superseded and its closure records remain
  historical.
- Default and no-default serial suites, strict Clippy, `cargo deny`, and the
  locked release build pass; operator docs describe the new model.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 1 | conditionally closed | [001](../implementation/provider-agents/001-agent-account-and-reverse-tunnel.md) | [001](../closure/provider-agents/001-status.md) | Capability complete and verified; the dependency-audit gate on a pre-existing SSH advisory owned by Security hardening M008 remains |
| 2 | conditionally closed | [002](../implementation/provider-agents/002-owner-enrollment-and-revocation.md) | [002](../closure/provider-agents/002-status.md) | Capability complete and verified; the live ticket producer in `eggstack/eggpass#10` remains |
| 3 | conditionally closed | [003](../implementation/provider-agents/003-agent-model-origin-and-health.md) | [003](../closure/provider-agents/003-status.md) | Capability complete and verified; the live ticket producer in `eggstack/eggpass#10` remains, and the pinned reverse server does not observe an idle control disconnect |
| 4 | conditionally closed | [004](../implementation/provider-agents/004-retire-direct-oauth-custody.md) | [004](../closure/provider-agents/004-status.md) | Capability complete and verified; the live ticket producer in `eggstack/eggpass#10` and the repository-wide dependency-audit gate owned by Security hardening M008 remain |
