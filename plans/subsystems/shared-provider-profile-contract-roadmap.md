# Shared Provider Profile Contract Roadmap

Status: closed

Repository baseline reviewed: `b73eea6bc4ecdb6ce6b8974769b7821cf5e7e036`

Long-term references:

- `plans/000-long-term-specification.md` — deterministic provider configuration, protocol correctness, secret-safe operation, and optional downstream integrations.
- `plans/001-terminology-and-domain-model.md` — provider identity, wire surface, model catalog, and configuration authority.
- `plans/002-long-term-roadmap.md` — reusable Rust substrate and provider correctness.
- `plans/003-planning-process.md` — bounded infrastructure extraction and evidence-gated handoff.

Predecessor work:

- `plans/subsystems/provider-profile-metadata-corrective-roadmap.md` and M001 closure `plans/closure/provider-profile-metadata/001-status.md`.
- `plans/subsystems/provider-profile-metadata-planning-reconciliation-corrective-roadmap.md` and C001 closure.
- `plans/subsystems/request-admission-wire-roadmap.md` M006, which established the sibling-consumable `eggpool-wire` precedent.
- `plans/173-shared-model-routing-crate-roadmap.md`, which established the sibling-consumable `eggpool-model-routing` precedent.

Related ADRs:

- None required for M001. The milestone extracts an existing secret-free metadata boundary without moving provider runtime, account, credential, routing, health, or persistence authority. The first downstream consumer must pin an immutable EggPool revision. If implementation would publish a semver-stable public provider registry, make EggPool runtime depend on a sibling process, or move credential/account authority into the shared crate, stop and write an ADR.

## 1. Purpose and ownership boundary

EggPool already owns a mature secret-free description of provider contracts: base URLs, supported wire surfaces, per-surface paths and authentication shapes, model-discovery policy, verification hints, and model-wire preferences. Those facts currently live partly in `rust/src/config.rs` types and partly in `rust/assets/providers/_templates.toml`, so sibling consumers must duplicate them.

This workstream extracts a neutral Rust provider-profile contract that EggPool itself consumes and that CodeGG can pin directly. The shared contract describes provider facts; it does not execute HTTP, hold credentials, select accounts, route requests, maintain health, enforce quota, or persist state.

## 2. Trigger

A CodeGG `/connect` and OpenCode Go review exposed two forms of drift that a shared contract can prevent:

- model discovery and credential verification were conflated even though an endpoint can be public or a local model list can be static;
- OpenCode Go exposes OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages under one provider, with surface-specific authentication, while CodeGG currently models the provider as one OpenAI-compatible Chat transport.

Current first-party OpenCode Go documentation reviewed 2026-10-07 lists model-specific endpoint families and states that `/zen/go/v1/models` returns the available model list. A direct read of that endpoint returns ordinary OpenAI-style model rows without a wire-surface field, so model-to-wire hints cannot safely be inferred from that catalog alone.

## 3. Invariants

- The shared crate is secret-free and sans-I/O: no API-key values, credential stores, environment reads, filesystem reads at runtime, HTTP, async runtime, database, logging, clocks, or random state.
- EggPool remains the runtime authority for accounts, credentials, catalog refresh, routing, quota, health/quarantine, retries, transport, persistence, and wire negotiation.
- `eggpool-wire` remains the authority for canonical request/response/event semantics and wire codecs. Provider profiles reference wire surfaces; they do not duplicate codecs.
- One canonical embedded provider-profile data source must back both EggPool and sibling consumers. Do not land two independently edited copies of endpoint/auth/profile metadata.
- Existing provider IDs and EggPool operator configuration semantics remain compatible.
- Model-wire hints are advisory unless explicitly marked fixed. Unknown or newly added models remain unresolved rather than guessed from model-family names.
- The crate must be usable by a sibling Rust consumer without importing the EggPool root package or daemon runtime.
- Initial downstream use is by immutable Git revision. Crates.io publication or a semver stability promise is separate work.

## 4. Non-goals

- Sharing account configuration or API-key environment values as credentials.
- Moving catalog refresh, live provider verification, wire negotiation, routing, retries, or health into the crate.
- Making EggPool the external source of truth for provider facts; first-party provider documentation remains authoritative.
- Runtime web scraping or CI network freshness checks.
- Hard-coding provider-family guesses for models absent from reviewed metadata.
- Replacing `eggpool-wire` or `eggpool-model-routing`.
- Requiring the EggPool daemon for CodeGG direct-provider operation.

