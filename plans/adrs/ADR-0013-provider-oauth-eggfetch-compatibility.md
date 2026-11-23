# ADR-0013: Eggfetch compatibility for provider OAuth

Status: accepted

Date: 2026-11-23

Decision owners: project maintainers

Related specification sections:

- plans/000-long-term-specification.md#2-end-state-invariants-normative
- plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms

Affected subsystem roadmaps:

- plans/subsystems/provider-oauth-roadmap.md
- plans/subsystems/provider-transport-roadmap.md (compatibility boundary only; M006 remains historical)

## Context

ADR-0012 selects eggpass-auth =0.1.1 as the exact OAuth runtime candidate while the OAuth branch was cut from baseline 5b212d2d69137b99ed39f0dbeab10331c839e54e, whose Cargo.lock pins eggfetch-core =0.2.2. Crate metadata and API inspection establish that eggpass-auth 0.1.1 uses Eggfetch 0.4 client types. A 0.2.2 Client is not type-compatible. A separate 0.4.0 OAuth client would create a second Eggfetch version and would not reuse the selected account's existing client/transport. The branch's minimal native-http1 plus tls-rustls profile is still available on 0.4.0.

The user explicitly approved a targeted 0.4.0 dependency upgrade with full provider-transport requalification on 2026-11-23. The separate dirty checkout contains other 0.4.0 transport edits; those source changes are not evidence or implementation input for this decision.

## Decision drivers

- Use the explicitly selected eggpass-auth =0.1.1 without incompatible or duplicate Eggfetch client types.
- Keep one resolved eggfetch-core version and reuse the selected account client.
- Preserve exactly the minimal native-http1 plus tls-rustls provider feature pair.
- Keep the OAuth PR limited to the dependency move and the smallest OAuth-required adapter changes; do not import unrelated transport changes.
- Requalify existing provider behavior and fail closed on any wider transport redesign.

## Considered options

### Option A — Keep Eggfetch 0.2.2 and add a separate 0.4 OAuth client

This preserves the old provider client but creates a second Eggfetch version with an incompatible Client type, does not reuse the selected account's transport, and expands the graph. Rejected.

### Option B — Move the single Eggfetch version to 0.4.0

Update the root exact pin and lockfile to eggfetch-core =0.4.0, retain native-http1 plus tls-rustls, and make only the adapter changes required to pass the existing selected account client to eggpass-auth. Requalify provider transport. Accepted by the user.

### Option C — Replace or downgrade eggpass-auth

This would depart from the explicitly selected latest runtime candidate and repeat protocol integration work. Rejected absent a new user decision.

## Decision

Approve the single-version move from eggfetch-core =0.2.2 to eggfetch-core =0.4.0 for the OAuth implementation, alongside eggpass-auth =0.1.1. Preserve only the native-http1 and tls-rustls features. Use one Eggfetch version across the resolved graph and reuse the selected provider/account client; do not create a second transport stack.

Only source changes demonstrably needed to expose/adapt that client for OAuth are in scope. Do not copy or transplant provider transport source from another checkout, change proxy/timeout/retry/TLS policy, enable additional Eggfetch features, or update Eggress. If compilation or qualification requires such changes, stop and request a separate decision. This ADR supersedes only the dependency-compatibility constraint in ADR-0012/M001; ADR-0012's 1:1 ownership, approved flow matrix, secret custody, and no-replay rules remain unchanged.

## Consequences

### Positive

- The selected OAuth library and EggPool share one compatible Eggfetch client type and one resolved Eggfetch version.
- The approved minimal provider feature profile remains unchanged.
- No new database schema or credential protocol is introduced by the dependency decision.

### Negative

- The provider transport implementation moves to a newer Eggfetch release and requires focused plus full requalification before merge.
- The dependency audit must distinguish issues in the selected dependency path from pre-existing findings without adding policy exceptions.

### Neutral or deferred

- This does not reopen or rewrite Provider Transport M006, and makes no claim that the new version is qualified until the required tests pass.
- The branch's prior cargo check --lib on a provisional single-version 0.4.0 graph is compile evidence only; no test or release qualification is claimed here.

## Compatibility and migration

There is no persisted-format or public HTTP change. Existing static-key accounts, account IDs, wire behavior, and provider configuration remain unchanged. The exact Eggfetch dependency and lockfile are updated; downstream transport semantics must pass the provider-transport test matrix before closure. The implementation must not add an Eggfetch duplicate or alter historical migrations.

## Security and reliability implications

Keep the existing provider transport boundaries: account-specific client identity, configured proxy/no-direct-fallback behavior, HTTP/1-only protocol, timeout/admission policy, typed failures, no hidden retry, and origin TLS policy. OAuth tokens remain secret and are resolved only for their bound owner/account. HTTP 401 never triggers refresh-and-replay or account fallback. Never log credentials or provider response bodies.

## Verification

Require cargo tree -e features -i eggfetch-core to show exactly one eggfetch-core 0.4.0 node with the minimal feature pair, cargo tree --duplicates to show no new duplicate, and cargo deny review with no new exception. Run focused provider_transport and OAuth tests; default and no-default check/strict Clippy; serial workspace tests; and a locked release build. If advisory checks fail, compare with the original baseline and report the exact finding; do not suppress it or modify unrelated Eggress/Russh dependencies under this ADR.

## Supersession

This ADR supersedes only the Eggfetch-version compatibility constraint for the OAuth work in ADR-0012 and M001. All other ADR-0012 decisions remain accepted.
