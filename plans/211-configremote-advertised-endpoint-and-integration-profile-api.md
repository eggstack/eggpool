# Plan 211: `configremote`, advertised endpoint, and integration-profile API

> **Status:** ready for implementation
>
> **Baseline:** EggPool `main` at `6997cdbb4cee4ec69c71cab15b53b039c7e3309a` (2026-09-16)
>
> **Parent:** Plan 209
>
> **Depends on:** Plan 210 portable profile/types boundary
>
> **Primary authority:** `rust/src/config.rs`, `rust/src/config_reload_policy.rs`, `rust/src/cli.rs`, `rust/src/runtime.rs`, `rust/src/operations/integrations.rs`, `rust/src/server/mod.rs`, `rust/src/server/health.rs`, `rust/src/server/middleware.rs`
>
> **Priority:** P0 — server-side remote setup entry point
>
> **Scope:** add a stable advertised integration endpoint, a read-only `eggpool configremote` command, and a separately versioned authenticated integration-profile API without changing standard `/v1/models`.

## Objective

A headless EggPool host should be able to produce one small, reusable, secret-free connection command for Codex/OpenCode clients running on other machines.

The server must publish two different classes of facts correctly:

- **bind/listen facts** — where the EggPool process listens locally;
- **advertised integration facts** — the URL desktop clients should use to reach it.

These are not equivalent when EggPool is behind DNS, a reverse proxy, Tailscale/WireGuard, NAT, or an explicit LAN interface.

This plan adds an explicit advertised URL contract and uses it to generate a portable `ConnectionProfileV1` token from Plan 210. The token points at a new EggPool-specific authenticated endpoint carrying current sanitized agent-integration metadata.

---

# Workstream 1 — Add an explicit advertised integration URL

Do not overload `[server].host`. Keep server binding and client advertisement separate.

Add a small typed config section, preferably:

```toml
[integrations]
advertise_base_url = "https://pool.example.internal/v1"
```

If repository naming conventions strongly favor another location/name, keep the same semantics: the value is the URL coding clients should place in their provider configuration, not a socket bind address.

### Validation

`advertise_base_url`, when set, must:

- be an absolute `http://` or `https://` URL;
- contain an authority/host;
- contain no userinfo credentials;
- contain no fragment;
- contain no control/whitespace characters;
- normalize a trailing slash deterministically;
- normalize to the expected EggPool API root (`.../v1`) without silently rewriting arbitrary path components;
- remain bounded in length.

Do not accept `file:`, `ssh:`, `data:`, or shell-like pseudo URLs.

### Reload classification

Add the new field to `config_reload_policy::classify_transition` intentionally.

Because the advertised URL affects only generated configuration/profile output and does not change the listening socket or request runtime, it should normally be live/non-disruptive. Prove this with transition tests. Do not force a service restart unless implementation coupling makes that necessary.

### Fallback behavior

For compatibility, existing `configsetup --host/--base-url` behavior remains unchanged.

For `configremote`:

1. explicit `--base-url` wins;
2. configured `integrations.advertise_base_url` is next;
3. detected LAN address may be offered as an explicit visible fallback only when it is structurally compatible with the actual listen configuration;
4. if EggPool is loopback-only or a reachable remote endpoint cannot be established safely, fail with guidance rather than emitting a misleading shareable command.

Do not claim that a reverse-proxy/public URL is reachable merely because the EggPool host can parse it. Optional active verification may be a separate `--verify` behavior; avoid making hairpin/reverse-proxy self-connectivity a requirement for generating a valid profile.

---

# Workstream 2 — Make integration context construction read-only for `configremote`

Current integration context construction can resolve/create a server key and may enable transcoding based on provider configuration. Those side effects are reasonable for existing local setup paths where explicitly designed, but `configremote` should be a read-only export operation.

Introduce a read-only context/projection builder that:

- reads validated EggPool config;
- reads current persisted catalog/model-info facts;
- merges static models/overrides through the existing authoritative projection path;
- resolves the advertised base URL;
- reports the auth reference (`EGGPOOL_API_KEY`) and whether server auth is configured;
- does **not** create/rotate keys;
- does **not** mutate transcoder settings;
- does **not** refresh provider catalogs;
- does **not** send upstream requests;
- does **not** alter routing/health/quota state.

If required integration prerequisites are absent, report them explicitly. Do not make export mutate the proxy to manufacture a setup.

---

# Workstream 3 — Add `eggpool configremote <target>`

Extend `rust/src/cli.rs` with a dedicated remote-setup command rather than overloading `configsetup --apply`.

Initial contract:

```text
eggpool configremote codex
eggpool configremote opencode
```

Recommended options:

```text
--base-url URL         override advertised URL for this profile
--format command|token|json
--shell auto|posix|powershell|all
--no-bootstrap         print token/profile only
```

Avoid a large matrix of flags. `configremote` should generate a connection profile, not become a remote filesystem manager.

### Default output

Default human output should make important security/topology facts visible:

```text
EggPool remote configuration
Target:   Codex
Endpoint: https://pool.example.internal/v1
Auth:     EGGPOOL_API_KEY (credential not included)
Profile:  epc1....

macOS/Linux:
  <version-pinned verified bootstrap invocation>

Windows PowerShell:
  <version-pinned verified bootstrap invocation>

This profile contains no credential and may be shared with authorized users
who can reach this EggPool instance.
```

Until Plan 214 publishes the real bootstrap artifact, implement `--format token/json` first or emit an explicit “bootstrap not yet available” rather than inventing a mutable URL. Do not land a command pointing to a nonexistent or unverified script.

### Machine-readable output

`--format json` should return a stable bounded object with fields such as:

- schema version;
- target;
- normalized advertised base URL;
- profile token;
- auth reference;
- bootstrap commands only when available.

Never include the resolved key.

---

# Workstream 4 — Add an EggPool-specific integration-profile API

Add a separately named endpoint, recommended:

```text
GET /api/integrations/v1/profile
```

Do **not** modify:

```text
GET /v1/models
```

The endpoint returns `AgentIntegrationProfileV1` from the Plan 210 crate using the same conservative projection as local `configsetup`.

### Authentication

The endpoint is not public merely because the returned object is sanitized. Use normal EggPool API authentication through the existing middleware/auth contract.

Do not add a second credential database or custom auth protocol in this plan.

If current middleware has route-specific loopback exemptions, explicitly test remote authentication behavior. The profile endpoint must not accidentally inherit a dashboard/public exemption.

### Response requirements

- deterministic schema version;
- normalized advertised base URL;
- deterministic revision/fingerprint;
- model entries in deterministic public-ID order;
- conservative capabilities/limits only;
- no API/provider key;
- no provider-private source metadata;
- no database/account IDs unless deliberately required and proven non-sensitive (prefer omission);
- no filesystem paths;
- no prompts/responses/tool payloads/user data;
- bounded response bytes/model count.

Add an appropriate `Content-Type` and ordinary cache semantics. Because authorization state/model catalog can change, avoid a long implicit shared cache TTL. An ETag/revision may be useful so helpers can avoid rewriting unchanged local state.

### Error semantics

Use bounded, generic errors:

- 401/403 for auth;
- 503/appropriate server status when integration projection is unavailable;
- no raw internal error/database/provider body in the response.

Do not trigger catalog refresh on GET.

---

# Workstream 5 — Revision/freshness semantics

The connection token should remain reusable even as models change. Therefore it references the profile endpoint rather than carrying the normal model inventory.

The integration-profile response should include a revision that changes whenever client-relevant sanitized content changes.

Compute the revision from canonical sanitized profile content, not from:

- database row IDs;
- timestamps alone;
- provider-private metadata;
- credentials;
- nondeterministic map ordering.

