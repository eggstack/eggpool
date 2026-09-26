# Provider Transport Roadmap

Status: active

Long-term references:

- `plans/000-long-term-specification.md` — provider transport remains a single bounded HTTP owner beneath coordinator policy; no second retry owner.
- `plans/001-terminology-and-domain-model.md` — request/admission and runtime-generation terminology remain authoritative.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — Eggfetch/Eggress requalification and provider-transport hardening are sustaining Phase 1 work.
- `plans/003-planning-process.md` — interim planning, dependency, and closure rules.

Related ADRs:

- None required for the current milestones. The durable Eggfetch/Eggress dependency and protocol boundary is already established by legacy Plans 215–220 and 241. A future milestone that changes transport ownership, enables a new protocol, or selects a new durable dependency must re-evaluate the ADR threshold.

## 1. Purpose and ownership boundary

This subsystem owns EggPool's provider-side HTTP transport adapter in
`rust/src/providers/transport.rs` and the immediate construction/test surface
around `ProviderHttpClient`, `ProviderResponse`, `ProviderBody`, and the
Eggress-backed Eggfetch `Dialer`.

The boundary consumes:

- provider/account routing and request policy from the coordinator/provider layer;
- exact-pinned `eggfetch-core` for HTTP/1.1 framing, destination TLS, connection pooling, physical admission, connect deadlines, and established transport I/O guards;
- exact-pinned Eggress outbound primitives for route/proxy establishment.

It must not own:

- coordinator retries, failover, health effects, or finalization;
- wire adaptation or terminal-event synthesis;
- provider credential policy;
- downstream server transport;
- updater/release-download HTTP;
- an EggPool-specific replacement for a missing general-purpose Eggfetch API.

## 2. Work classification

### Invariants

- One EggPool coordinator attempt produces at most one provider transport submission.
- Provider transport remains HTTP/1.1 only unless a separately reviewed protocol milestone changes that contract.
- Eggfetch logical retries, redirects, high-level URL/auth policy, compression, and built-in proxy routing remain disabled.
- A configured Eggress route fails closed and never falls back to direct networking.
- Direct and proxied accounts retain separate Eggfetch client/pool ownership.
- Origin TLS remains Eggfetch-owned after Eggress supplies the routed byte stream.
- Physical connection limits remain `PhysicalConnectionPolicy` limits, not logical request-concurrency limits.
- Stable `TransportError` categories remain the only transport errors exposed above this module.
- Response-body frame handling must be explicit: DATA remains incremental; HTTP trailers must never be silently reinterpreted as DATA, wire terminal evidence, or coordinator success.
- Transport diagnostics and `Debug` implementations must not introduce new credential/body/trailer-value exposure.

### Capabilities

- Direct HTTP/HTTPS provider transport.
- Eggress-routed provider transport across the supported route corpus.
- Incremental provider response-body consumption with bounded optional buffering.
- Stable timeout, cancellation, connection, TLS, protocol, and proxy failure classification.

### Infrastructure

- `eggfetch-core` native HTTP/1.1/custom-dialer integration.
- Eggress-to-Eggfetch `Dialer` adaptation.
- Provider-scoped pooling and physical connection admission.
- Typed transport error translation.

### Polish

- Remove brittle implementation-detail classification where local invariants permit a typed/fail-closed alternative.
- Keep transport-frame semantics and documentation explicit rather than implicit.
- Adopt future general-purpose Eggfetch classification helpers when they are published and qualified.

## 3. Non-goals

- No updater migration from its dedicated Hyper/Rustls release client.
- No Eggfetch high-level request-builder adoption.
- No HTTP/2 or HTTP/3 provider transport.
- No response decompression or `Accept-Encoding` behavior.
- No built-in Eggfetch proxy routing.
- No EggPool-owned proxy handshake implementation.
- No coordinator retry/failover changes.
- No provider/account selection changes.
- No new public downstream response-trailer behavior merely because upstream provider trailers become observable inside the provider transport.
- No local string-parsing compatibility layer for a future Eggfetch error taxonomy.

## 4. Current state

