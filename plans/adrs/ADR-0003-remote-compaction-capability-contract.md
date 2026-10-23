# ADR-0003: Remote compaction capability contract for the Responses compact endpoint

Status: accepted

Date: 2026-10-22

Accepted: 2026-10-23 by the project maintainers, on explicit maintainer
direction. The acceptance revision is the commit that introduces this file
with the accepted status; `plans/closure/request-admission-wire/008-status.md`
records the implementing revision.

Decision owners: project maintainers

Related specification sections:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for provider wire surfaces and capability advertisement

Affected subsystem roadmaps:

- `plans/subsystems/request-admission-wire-roadmap.md`
- `plans/subsystems/shared-provider-profile-contract-roadmap.md`

## Context

`POST /v1/responses/compact` is a public HTTP surface with a complete
implementation. `rust/src/coordinator/endpoints.rs` exposes a dedicated
finite execution path for it, `rust/src/request/admission.rs` has a dedicated
admission function for it, the wire runtime carries a compaction capability
surface for it, and `rust/tests/codex_compaction_compat.rs` holds a dedicated
compatibility suite for it. The operator-facing API reference describes it.

The capability that makes it usable is advertised per provider wire surface.
`rust/src/config.rs` defines `supports_remote_compaction_v1` on a provider
wire surface configuration, defaults it to `false`, and validates that a
surface advertising the flag also supplies a `compact_path_template`.
`rust/src/wire/adapters.rs` projects that configuration into the kernel's
compaction capability type, and the finite coordinator path attaches it to
the wire runtime context.

No provider currently advertises the capability.

- The bundled provider profile asset at
  `rust/crates/eggpool-provider-profile/assets/_provider_profiles.toml`
  contains no compaction opt-in at all.
- `rust/config.example.toml` carries the flag only inside a commented-out
  example block.
- The repository's own rules state that a bundled model-wire hint stays
  `fixed = false` unless first-party evidence plus a runtime guarantee both
  exist, and that discovery evidence never proves a credential.

The practical consequence is that a real request to the public endpoint
cannot reach a capable upstream. The endpoint is fully implemented, fully
tested, and publicly documented, and it cannot succeed in a default
installation. The current operator-facing documentation acknowledges this
only obliquely, by describing the behavior as opt-in forward compatibility.

This is a public compatibility contract. The endpoint exists, it is
documented, and clients can call it. Deciding what the contract is — and
whether it stays — is therefore an architectural decision rather than a
local implementation detail.

## Decision drivers

- A public endpoint that cannot succeed is worse than one that fails
  explicitly, because an operator cannot tell a misconfiguration from a
  missing capability.
- Bundled provider capability claims must not be asserted without first-party
  evidence. Adding a bundled opt-in for a provider that does not document
  remote compaction would put an unverifiable claim into the shared profile
  contract.
- The endpoint has substantial sunk cost in implementation, tests, and
  compatibility suites. Removing it discards working machinery.
- Codex and other clients use local compaction by default, so the endpoint is
  not required for normal operation.
- The decision must not silently change the behavior of an installation where
  an operator has already configured a capable surface.

## Considered options

### Option A — Operator-opt-in endpoint with an explicit capability failure

Keep the endpoint, keep it operator-configured, and make the failure explicit
and typed when no eligible provider surface advertises the capability. The
capability advertisement stays out of the bundled provider profile asset
unless first-party evidence exists.

Benefits: preserves the implemented machinery and the compatibility suites;
makes the failure diagnosable; introduces no unverifiable bundled claim;
keeps the endpoint usable by an operator who has a provider that supports it.

Costs: a public endpoint remains that most installations cannot use; the
capability failure needs a distinguishable status and error code, which is a
public response-shape change.

Failure modes: an operator reads a capability error and does not know which
provider surface to configure. Mitigated by operator-facing documentation
naming the configuration keys.

### Option B — Add a bundled provider opt-in

Add `supports_remote_compaction_v1 = true` and a compact path template to a
bundled provider profile.

Benefits: the endpoint works out of the box for that provider.

