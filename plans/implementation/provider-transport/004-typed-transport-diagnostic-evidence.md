# Provider Transport Milestone 004 — Typed transport diagnostic evidence

Status: blocked

Repository planning baseline: `8bf99e7cb7d3a4c8e0cad383be8efb4ffa9cb91f`

Execution baseline: MUST be refreshed after Provider Transport M001 and M003 close.

Source roadmap:

- `plans/subsystems/provider-transport-roadmap.md#milestone-004--typed-transport-diagnostic-evidence`

Long-term requirements:

- `plans/000-long-term-specification.md` §2 — secret-free diagnostics.
- `plans/000-long-term-specification.md` §3 — provider client ownership remains generation-scoped.
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining` — provider transport sustaining work.
- `plans/003-planning-process.md` — polish must not silently become a policy/capability change.

Applicable ADRs:

- None required for diagnostic-only propagation. If inventory shows `error_class` is an externally versioned compatibility contract or work would alter retry/health semantics, stop and reassess ADR/migration requirements.

Primary class: polish

Hard dependencies:

- Provider Transport M001 closure.
- Provider Transport M003 closure.

M004 does NOT depend on blocked M002. M002 concerns replacing Eggfetch-internal source-chain inspection; M004 consumes EggPool's already-stable `TransportError` enum above that boundary.

## 1. Objective

Preserve the existing stable `TransportError` category as bounded, credential-free diagnostic evidence when provider transport failures become coordinator `FailureObservation` records, so finite and streaming diagnostics can distinguish proxy authentication, proxy target rejection, proxy route/connect failure, pool/connect/read/write timeout, origin TLS, protocol, cancellation, and related transport classes.

This milestone must not change `FailureSource`, `FailureCategory`, `RetryScope`, retry budgets, account penalties, persistent backoff, quarantine, circuit effects, provider attribution, client status mapping, or upstream-submission count. The value is observability only.

## 2. Why this milestone is blocked

The code already exposes the necessary typed `TransportError`, but M001 and M003 intentionally settle the provider-body/error adapter and Eggress baseline first. After both close, refresh current evidence and promote M004 to ready only if the assumptions below still hold.

## 3. Current implementation evidence

At the planning baseline:

- `rust/src/providers/transport.rs` exposes a stable `TransportError` enum covering configuration/proxy configuration/invalid target/body bounds/pool timeout/direct connect timeout/failure/proxy connect timeout/failure/proxy authentication/proxy target connect/origin TLS/write/read timeout/failure/protocol/cancellation.
- Eggress detailed route errors already map into these categories without message parsing; M004 must not reach through `TransportError` to inspect Eggress internals.
- `rust/src/coordinator/finite.rs` maps `ResponseBodyTooLarge` specially, while ordinary `AttemptError::Transport(_)` becomes `FailureSource::Transport` with no category hint. Its observation helper currently sets `error_class` from generic dispatch-phase labels such as `transport`/`body_read`.
- `rust/src/coordinator/streaming/` has an equivalent observation boundary.
- `rust/src/coordinator/failure.rs` classifies `FailureSource::Transport` as `FailureCategory::TransientTransport` and applies established account/circuit/backoff/retry behavior. Current policy does not branch on `FailureObservation.error_class` or `transport_phase`.
- `FailureObservation` already contains `transport_phase` and `error_class`, providing a diagnostic channel separate from policy-driving source/category fields.

Before implementation, inventory every serializer, database/finalization consumer, API projection, metric/event sink, and test assertion touching `FailureObservation.error_class` or `transport_phase`. Repository evidence decides whether a compatibility migration/additive field is required.

## 4. Invariants that must not regress

- `TransportError` remains the only provider transport category exposed above `providers/`; coordinator code must not match Eggress/Eggfetch internal types.
- FailureSource for ordinary transport failure remains `Transport`.
- `FailureCategory` and `FailureEffects` for the same request/error remain unchanged.
- `ProxyAuthentication` must not be reinterpreted as provider API-key authentication; it describes the outbound proxy.
- Route TLS and provider/origin TLS remain distinguishable by existing `TransportError` categories.
- No label contains hostnames, IPs, usernames, passwords, proxy URIs, provider URLs, keys, prompts, bodies, cache keys, raw error strings, or arbitrary upstream text.
- Finite and streaming paths use one label authority.
- `ResponseBodyTooLarge` keeps existing provider-response/fatal handling.
- Cancellation/downstream-started replay prohibition remains unchanged.
- No retry/failover/health decision may branch on `error_class` in M004.

## 5. Scope

### In scope

- rebaseline after M001/M003 and inventory `error_class`/`transport_phase` consumers;
- define one exhaustive bounded/static label mapping from `TransportError`;
- feed that label into finite/streaming `FailureObservation.error_class` where an actual `TransportError` exists;
- preserve existing dispatch/transport phase semantics unless inventory proves otherwise;
- mapping/propagation regression tests;
- policy-invariance tests proving identical `FailureEffects` before/after diagnostic enrichment;
- architecture/observability docs if they describe generic-only transport evidence;
- closure record and registry/roadmap transition.

### Explicitly out of scope

- no new `TransportError` variants unless a separate correctness defect is found;
- no Eggress/Eggfetch dependency or feature changes;
- no retry scope/action/backoff/quarantine/circuit changes;
- no new HTTP status/client error mapping;
- no raw source-chain/string logging;
- no persistence schema change without separately approved migration scope;
- no dashboard/API redesign;
- no metrics-cardinality expansion from host/proxy/provider identifiers;
- no `direct://`, proxy URI parsing, or account-routing changes.