The current qualified baseline exact-pins `eggfetch-core =0.2.0` with
`default-features = false` and `native-http1,tls-rustls`. Legacy Plan 241
closed the 0.2.0 adoption with the provider transport, coordinator, no-default,
dependency, and footprint gates green.

`ProviderHttpClient::send` submits a single HTTP/1.1 request through
`Client::execute_http_body` with canceled-request retry disabled.
`build_eggfetch_client` configures physical admission, idle reuse, connect
timeout, established read/write inactivity, WebPKI roots plus test roots, and a
custom Eggress dialer for proxied accounts.

`ProviderBody::next` currently consumes Eggfetch `NativeResponseBody`
frames and returns DATA as `Bytes`; non-DATA frames are skipped. This means
valid HTTP response trailers are currently discarded at the adapter boundary.
No current provider/wire path depends on trailers, but the behavior is implicit
rather than part of the documented contract.

`map_eggfetch_error` is predominantly typed. It already uses Eggfetch's
physical-admission helper, typed timeout phases, typed transport-I/O direction,
typed Eggfetch variants, and typed custom-dialer facts. Two implementation
couplings remain intentionally visible for future work:

1. the broad source-chain inspection for Hyper cancellation/protocol facts and
   Rustls TLS facts where Eggfetch 0.2.0 does not expose an equivalent stable
   classification helper; and
2. a message-text inspection inside the residual `EggfetchError::Pool` arm
   for `"max_live"`, even though EggPool validates `max_connections > 0`
   before constructing the client.

The current provider transport integration test suite already exercises direct
and proxied request shape, TLS, keepalive, pool pressure, cancellation recovery,
timeouts, premature closes, body bounds, account isolation, supported Eggress
route families, route authentication/refusal, and fail-closed behavior.

Eggress is exact-pinned at 1.0.10 across the live outbound/test-support family.
Production uses the listener-free `eggress-outbound` `OutboundConnector` and
`connect_tcp_detailed` typed failure surface; the full `eggress-embed`/runtime
service facade is absent from the normal release path. The root `ssh` capability
enables `eggress-outbound/ssh` plus `eggress-pproxy-compat/ssh`, while
`--no-default-features` rejects SSH configuration before dialing and retains
direct/non-SSH proxy support.

Eggress 1.0.10 adoption and requalification completed under M003. The
v1.0.8..v1.0.10 range contains 44 commits and included changes relevant to
embedding consumers:
physical TCP metadata recovery, hop-zero SSH/H2 pooling/reuse isolation,
TLS-policy identity scoping for pooled H2, nested-hop pooling behavior, and
preservation of caller rustls trust/mTLS/verifier state during ALPN
adaptation. The public `OutboundConnector::from_pproxy_uri` and
`connect_tcp_detailed` surfaces used by EggPool remain available.

At the coordinator boundary, finite and streaming paths intentionally reduce
ordinary `TransportError` values to `FailureSource::Transport` for policy. The
observation model already has a separate `error_class` field, but current
transport failures generally record dispatch-phase labels rather than the
stable `TransportError` category. This is an observability gap, not a current
retry-policy correctness defect.

## 5. Target architecture

The provider adapter remains thin and policy-neutral.

Response framing should have an explicit two-level API:

- `ProviderBody::next()` remains the compatibility DATA-only streaming
  method used by existing callers;
- trailer frames encountered while consuming the body are retained as bounded
  HTTP metadata and made available through an additive post-consumption accessor
  (for example `take_trailers()`), without automatically forwarding them into
  EggPool's downstream HTTP/SSE surfaces;
- multiple trailer fields/values preserve HTTP header multiplicity;
- body/trailer internals are omitted or redacted from `Debug` output.

Error classification should use stable typed facts whenever Eggfetch exposes
them. EggPool should not parse `Display` text from upstream errors. Locally,
if the physical-admission helper has already ruled out an admission timeout,
residual `Pool` errors should map conservatively according to the validated
construction invariant rather than matching message strings.

A later milestone may consume a general-purpose Eggfetch error-kind/helper API
for cancellation/protocol/TLS/pool classification. That API belongs upstream
because it is useful to any embedding consumer and should not be invented as an
EggPool-only facade.

