# ADR-0016: Provider Agent Upstreams over Reverse TLS Tunnels

Status: accepted

Date: 2026-11-24

Decision owners: project maintainers

Related specification sections:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Affected subsystem roadmaps:

- `plans/subsystems/provider-agents-roadmap.md` (new)
- `plans/subsystems/provider-oauth-roadmap.md` (superseded when Provider Agents M004 closes)

## Context

EggPool is an on-prem gateway for small teams. Today the only way to attach a
user's own OpenAI or xAI subscription is for the EggPool server to run the OAuth
flow and keep the resulting credential set under `.eggpool-oauth/`, with
owner-scoped refresh and a single 401 retry (ADR-0012 through ADR-0015).

That custody model has two structural problems.

1. **The provider callback cannot reach a shared server.** OpenAI's OSS dynamic
   client pins `redirect_uri` to `http://127.0.0.1:<port>/auth/callback` from
   initial registration onward; only the port may vary, and the scheme, host,
   and path must not change. A server-hosted callback is only available through
   a separately provisioned partner client, which does not fit an on-prem
   deployment. The workarounds are a pasted callback URL or a CLI run on the
   server, and both put a provider code in transit.
2. **Token custody is the wrong trust boundary.** Personal refresh tokens should
   stay on the user's machine. It is also the model the provider documents: one
   agent host per installation, identified by `ext_agent_host_id`.

Eggpass already runs a loopback OpenAI-compatible server (`/v1/chat/completions`,
`/v1/models`, `/v1/usage`, `/health`), holds its issued client key, and refreshes
provider tokens locally. It is the correct owner of the OAuth grant.

Eggress `1.0.12` already provides a native reverse tunnel: a reverse server that
accepts control connections and dispatches accepted external connections back
through them, and a reverse client that dials out and services those requests
against a fixed local target. Both sides support TLS, including a client CA and
required client certificates on the server. EggServe `0.4.0` and `0.5.0` do not
terminate TLS (the `tls` feature gates only shared-kernel timeouts) and do not
provide a reverse listener, so EggServe is not the vehicle for this decision.

## Decision drivers

- No provider access or refresh token may be stored on the EggPool server for
  personal accounts.
- The user must not need an inbound port, a public DNS name, or a VPN/Tailscale
  account.
- The existing owner-binding, routing, quota, budget, and accounting model must
  be reused, not replaced.
- The transport must be encrypted and mutually authenticated; a bearer blob
  pasted over an unencrypted LAN is not acceptable.
- The new dependency must come from the already-reviewed Eggress family at the
  exact pinned version, with a narrow feature set.
- The change must be reversible per account: revocation is local and immediate.

## Considered options

### Option A — Keep server-side OAuth custody, improve the callback UX

Keep the current credential store and add a local relay client or richer paste
flow.

Benefits: no new dependency; smallest immediate delta.

Costs: the server still holds every user's refresh tokens; a server compromise
yields provider access for every user; the loopback constraint remains; the
relay artifact must be installed per user anyway. Rejected: it does not change
the trust boundary.

### Option B — Direct LAN connection to a TLS agent

Each Eggpass serves HTTPS on the LAN; EggPool dials it directly.

Benefits: simple, no reverse runtime, TLS is straightforward.

Costs: an inbound listener on the user's machine, firewall and NAT problems,
and EggPool gains an SSRF surface because it dials user-chosen addresses.
Rejected.

### Option C — Per-user Eggpass agent behind an Eggress reverse TLS tunnel

EggPool runs one Eggress reverse server per agent; Eggpass runs a reverse client
that dials out. EggPool reaches the agent on a server-local loopback port.

Benefits: no inbound port; no Tailscale requirement; encrypted and mutually
authenticated; no provider token on the server; EggPool never dials a
user-supplied address, so the SSRF surface disappears; reuse of the existing
binding and routing model; the family dependency is already on the reviewed
Eggress line.

Costs: a new exact-pinned dependency and embedded supervisor; per-agent port and
certificate lifecycle; availability follows the user's machine; an upstream
Eggpass feature is required.

Selected.

### Option D — OpenAI partner callback on a public HTTPS origin

Use the partner `oaiapp_` client with a registered callback.

Benefits: the conventional browser flow.

Costs: partner access, a public or at least registered HTTPS origin, TLS
certificate lifecycle, and it still leaves custody on the server. Rejected for
an on-prem product.

### Option E — Hosted relay service

EggPool or a third party hosts a rendezvous service so agents can be reached
without a tunnel.

Benefits: works across NAT without any local listener.

Costs: a new hosted service, an availability dependency, and a second identity
system. Rejected as out of scope.

## Decision

Adopt Option C.

1. **Agent accounts.** A configured provider account may declare
   `auth_mode = "agent"`. It carries a per-account loopback `base_url` pointing at
   the server-local reverse-server external port, plus the agent's local API key
   through the existing `api_key`/`api_key_env` fields. Owner binding, routing,
   quota, budget, and accounting reuse the existing account, binding, and
   eligibility model unchanged.
