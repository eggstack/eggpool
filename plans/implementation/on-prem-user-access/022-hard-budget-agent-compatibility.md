# On-Prem User Access M022 — Hard-budget interoperability with coding agents

Status: implemented

Repository baseline: `88f58bbf2e9095cf46437945edf83e580270bdb6` (`main`, post-PR #7).

Source roadmap: `plans/subsystems/on-prem-user-access-roadmap.md#m022--hard-budget-agent-interoperability`

Long-term requirements: `plans/000-long-term-specification.md#2-end-state-invariants-normative`, `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`.

Applicable ADR: `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted).

Primary class: polish

## 1. Objective

Measure and, only where provably safe, improve the usability of hard per-user and instance-wide request/token/USD budgets with Eggpool's supported coding-agent clients. Do not weaken hard reservation guarantees to make a client appear compatible.

## 2. Readiness and dependencies

Hard: M017 client credential/replay integration must close first. Interface: existing M008/M009 hard/estimated admission API, canonical wire fields and provider-qualified price bounds. Operational: use synthetic clients/request captures with no personal code/prompts or production keys.

## 3. Current evidence

`rust/src/user_budget.rs` and `rust/src/instance_budget.rs` reserve worst-case hard costs; `docs/model-limits.md` documents proof-or-reject semantics. Hard token rules require an explicit caller output cap. Hard USD rules also require a conservative operator tariff for all eligible providers. Synthetic loopback request captures against a local mock server used Codex CLI 0.162.1 and OpenCode 1.18.35: Codex sent a streaming Responses request without an output cap, while OpenCode sent `max_output_tokens: 1024` from its configured model `limit.output`. The mock returned synthetic HTTP 400; only endpoint, version, stream and cap fields were retained. No prompt, request body, key, or provider call was used. The sanitized evidence is in `rust/tests/fixtures/on_prem_budget_agent_compatibility.json`. M015 corrected under-reserved hard-dollar proof, so a relaxed estimated fallback would regress security.

## 4. Invariants

Proof-or-reject semantics remain strict: no output/price bound means deny, never assign zero USD or silently downgrade hard to estimated. Preserve model policy intersection, retry and selector overhead, stream/cancellation settlement, provider account scoring and backward-compatible client wire. Private local models may use request/token budgets without USD tariffs.

## 5. Scope

In: reproduce which clients/protocols omit bounds, clearer budget failure diagnostics, optional operator-clamped output cap only if upstream and wire semantics guarantee an enforceable bound. Out: estimating output as hard proof, pricing consumer subscriptions as metered invoices, changing user quota to upstream account routing, unlimited retries.

## 6. Production changes

Create a protocol/client matrix with controlled synthetic requests and identify the accepted output-limit fields for Chat Completions, Responses, compact Responses and Anthropic Messages. Existing request admission and `BudgetProof` consume these fields without mutating the request. Do not add an operator cap: there is no protocol-wide evidence that an inferred limit would be preserved across all upstreams, wire translations and retries. Preserve rejection for clients omitting a bound and give a corrective hint.

## 7. Ordered work packages

### A — Freeze compatibility corpus

Collect bounded, sanitized synthetic request shapes for Chat, Responses, compact and Messages, including explicit/omitted/invalid output caps; distinguish user UI and agent API behavior. Confirm actual client versions and surface selection at test time.

Acceptance: reproducible proof-or-reject table by client/surface, no real request bodies.

### B — Bounded remediation where supported

For a demonstrably enforceable native upstream cap, add explicit opt-in operator limit and adapt admission proof to the guaranteed output bound. Ensure that no upstream can exceed its advertised limit through alias, transcoding, retry or selector; otherwise retain 429. Do not require custom clients to send a nonstandard header.

Acceptance: adversarial proof test for every affected surface, including malformed/ambiguous cap and fallback provider.

### C — UX and failure mapping

Return stable sanitized codes for an exhausted budget (`budget_exceeded`) and a hard proof failure (`hard_budget_not_enforceable`), with a corrective hint for explicit output caps and operator tariffs. The current transaction result intentionally does not distinguish a missing token bound from an unknown tariff or arithmetic overflow, so the message describes both safe client/operator remedies without exposing rule contents. No `Retry-After` is added: these are policy/proof decisions rather than transient waits. Preserve non-priced private-model token/request limits.

Acceptance: a member sees a stable machine code and can identify the client-request or operator-tariff correction without seeing another user's budgets or accounts.

## 8. Failure, cancellation, restart and contention

Atomic pre-dispatch holds and per-attempt retry bounds must survive stream termination, cancellation, restart and double finalization. A config reload changing model limits or provider tariff cannot let in-flight requests evade reserved proof. Do not hold the DB gate across I/O.

## 9. Compatibility and migration

Preserve existing default `estimated` rules and every currently supported hard behavior; opt-in operator bound is additional config with correct rehash/restart classification. No DB migration anticipated unless contract evidence proves necessary; if so use append-only/checksum plus restore tests.

## 10. Tests

Extend `rust/tests/on_prem_budgets.rs`, `rust/tests/on_prem_global_budgets.rs`, `rust/tests/on_prem_attribution.rs`, `rust/tests/coordinator_boundaries.rs`, `rust/tests/provider_transport.rs` and focused wire/transcoder suites. Add sanitized client contract fixtures and prove no unbounded upstream dispatch; include streaming, compact, virtual routes and server reload.

## 11. Verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

## 12. Documentation

`docs/model-limits.md`, `docs/agent-configuration.md`, `docs/api-reference.md`, `architecture/deep-dive-request-lifecycle.md`; include support matrix and preserved proof-or-reject boundary.

## 13. Acceptance criteria

Hard budgets never dispatch requests without conservative upper bounds; supported clients either work with provably bounded output or receive clear actionable errors; local-only models can use token/request quotas without fabricated USD cost.

## 14. Stop conditions

Do not start before M017. Stop if a client/provider lacks an enforceable output cap, if a solution dilutes M015 proof, or if verifying compatibility requires real user prompts. An unfixable surface is a documented limitation, not permission to relax admission.

## 15. Closure evidence

`plans/closure/on-prem-user-access/022-status.md` with client/wire matrix, worst-case bound proofs, synthetic negative traces, exact SHAs, serial/no-default CI and unresolved cases; unblock audit.

## 16. Handoff

This is a scoped interoperability and optional corrective milestone; never promise automatic output-cap injection without protocol evidence.