Separately, the coordinator may preserve the already-stable `TransportError`
category into bounded diagnostic evidence without changing `FailureSource`,
`FailureCategory`, retry scope, backoff, quarantine, or health effects. That
diagnostic propagation consumes `TransportError` only; it must not couple the
coordinator to Eggress/Eggfetch internal error types.

## 6. Dependency graph

- Hard: current provider/coordinator ownership from legacy Plans 215–220 and
  241 is closed and remains authoritative.
- Hard: exact-pinned `eggfetch-core 0.2.0` native/custom-dialer surface.
- Hard: exact-pinned Eggress outbound surface used by the current custom dialer.
- Interface: coordinator consumes only stable `TransportError` plus
  incremental DATA; M001 must preserve this interface.
- Soft: wire/runtime tests verify that making provider trailers observable
  internally does not change downstream finite/SSE semantics.
- Interface/blocker for future M002: a published, general-purpose Eggfetch
  typed classification surface that replaces the remaining Hyper/Rustls
  source-chain inspection without weakening error fidelity.
- M003 hard dependencies: the closed Plan-243 Eggress outbound boundary and a published Eggress 1.0.10 family; both are satisfied and M003 is closed.
- M003 coordination is resolved: M001 closed before the 1.0.10 requalification was accepted; no Eggress source adaptation was needed.
- M003 interface dependencies: `eggfetch-core 0.2.0` custom `Dialer` contract and Eggress `OutboundConnector` typed detailed-connect surface.
- M003 operational dependency: hosted CI/dependency-audit evidence is required for closure.
- M004 hard dependencies: M001 and M003 closure, so diagnostics target the final qualified provider-body/error adapter and current Eggress baseline.
- M004 does not depend on M002: it consumes EggPool's existing stable `TransportError` categories and does not replace Eggfetch source-chain classification.
- Deferred upstream simplification: an additive Eggress pproxy constructor accepting caller executor/TLS options could later eliminate the private test-root chain-executor seam. No EggPool-local replacement is authorized without that upstream contract.

## 7. Milestones

### Milestone 001 — Eggfetch adapter contract hardening

Class: invariant

Objective:

Make provider response-frame behavior explicit and remove the remaining
message-string classification from the local Eggfetch error adapter without
changing provider/coordinator/wire behavior.

Dependencies:

- Hard dependencies closed; no external blocker.

Deliverable boundary:

- additive trailer retention/access at `ProviderBody`;
- unchanged DATA-only compatibility behavior for existing callers;
- no trailer-to-wire forwarding;
- no message-text inspection in the residual Eggfetch pool error mapping;
- focused regression coverage plus provider/coordinator/no-default/full-suite
  qualification.

User or operator value:

No intended visible API behavior change. The value is a more faithful and
less brittle transport boundary that is safer to evolve as provider/protocol
behavior changes.

Exit conditions:

- valid upstream trailer frames are preserved without altering DATA order;
- existing callers that only call `next()` behave exactly as before;
- trailers do not become downstream SSE/JSON terminal semantics;
- `Debug`/diagnostics gain no trailer-value exposure;
- pool classification contains no upstream message-text matching;
- current transport and coordinator qualification remains green;
- closure record documents the exact compatibility behavior.

Deferred work:

- replacing remaining Hyper/Rustls source-chain inspection requires M002.

### Milestone 002 — Adopt stable Eggfetch transport error taxonomy

Class: infrastructure

Objective:

Replace remaining Eggfetch source-chain type inspection with a stable
Eggfetch-owned classification API while preserving EggPool's
`TransportError` contract.

Dependencies:

- Interface blocker: upstream Eggfetch must expose and publish a
  general-purpose typed classification/helper surface sufficient to
  distinguish cancellation, protocol/framing, TLS, admission/pool, and ordinary
  connection failures without `Display` parsing.

Deliverable boundary:

- exact-pinned Eggfetch version adoption;
- typed mapping only;
- no new EggPool-local compatibility facade;
- full provider/coordinator/dependency/footprint requalification.

User or operator value:

Lower coupling to Hyper/Rustls internals and safer future Eggfetch upgrades.

Exit conditions:

- no provider error classification relies on nested Hyper/Rustls downcasts
  where upstream now exposes equivalent typed facts;
- stable `TransportError` semantics remain unchanged;
- dependency/footprint impact is characterized.

Deferred work:

- any new protocol or retry feature remains separate.

### Milestone 003 — Eggress 1.0.10 adoption and requalification

Class: infrastructure

Objective:

Move the exact-pinned live Eggress family from 1.0.8 to 1.0.10 and requalify
the existing listener-free outbound boundary, with special attention to
SSH/multi-hop/TLS isolation, cancellation/recovery, account-client isolation,
and the unchanged typed route-error contract.

Dependencies:

- Hard dependencies closed; no external blocker.
- Soft coordination with M001 because both can touch `transport.rs`/`provider_transport`.

Deliverable boundary:

- exact package-family/lockfile upgrade;
- compatibility fixes only if 1.0.10 proves them necessary;
- default/test-support/no-default provider transport qualification;
- focused coordinator/wire non-regression;
- dependency/security/release footprint evidence;
- current-authority documentation and closure.

User or operator value:

Consume current upstream proxy transport fixes while preserving EggPool's
existing proxy behavior and ownership split.

Exit conditions:

- all live Eggress packages resolve at 1.0.10 from registry sources;
- production remains on `eggress-outbound` with no listener/runtime facade;
- fail-closed routing, typed route errors, proxy/origin TLS separation,
  one-attempt/one-submission, cancellation recovery, and account isolation
  remain qualified;
- default/no-default SSH contracts remain intact;
- graph/artifact deltas are measured and explained;
- closure record accepted.

Deferred work:

- coordinator diagnostic enrichment (M004);
- upstream API work to converge the private custom-proxy-CA test seam on the
  production pproxy constructor.

### Milestone 004 — Typed transport diagnostic evidence

Class: polish

Objective:

Carry the existing stable `TransportError` category into bounded
`FailureObservation` diagnostic evidence so operator/debug records distinguish
proxy authentication, target rejection, route failure, origin TLS,
pool/connect/read/write timeout, protocol failure, and cancellation without
changing coordinator retry/health policy.

Dependencies:

- Hard: M001 and M003 closed.
- Independent of blocked M002; consumes `TransportError` rather than Eggfetch internals.

Deliverable boundary:

- one static/credential-free `TransportError` diagnostic label authority;
- finite + streaming propagation;
- consumer/persistence compatibility audit;
- policy-invariance and redaction regression coverage;
- no retry/backoff/quarantine/account-health behavior change.

User or operator value:

More actionable transport diagnostics without changing routing decisions.

Exit conditions:

- equivalent finite/streaming failures emit the same bounded class;
- diagnostic labels contain no dynamic route/provider data;
- `FailureSource`/`FailureCategory`/`RetryScope`/action/effects remain unchanged;
- consumer compatibility is explicitly audited;
- closure record accepted.

Deferred work:

- any policy change based on detailed proxy classes requires a separate evidence-driven milestone.

## 8. Cross-cutting requirements

Storage/migration: none.

Protocol/compatibility: M001 is additive internally and must preserve existing
HTTP/1.1 request/response behavior. Upstream trailers are metadata only and
must not be synthesized into EggPool's public wire surfaces.

Security: never include provider trailer values, credentials, route secrets,
raw bodies, or prompts in diagnostics/closure evidence. Existing route-error
redaction remains mandatory.

Concurrency/cancellation/recovery: trailer retention must remain body-local;
dropping/cancelling a body releases the Eggfetch lease exactly as before.
No additional task, lock, or connection owner is introduced.

Observability: no new logging is required. If diagnostic facts are added, use
only bounded categories/booleans/counts, not raw trailer values.

Performance: trailer preservation must not buffer DATA or the complete
response body. Metadata storage is bounded by Hyper/header parsing limits and
must not add a second response-body buffer.

Documentation/operations: keep `architecture/deep-dive-providers.md`,
development skill guidance, and this roadmap synchronized with the implemented
contract.

Dependency sustaining: Eggress family bumps must be exact-pin/feature-graph
requalifications with immediate pre/post release footprint evidence. Historical
Plan-243 artifact measurements are evidence for that closure, not substitutes
for the current baseline after later repository changes.