Costs: requires first-party evidence that the provider actually supports
remote compaction, plus a runtime guarantee that the response shape matches
what the endpoint expects. If that evidence does not exist, the bundled asset
carries a false capability claim, which the provider-profile contract
explicitly forbids.

Failure modes: the bundled claim is wrong, every compact request against that
provider fails at the upstream, and the failure is harder to diagnose than
Option A's explicit capability error.

### Option C — Remove the endpoint

Remove the route, the finite execution path, the admission function, the
capability surface, and the compatibility suites.

Benefits: no public endpoint that cannot succeed; no documentation to
maintain.

Costs: discards working, tested machinery; removes a route that clients may
already call; is a breaking public change with no migration path for a
caller that configured a capable surface.

Failure modes: an existing caller that had configured a capable provider
surface loses a working capability without notice.

## Decision

Option A, with Option B available only as a separate, evidence-gated change.

Precisely:

1. `POST /v1/responses/compact` remains a public endpoint. It is not
   removed.
2. The remote compaction capability is an operator-configured provider wire
   surface fact. It is not asserted in the bundled provider profile asset
   without first-party evidence plus a runtime guarantee, per the existing
   provider-profile rules.
3. When no eligible provider surface advertises the capability, the endpoint
   fails with a typed, distinguishable capability outcome rather than a
   generic no-eligible-account outcome. The failure must be specific enough
   that an operator can tell a missing capability from a missing account.
4. Option B, a bundled provider opt-in, is a separate change requiring its
   own first-party evidence. It is not authorized by this decision.
5. The wire runtime context for the streaming path does not carry compaction
   capabilities, and it must not: the compact operation is finite-only. A
   divergence between the finite and streaming contexts here is intentional and
   must not be "corrected" into a shared builder.

Ownership boundaries: the endpoint execution and admission remain owned by
`rust/src/coordinator/endpoints.rs` and `rust/src/request/`; the capability
projection remains owned by `rust/src/wire/adapters.rs`; the configuration
surface and its validation remain owned by `rust/src/config.rs`; the bundled
provider profile asset remains owned by the shared provider profile contract.

## Consequences

### Positive

- A caller to the public endpoint receives a diagnosable failure instead of a
  generic selection failure.
- The operator-facing documentation can state the capability contract
  precisely instead of describing the endpoint as forward compatibility.
- No unverifiable capability claim enters the shared provider profile contract.
- The implemented machinery and its compatibility suites are retained.

### Negative

- A public endpoint remains that most installations cannot use.
- The capability failure introduces a distinguishable status and error code,
  which is a public response-shape change and must be documented as such.
- Operators must configure the capability themselves.

### Neutral or deferred

- Whether any bundled provider should advertise the capability is deferred
  until first-party evidence exists.
- The remote compaction version 2 capability flag remains unsupported and
  unchanged.

## Compatibility and migration

No storage migration. No configuration key is added or removed; the existing
provider wire surface keys are reused.

The public response contract changes for one case: a compact request that
finds no capable provider surface now returns a distinguishable capability
outcome instead of a generic selection failure. Clients that already treat
any non-success compact response as a failure continue to work. Clients that
branch on the specific status code must be documented in the API reference
change.

## Security and reliability implications

The capability failure must not disclose account, credential, or provider
configuration detail beyond what the existing selection diagnostics already
expose. It must not echo a request body.

No new authentication or authorization surface. The compact path keeps its
existing admission rules, including the stateless policy and the rejection of
a client-supplied compaction trigger.

No new contention, cancellation, or restart semantics. The capability check
happens during selection, before submission, and must not hold the selection
lock across any await.

## Verification

- A test asserts a compact request against an installation with no capable
  provider surface returns the distinguished capability outcome.
- A test asserts a compact request against a configured capable surface
  succeeds and reaches the upstream.
- A test asserts the streaming path does not carry compaction capabilities.
- A test asserts the capability failure body contains no credential, account
  secret, or request body material.
- The existing compact compatibility suite continues to pass.
- The API reference change is validated by the release documentation checks.

## Supersession

None. This decision supersedes no prior ADR.