## 6. Required production changes

### Single diagnostic label authority

Create one exhaustive/static mapping for every `TransportError` variant. The location may be the `TransportError` owner (for example a `diagnostic_class()` method) or a coordinator-local helper if evidence favors keeping diagnostic vocabulary above providers/. Do not duplicate match tables in finite and streaming code.

The mapping must return bounded `&'static str` labels. Recommended vocabulary, subject to the consumer inventory:

- `configuration`
- `proxy_configuration`
- `invalid_target`
- `request_body_too_large`
- `pool_timeout`
- `connect_timeout`
- `connect`
- `proxy_connect_timeout`
- `proxy_connect`
- `proxy_authentication`
- `proxy_target_connect`
- `tls`
- `write_timeout`
- `write`
- `read_timeout`
- `read`
- `response_body_too_large`
- `protocol`
- `cancelled`

Do not include Display/Debug/source text in labels.

### Coordinator propagation

When `AttemptError::Transport(error)` becomes a `FailureObservation`, retain existing source/category-hint behavior and set `error_class` from the stable label. For body-read errors returned after response headers, use the same authority while preserving response/downstream/retry legality.

Apply the same rule to streaming observation sites where an actual `TransportError` exists. Do not synthesize a transport class when none exists. Keep `dispatch_phase` and `transport_phase` meanings stable by default; M004 should normally change only `error_class`.

### Policy-invariance guard

Add tests comparing representative observations with the old generic diagnostic class and new specific class and assert `classify()`/`FailureDecisionEngine` returns identical policy-driving outputs. Cover at least `ProxyAuthentication`, `ProxyTargetConnect`, `ProxyConnectTimeout`, `Tls`, `ReadTimeout`, `Cancelled`, and direct `Connect`.

Compare category, retry scope/action, account/circuit/model/wire effects, persistent-backoff decision/reason/duration, provider attribution, retry boolean, and client outcome. Diagnostic/evidence strings may differ only where explicitly intended.

### Consumer/persistence audit

Search all Rust/tests/docs/schema/migration consumers of `error_class` and `transport_phase`; classify each as ephemeral internal, persisted but non-contractual, externally projected, or compatibility-sensitive.

If exact `error_class` values are a documented external/schema/migration contract, stop. Prefer an additive dedicated transport diagnostic field under separately planned compatible scope rather than silently changing a durable contract.

## 7. Ordered work packages

### Work package A — Rebaseline and consumer inventory

Intent: prove the field is safe to enrich after M001/M003.

Acceptance evidence: both closure links; current transport enum; complete producer/consumer inventory; explicit compatibility classification.

### Work package B — Introduce single label authority

Intent: prevent finite/streaming drift and keep labels secret-free.

Acceptance evidence: exhaustive mapping with unit coverage for every enum variant; static bounded labels only.

### Work package C — Propagate finite evidence

Intent: preserve typed category at finite coordinator boundary.

Acceptance evidence: submit/body-read paths emit expected labels with unchanged `FailureEffects`.

### Work package D — Propagate streaming evidence

Intent: match finite semantics without reopening replay after handoff.

Acceptance evidence: pre-handoff policy unchanged; post-handoff failures remain terminal/non-replayable; equivalent errors produce equivalent labels.

### Work package E — Policy invariance/redaction qualification

Intent: prove observability-only behavior.

Acceptance evidence: policy outputs unchanged for representative classes; diagnostics contain labels only, never route/provider secrets or raw error strings.

### Work package F — Full verification/docs/closure

Intent: close against repository authority.

Acceptance evidence: §11 gates green; serial suite green; docs validators green; `plans/closure/provider-transport/004-status.md` accepted.

