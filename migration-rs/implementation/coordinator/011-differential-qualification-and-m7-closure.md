# C011 — Differential Qualification and M7 Closure

Status: planned; blocked on C010 accepted closure

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: invariant

Hard dependency: accepted C001-C010 closure.

## Objective

Run the integrated Python/Rust coordinator qualification and close M7 only if the real Rust inference endpoints, retry/failover, wire negotiation, streaming/cancellation, durable finalization, and restart reconciliation satisfy the frozen C001 contract under adversarial failure modes.

C011 should primarily add qualification/evidence. Fix bounded defects discovered by the matrix in the implementation commit; if a material new architecture decision is required, stop and create a corrective plan/ADR rather than weakening the oracle.

## Integrated matrix

At minimum cover all three public client surfaces against deterministic providers using native and cross-wire profiles, finite and streaming requests, direct and proxied M4 account clients, single/multiple accounts, fixed and negotiable wire profiles, and virtual-router semantic selection where applicable.

For each case compare externally meaningful semantics:

- client status/headers/body or ordered SSE frames;
- exact number/order of provider attempts;
- selected provider/account/wire sequence;
- retry category/scope/action and exhaustion;
- response-start point and whether later retry was forbidden;
- request/attempt/reservation/routing-decision durable rows;
- M5 health/backoff/quarantine/quota/circuit effects;
- wire resolver preference/rejection/flight state;
- usage/cost/request-ID/timing class and release reason;
- retained finalization convergence;
- restart reconciliation result.

## Mandatory failure cases

Include: malformed client input, selection exhaustion, DB publication fault, post-commit interruption, pool/connect/TLS/proxy/write/header/read failures, auth/quota/rate-limit/model absence/server errors, Retry-After variants, alternate-wire deterministic rejection, negotiation leader/follower cancellation, finite malformed/provider error, header/first-byte/idle timeout, empty/partial/malformed stream EOF, terminal failure/incomplete, upstream midstream exception, client disconnect before/after handoff, downstream write failure, finalizer DB fault, runtime release fault, terminal conflict, supervisor capacity, simulated crash/restart, and subsequent valid request recovery.

## Concurrency and leak pass

Run bounded concurrent requests through shared account/wire/finalizer state. After each batch and after cancellation storms, assert:

- active request count returns to baseline;
- pending/reserved quota load converges;
- circuit probe ownership returns;
- provider connection limits remain usable;
- wire negotiation gates/flights return to baseline;
- retained terminal jobs drain or remain only as bounded observable retry work;
- DB has no unexplained active reservations/attempts;
- next valid request succeeds without restart.

## Exactness rules

Do not normalize away attempt order, selected account/wire, retry count, response-start timing class, terminal outcome, release reason, durable status, effect class, or terminal evidence. Incidental timestamps/UUIDs may be normalized only through injected fixture identities/clocks. JSON object key order may be semantic where already allowed by the public surface; arrays/SSE frames remain ordered.

## Security/dependency/resource audit

Confirm no auth/proxy/session/request-body/provider-body secret appears in logs, Debug, persisted error detail, fixture snapshots, or response headers. Confirm no second HTTP/TLS stack, ORM, actor framework, task queue, or full-stream buffer was introduced. Review task/queue/cache capacities for SBC/local deployment scope.

## Verification

Run Rust format/clippy/all-target tests; M7 focused qualification; migration oracle; targeted Python coordinator/retry/finalizer/wire/stream tests; smoke/API tests; SQLite Python↔Rust compatibility; `git diff --check`. Live paid-provider traffic is not required for M7 closure.

## Closure criteria

M7 closes only if:

1. all C001 mandatory corpus rows pass or have an approved supported difference;
2. no transparent retry occurs after downstream handoff;
3. every failed attempt is terminal/cleanup-owned before replacement ownership;
4. terminal commands converge under duplicate/cancel/fault/restart cases;
5. no local/durable resource leak remains after bounded recovery;
6. public finite/stream endpoints match Python semantically;
7. wire learning/rejection and retry scopes are correct and bounded;
8. restart reconciliation is idempotent and never replays unknown in-flight work;
9. no unresolved high/medium M7 correctness/security finding remains;
10. M8 receives explicit stable interfaces for supervisor ownership, reconciliation invocation, generation teardown/drain, and request leases.

## Closure record

Create `migration-rs/closure/coordinator/011-status.md` with requirement-to-evidence matrix, commands/results, failure corpus, concurrency/leak evidence, dependency/security review, supported differences, and exact M8 handoff.

Accepted C011 closure marks M7 closed and makes M8 eligible for its own planning review. It does not automatically promote an M8 implementation plan.