A helper can then report:

```text
remote revision changed -> local catalog/provider sync required
```

without comparing large arbitrary text blobs.

Do not promise that a revision is a cryptographic server identity. It is a content fingerprint.

---

# Workstream 6 — CLI/runtime integration and error mapping

`rust/src/runtime.rs` should remain the presentation/dispatch adapter. Put reusable construction/rendering in operations/shared crate code.

Add stable errors for:

- missing/unsafe advertised endpoint;
- unsupported remote target;
- profile serialization/bounds failure;
- integration profile unavailable;
- bootstrap artifact unavailable (if surfaced before Plan 214).

Ensure Debug/error output never prints resolved credentials. The new command should not require the service process to be running merely to encode static connection facts, but it must not pretend catalog-backed integration metadata exists when the configured database/catalog is unavailable.

Choose the least surprising behavior and document it: likely generate a token from config + available stored projection and let the desktop detect reachability/auth during install.

---

# Workstream 7 — Tests

Add focused deterministic tests covering:

### Configuration

- valid HTTP and HTTPS advertised URLs;
- trailing-slash normalization;
- invalid userinfo/fragment/scheme/control chars;
- no-op/live transition classification;
- loopback-only ambiguity behavior.

### CLI

- `configremote codex/opencode` parse contract;
- unknown targets rejected;
- JSON/token output stable;
- no secret in stdout/stderr/profile;
- explicit `--base-url` precedence.

### HTTP

- authenticated success;
- unauthenticated rejection;
- deterministic model order/revision;
- byte/model bounds;
- no secret/source metadata leakage;
- GET has no catalog refresh/provider traffic/health mutation;
- `/v1/models` response shape unchanged.

### Existing integrations

Run:

```bash
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::integrations
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
```

Add a focused integration-profile server test target only if it materially improves isolation; prefer extending the existing relevant server/operations suite rather than creating many tiny files.

---

# Documentation

After behavior lands, update:

- `architecture/deep-dive-integrations.md` — portable profile boundary and remote profile endpoint;
- `architecture/overview.md` — new CLI/API route and ownership;
- `docs/agent-configuration.md` — local vs remote setup workflows;
- `docs/api-reference.md` — versioned EggPool-specific profile endpoint;
- `docs/configuration.md` / `config.example.toml` / `config.sbc.example.toml` — advertised URL field;
- `docs/raspberry-pi.md` — headless proxy + desktop client example.

Do not document Plan 214 bootstrap commands until those exact release artifacts exist.

---

# Acceptance criteria

1. A typed advertised integration URL exists independently from the server bind host.
2. Its transition policy is tested and does not unnecessarily restart the proxy.
3. `eggpool configremote codex` and `eggpool configremote opencode` generate deterministic secret-free profiles.
4. `configremote` performs no server-key creation/rotation, config mutation, transcoder mutation, provider refresh, or upstream request.
5. Explicit `--base-url` overrides are supported and validated.
6. Unsafe/ambiguous loopback-only remote exports fail clearly rather than emitting a misleading command.
7. `GET /api/integrations/v1/profile` is authenticated, versioned, bounded, deterministic, and sanitized.
8. The endpoint is built from the same conservative projection as local client setup.
9. The endpoint exposes no credentials, provider-private source metadata, local paths, prompts, responses, or user data.
10. Standard `/v1/models` is byte/shape compatible with its existing contract; no Codex-private fields are added.
11. Profile revisions are canonical sanitized-content fingerprints, not timestamp noise.
12. CLI/HTTP error output remains bounded and secret-free.
13. Existing `configsetup` focused tests remain green.
14. Documentation clearly distinguishes listen/bind URL from advertised client URL.

## Handoff note

Land the advertised URL and read-only profile construction before exposing polished bootstrap output. Keep `configremote` an exporter: it should never reach across the network to edit client machines, and it should never alter EggPool configuration merely because a user asked how to connect a desktop.