## 8. Failure, cancellation, restart, contention semantics

This milestone changes evidence, not behavior.

- Proxy authentication/target rejection remain ordinary transport-source failures for current retry/account-health policy.
- Cancellation remains cancellation/future-drop semantics; a `cancelled` label must not make post-handoff streaming work retryable.
- Read/write failures after downstream handoff remain terminal according to current coordinator ownership.
- Restart/reload/generation retirement is unaffected; no diagnostic state reconstructs a transport client.
- Concurrent failures emit static labels only; no global mutable registry or unbounded cardinality is introduced.

## 9. Compatibility and migration

No intended config/API/database migration.

The consumer inventory is a gate. If `error_class` is persisted/exposed but documented as opaque diagnostic vocabulary, refinement may proceed with evidence. If exact values are contractual, do not overwrite them; use an additive compatible field under separately approved scope.

`TransportError` Display strings remain unchanged. M004 consumes enum identity, not message text.

## 10. Required tests

Unit:

- every `TransportError` maps to one expected static label;
- conservative character/length bound;
- no dynamic source text.

Policy invariance:

- generic vs specific `error_class` yields identical `FailureEffects` for representative direct/proxy/TLS/read/cancel classes;
- `ResponseBodyTooLarge` retains special fatal/provider-response behavior.

Finite integration:

- submit-time proxy authentication/target/connect-timeout labels with unchanged retry outcome;
- body-read read-timeout/protocol labels with unchanged replay behavior.

Streaming integration:

- pre-handoff failure label + unchanged retry behavior;
- post-handoff failure label + no replay;
- cancellation remains recoverable/non-synthetic.

Reuse existing coordinator/provider fixtures; do not create a second classification harness unless necessary.

## 11. Required verification commands

After promotion/rebaseline, at minimum:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --no-default-features -- --test-threads=1
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
```

If no dependency files change, `cargo deny`/release artifact remeasurement is not mandatory. If Cargo metadata changes unexpectedly, run full dependency-policy gates and explain why.

## 12. Documentation updates

- `architecture/deep-dive-providers.md`: stable `TransportError` diagnostic labels vs retry policy.
- `architecture/deep-dive-retry.md` and/or `architecture/deep-dive-observability.md`: `error_class` is evidence while source/category drive policy, if current docs conflate them.
- relevant `.opencode` skills only if current guidance requires generic labels.
- `plans/closure/provider-transport/004-status.md`: consumer inventory and policy-invariance matrix.

Do not edit historical Plan 243 to claim it propagated diagnostics.

## 13. Acceptance criteria

1. One exhaustive static mapping owns `TransportError` diagnostic labels.
2. Finite/streaming observations preserve the same class for the same error.
3. No label contains dynamic/secret-bearing material.
4. Existing source/category/retry/action/account/circuit/backoff/quarantine/client-outcome behavior is unchanged.
5. `ResponseBodyTooLarge` and cancellation retain established exceptional semantics.
6. No retry decision branches on `error_class`.
7. Consumer/persistence compatibility is explicitly audited/preserved.
8. Default/no-default focused and serial workspace tests pass.
9. Closure record is accepted.

## 14. Stop conditions

Stop rather than improvise if:

- M001 or M003 is not closed or final `TransportError` differs materially from this planning baseline;
- `error_class` exact strings are an externally versioned/migration-sensitive contract with no additive-compatible field in scope;
- labels require changing failure category/retry/health/backoff policy;
- a desired distinction exists only inside Eggress/Eggfetch and is not represented by `TransportError`;
- propagation requires raw source errors/proxy URIs;
- streaming changes would make post-handoff failure replayable;
- work expands into dashboard/API redesign or schema migration.

## 15. Closure evidence required

`plans/closure/provider-transport/004-status.md` must contain:

- M001/M003 closure references and refreshed execution baseline;
- complete `error_class`/`transport_phase` consumer inventory + compatibility classification;
- final `TransportError`-to-label mapping;
- finite/streaming producer sites changed;
- unit/integration results for label families;
- policy-invariance matrix showing unchanged `FailureEffects`;
- secret/redaction review;
- no-default + serial workspace results;
- documentation updates;
- external/persisted contract findings and compatibility handling;
- deviations, severity-tagged unresolved findings, disposition.

## 16. Handoff notes

Do not execute until M001 and M003 close and this plan is re-baselined/promoted to ready. Prefer changing only `error_class`; preserve `transport_phase`/`dispatch_phase` unless inventory proves otherwise. If richer proxy classes suggest a different retry strategy, capture that as a new evidence-driven plan instead of changing policy here.