## 5. Current state

At the baseline:

- `ProviderConfig` in `rust/src/config.rs` already has `auth`, `wire_surfaces`, `models_endpoint`, `verify`, and `model_wire` structures.
- `rust/assets/providers/_templates.toml` carries the reviewed bundled provider metadata. OpenCode Go has the correct `https://opencode.ai/zen/go/v1` prefix, Chat/Responses/Messages paths, Bearer auth for Chat/Responses, and `x-api-key` for Messages.
- EggPool's process-owned wire resolver can consume configured/model metadata hints while retaining bounded reactive negotiation.
- `eggpool-wire` intentionally excludes credentials, catalogs, provider clients, and routing.
- CodeGG already pins `eggpool-wire` and `eggpool-model-routing`, but still maintains overlapping provider setup metadata locally.

## 6. Target architecture

```text
first-party provider documentation
          |
          v
canonical embedded provider-profile data
          |
          v
eggpool-provider-profile
  - ProviderProfileRegistry
  - ProviderProfile
  - WireSurfaceProfile
  - AuthProfile
  - ModelsEndpointProfile
  - CredentialVerificationPolicy
  - ModelWirePreference
          |
          +-------------------+
          |                   |
          v                   v
      EggPool root         CodeGG
  local runtime adapter   immutable Git pin
  accounts/routing/etc.   direct-provider adapter
          |
          v
      eggpool-wire
  canonical codecs/events
```

## 7. Milestones

### Milestone 001 — Neutral provider-profile contract and OpenCode Go surface hints

Status: implemented (closed).

Primary class: infrastructure.

Implementation plan:

- `plans/implementation/shared-provider-profile-contract/001-neutral-provider-profile-contract-and-opencode-wire-hints.md`

Dependencies:

- hard: provider-profile metadata M001/C001 closed — satisfied;
- interface: `eggpool-wire` surface identifiers and the existing EggPool provider-template/config semantics — stable;
- operational: CodeGG will consume only an immutable closure revision.

Objective: extract the secret-free provider-profile contract and canonical embedded profile data into a sibling-consumable crate, cut EggPool over to that single source, and add non-fixed current OpenCode Go model-wire hints required by downstream direct-provider consumers.

Exit conditions:

- one canonical profile data source exists;
- EggPool behavior/profile parsing remains equivalent except for additive non-fixed OpenCode Go wire hints;
- the crate has no runtime I/O or secret-bearing types;
- CodeGG can resolve provider endpoint, per-surface auth/path, discovery policy, verification policy, and current documented OpenCode Go model-wire hints from the crate without importing EggPool runtime code;
- unknown OpenCode Go model IDs stay unresolved, not guessed;
- focused profile/config/wire-resolver tests plus default/no-default workspace qualification pass;
- closure records an immutable downstream revision.

## 8. Cross-cutting requirements

Security: auth profiles describe header names/schemes only; no secret values or credential references. Debug/serialization fixtures must not make it possible to smuggle secrets into profile metadata.

Compatibility: preserve all existing provider IDs and effective bundled profile values. Explicit operator configuration must continue to override bundled defaults according to current EggPool semantics.

Failure semantics: malformed embedded profiles fail closed at construction/build/test time. A missing model-wire hint is an unresolved capability, not an instruction to choose Chat Completions.

Performance: embedded profile lookup is bounded and allocation-conscious. Do not add network work or background tasks.

Documentation: architecture/provider/catalog docs must identify the shared crate as the secret-free metadata owner while keeping first-party documentation as external authority.

## 9. Completion definition

This roadmap closes when M001 has an accepted closure record, EggPool consumes the shared profile contract without a second live metadata copy, the OpenCode Go profile contains reviewed non-fixed model-wire hints, and an immutable revision is available to CodeGG.

## 10. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| M001 neutral provider-profile contract and OpenCode Go surface hints | closed | `plans/implementation/shared-provider-profile-contract/001-neutral-provider-profile-contract-and-opencode-wire-hints.md` | `plans/closure/shared-provider-profile-contract/001-status.md` (implementation `9ac6a131`) | none — roadmap terminal; no successor registered |
