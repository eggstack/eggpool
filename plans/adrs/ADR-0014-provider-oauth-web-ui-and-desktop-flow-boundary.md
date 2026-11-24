# ADR-0014: Provider OAuth Web UI and Desktop Flow Boundary

Status: superseded

Date: 2026-11-23

Decision owners: project maintainers

Related specification sections:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`

Affected subsystem roadmaps:

- `plans/subsystems/provider-oauth-roadmap.md`

Related decisions:

- `plans/adrs/ADR-0012-provider-oauth-account-custody-and-lifecycle.md`
- `plans/adrs/ADR-0013-provider-oauth-eggfetch-compatibility.md`

## Context

ADR-0012 originally approved xAI Web UI device-code, OpenAI Web UI authorization-code/PKCE through an HTTPS callback on EggPool's listener, and desktop loopback PKCE for both providers. The implementation work exposed an important boundary: the OpenAI sign-in guidance and the validated Eggpass real-workload flow establish the desktop loopback redirect workflow; they do not establish the separately configured HTTPS callback URI hosted by EggPool. EggPool may adapt the protocol asynchronously, but that does not make the redirect URI or callback host equivalent.

The user explicitly selected “Keep OpenAI in M003.” This decision records that narrowed flow matrix while preserving ADR-0012's account custody and lifecycle invariants. The supplied OpenAI guidance is <https://developers.openai.com/siwc/token-sharing-open-source/sign-in.md>.

## Decision drivers

- Keep each provider flow within the exact redirect/callback surface supported by its documented and validated workflow.
- Do not introduce an EggPool-hosted OpenAI callback or a new canonical callback-origin configuration without evidence for that exact redirect contract.
- Preserve the existing authenticated owner, operator-gate, CSRF, and atomic credential-commit boundaries.
- Keep the M002 Web UI capability bounded to xAI device-code while retaining OpenAI authorization in the M003 desktop workflow.

## Considered options

### Option A — Keep OpenAI Web UI callback in M002

Keep the fixed HTTPS callback on EggPool's existing listener, with its separately configured origin. This preserves the original ADR-0012 matrix, but provider evidence for the validated desktop loopback flow does not prove support for this distinct EggPool-hosted HTTPS callback. It also retains callback routes, reverse-proxy query handling, and a dedicated configuration surface.

### Option B — Keep OpenAI in the desktop loopback milestone

Use xAI device-code in the authenticated Web UI, and keep both providers' authorization-code/PKCE flows in M003's ephemeral loopback listener. EggPool's login host remains asynchronous; it may adapt protocol primitives without changing the provider redirect contract. This removes the unsupported server-side callback surface while preserving the validated desktop workflow.

## Decision

Select Option B.

- M002 supports authenticated xAI Web UI device-code only.
- M003 supports xAI and OpenAI desktop authorization-code/PKCE through its loopback-only ephemeral callback.
- OpenAI Web UI start/cancel/commit routes, the fixed `/oauth/openai/callback` route, and `[oauth].callback_base_url` are not part of the current product surface and must remain absent.
- M001's per-user/provider/account custody, gate, owner checks, token secrecy, refresh coordination, backup/restore, and no-replay/no-fallback rules remain unchanged.

## Consequences

### Positive

- Current behavior aligns with the documented and validated desktop redirect workflow.
- No public-listener OpenAI callback, callback-origin setting, or callback-query deployment guidance is needed.
- The M002 UI retains xAI device verification and M003 remains the single OpenAI login host.

### Negative

- OpenAI authorization is not available from the authenticated Web UI; a desktop CLI is required.
- Remote/headless deployments cannot direct a remote browser to the local loopback callback; they must use an approved supported workflow or defer OpenAI login.

### Neutral or deferred

- Any future OpenAI Web UI flow requires new first-party evidence for the exact redirect/client-registration contract and a separate architecture decision before implementation.
- Async versus synchronous host implementation is an adapter detail and does not establish a different redirect URI contract.

## Compatibility and migration

This change removes only the M002 OpenAI Web UI callback/configuration surface. Existing OAuth credential files, owner bindings, M001 commit semantics, the OAuth enablement setting, and M003 desktop loopback behavior are preserved. Existing configurations containing the now-unsupported `[oauth].callback_base_url` field must no longer be documented or required; configuration parsing does not accept it. No stored-credential migration is needed.

## Security and reliability implications

The removal narrows the public callback attack surface and avoids callback-query exposure at a trusted ingress. Existing xAI Web UI actions continue to enforce session, CSRF, account ownership, live grant, and operator-gate checks. M003 continues to bind the callback to an ephemeral loopback listener, PKCE, state, exact callback transaction, and owner-bound M001 commit. Credentials remain outside SQLite and logs; no pooling, cross-user fallback, or automatic 401 replay is introduced.

## Verification

- Verify the removed OpenAI Web UI start/cancel/commit and callback routes return not-found while xAI device-code routes remain registered.
- Directly test that revoking the xAI upstream grant after successful polling removes the staged credential before commit.
- Retain M003 loopback OAuth tests for OpenAI and xAI, owner-bound upload tests, documentation/route-census checks, and default/no-default Rust verification.
- Verify exact provider registration/support separately; local stubs and this ADR do not establish live provider behavior or legal compliance.

## Supersession

This ADR supersedes only ADR-0012's provider/flow matrix insofar as it approves OpenAI Web UI authorization-code/PKCE through an EggPool-listener HTTPS callback, and the dependent M002 callback/configuration acceptance requirements. ADR-0012 remains an immutable historical record; its account custody, ownership, lifecycle, secret-handling, gate, refresh, and no-replay decisions remain in force. ADR-0013 is unaffected.