2. **Reverse tunnel.** EggPool embeds the Eggress `1.0.12` reverse server
   (exact pin, narrowest feature set that provides it) inside a process-owned
   `AgentTunnelManager`. One reverse-server entry exists per agent account, with
   an operator-assigned control port and a server-local `127.0.0.1` external
   port. Eggpass runs the corresponding reverse client, dialing out and
   forwarding to its local HTTP server.
3. **Credentials.** EggPool issues and stores a per-agent transport secret
   (password or client certificate) provisioned by a one-time enrollment ticket.
   mTLS is preferred; a per-agent password over pinned TLS is the minimum
   acceptable v1. The ticket is single-use, short-lived, and carries no provider
   token.
4. **Enrollment.** The user runs `eggpass eggpool`, copies one line into the
   authenticated Web UI, and the server validates, enrolls, binds the account to
   that user, and starts the tunnel. See `eggstack/eggpass#10`.
5. **Removal is sequenced, not immediate.** Direct OAuth custody remains in
   place while the agent path is built and qualified. Provider Agents M004
   removes `[oauth]`, `AccountAuthMode::OAuth`, the credential store, the Web UI
   and CLI login surfaces, the backup members, and the ADR-0015 coordinator
   retry. Until then both account kinds coexist.
6. **No general port forwarding.** The reverse client may only reach the fixed
   local HTTP target. It must not become a user-controlled remote forwarder.

## Consequences

### Positive

- Provider tokens never reach the EggPool server for personal accounts; local
  logout/rotation revokes access without server involvement.
- No inbound listener, public hostname, VPN, or Tailscale requirement on the
  user's machine.
- EggPool never dials an address supplied by a user, removing the SSRF surface a
  direct-LAN design would add.
- The owner-binding, routing, quota, and accounting model is reused rather than
  duplicated.
- The OpenAI loopback/registration problem disappears: the callback happens on
  the machine where `127.0.0.1` is correct.

### Negative

- A user's account is only available while their machine and Eggpass are awake;
  routing must surface an honest offline state rather than retrying.
- EggPool gains an embedded network supervisor, per-agent port allocation, and
  certificate/secret lifecycle.
- A new dependency from the Eggress family is added; its supervisor and config
  model must not duplicate EggPool's generation lifecycle.
- Eggpass must ship the reverse client and enrollment command; this ADR is not
  implementable end-to-end without that upstream work.

### Neutral or deferred

- `eggress-runtime` versus `eggress-embed` as the embedding surface is a
  milestone-level decision.
- Whether one reverse-server control listener can multiplex several agents, or
  one reverse server per agent is required, is an upstream question.
- The Eggfetch `0.4.0` pin from ADR-0013 remains in force until a separate
  dependency review; only its OAuth motivation retires.

## Compatibility and migration

- `auth_mode = "agent"` is additive. `auth_mode = "oauth"` remains valid until
  Provider Agents M004.
- The binding model is unchanged: an agent account is bound to exactly one user
  through `personal_upstream_bindings`.
- Existing `.eggpool-oauth/` files are not deleted implicitly. M004 adds an
  explicit migration/cleanup step and a backup format version that no longer
  carries OAuth members.
- `[oauth].enabled` remains until M004; a new agent gate is introduced with the
  agent account kind.
- Eggress stays exact-pinned at `1.0.12`; the new crate must be added at the
  same exact version and its feature graph reviewed.

## Security and reliability implications

- The tunnel control channel requires TLS 1.2+ with CA verification and SNI;
  verification cannot be disabled in release builds.
- Server-side authentication uses mTLS where available, otherwise a per-agent
  password. Both are revocable per agent.
- Enrollment tickets are single-use, short-lived, bounded, and never logged,
  persisted, or returned in diagnostics.
- The reverse server binds its external listener to `127.0.0.1` only. The
  account `base_url` is validated as loopback for `auth_mode = "agent"`.
- Redirects are not followed and arbitrary request headers are not forwarded.
- Availability is a documented property, not a failure to retry around: an
  offline agent excludes its account and never causes another user's account to
  be selected.

## Verification

Evidence required to prove conformance:

- an in-repository fixture tunnel serving `/v1/models` and one chat completion;
- enrollment tamper/replay/expiry matrix and revocation;
- offline-agent exclusion with no cross-owner fallback;
- default and `--no-default-features` serial suites, strict Clippy, locked
  release build, and `cargo deny` after the dependency addition;
- a documented migration path before M004 removes the OAuth surfaces.

## Supersession

On acceptance, this ADR supersedes **ADR-0012** (server-side credential custody
and lifecycle), **ADR-0014** (OpenAI Web UI callback boundary), and **ADR-0015**
(owner-scoped 401 credential refresh) for personal provider accounts. Their
status lines are then changed to `superseded`; their content is not rewritten.
Until acceptance they remain in force.

**ADR-0013** (single resolved Eggfetch `0.4.0`) remains in force for the current
provider transport pin; this ADR retires only its OAuth motivation, and a later
dependency review may revisit the version.

`plans/subsystems/provider-oauth-roadmap.md` becomes `superseded` when Provider
Agents M004 closes and the OAuth surfaces are removed.