Diagnostics: specific transport classes may be added to diagnostic-only fields
only when bounded and secret-free. Retry/health policy continues to consume
central `FailureSource`/`FailureCategory` unless a separate milestone changes it.

## 9. Verification strategy

M001 should extend the existing `rust/tests/provider_transport.rs` fixture
rather than introducing a parallel transport harness. Required proof includes
a chunked HTTP/1.1 response carrying declared trailers, DATA ordering, trailer
availability only after the trailer frame is observed, no trailer-value debug
leak, and unchanged behavior for ordinary responses.

Run the focused provider suite in default and `test-support` modes, then the
coordinator boundary/finalization/publication/wire-runtime targets, the
no-default surface, Cargo policy checks, and the serial workspace suite.

M002, when unblocked, must additionally compare the old and new error mapping
against synthetic typed Eggfetch errors and representative real-socket
failures.

M003 reuses `provider_transport` under default/test-support/no-default profiles,
then the focused C008/C009/C011 + boundaries/finalization/publication +
`wire_runtime` targets, Cargo feature/dependency/security inspection, locked
release build, and the serial workspace suite. Cancellation/recovery evidence
must use observable fixture gates rather than arbitrary sleeps.

M004 adds exhaustive `TransportError` label tests plus finite/streaming propagation
and a policy-invariance matrix proving that changing only `error_class` does not
change `FailureEffects`.

## 10. Risks and decision points

- If preserving trailers requires changing Eggfetch itself rather than a narrow
  consumer-side frame adapter, stop M001 and reassess scope.
- If an additive `ProviderBody` trailer accessor would alter a separately
  documented public crate/API compatibility contract, record that evidence
  before implementation and choose an explicit discard contract instead.
- If residual `Pool` errors can represent a legitimate runtime condition not
  identified by `is_physical_connection_admission_timeout()`, do not guess;
  preserve behavior and move that classification into the upstream M002
  prerequisite.
- If trailer forwarding to downstream clients becomes a product requirement,
  that is a separate protocol/wire milestone and may cross the ADR threshold.
- Eggress 1.0.10 changes pooled SSH/H2 and TLS-policy internals. M003 must qualify the EggPool account/client boundary rather than infer safety from compilation alone.
- The current test-support `eggress-server/ssh` cfg coupling may or may not remain necessary in 1.0.10; preserve it unless both feature graphs and fixtures prove removal safe.
- On proxied clients, Eggfetch's connect deadline spans route establishment and subsequent origin TLS establishment. `ProxyConnectTimeout` therefore means connection establishment timed out while using a proxy route, not proof that the proxy itself timed out.
- If M004 discovers `error_class` exact values are an external/persisted compatibility contract, stop and plan an additive compatible field or migration instead of silently renaming values.

## 11. Completion definition

This roadmap is not complete when M001 or M003 lands. It remains active until
all registered provider-transport sustaining milestones are closed or
explicitly deferred/superseded. M001 closure must leave runtime behavior
unchanged while making the response-frame/error boundary explicit. M002 may
remain blocked on its upstream interface without preventing other milestones.
M003 must leave the Eggress ownership/feature contract unchanged while moving
to the current qualified upstream family. M004 may close the roadmap once its
diagnostic-only evidence is accepted and no additional sustaining milestone is
registered.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 001 — Eggfetch adapter contract hardening | closed | `plans/implementation/provider-transport/001-eggfetch-adapter-contract-hardening.md` | `plans/closure/provider-transport/001-status.md` | none |
| 002 — Adopt stable Eggfetch transport error taxonomy | blocked | — | — | upstream Eggfetch typed classification API not yet available/published |
| 003 — Eggress 1.0.10 adoption and requalification | closed | `plans/implementation/provider-transport/003-eggress-1.0.10-adoption-and-requalification.md` | `plans/closure/provider-transport/003-status.md` | none |
| 004 — Typed transport diagnostic evidence | closed | `plans/implementation/provider-transport/004-typed-transport-diagnostic-evidence.md` | `plans/closure/provider-transport/004-status.md` | none |
