# C001 — Coordinator Contract and Deterministic Failure Corpus Freeze

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: invariant/infrastructure

Repository baseline: `04820555479dc3ab86622d9c658c44c45c2c07e7`

Hard dependencies: M4 T001-T006, M5 D001-D009, and M6 W001-W012 accepted closure.

## 1. Objective

Freeze the live Python inference coordinator as an externally meaningful state-transition contract before Rust begins durable dispatch. Build deterministic, secret-safe fixtures that observe request/attempt/reservation publication, wire selection, retry legality, failure effects, response handoff, streaming terminal classification, cancellation, retained cleanup, and restart reconciliation.

C001 is evidence work. It should not add provider dispatch capability to Rust.

## 2. Python oracle

Inspect and record behavior from at least:

- `request/coordinator.py`;
- `request/attempt_finalizer.py`;
- `request/claim_lifecycle.py`;
- `request/finalization_job.py`;
- `request/finalizer.py`;
- `request/provider_bound_request.py`;
- `request/response_handoff.py`;
- `request/stream_completion.py` and `stream_diagnostics.py`;
- `retry/classification.py`;
- `failure/*`, health/backoff effects, and model quarantine;
- provider contract/header/client-pool code;
- `wire/resolver.py`;
- request/attempt/reservation/routing-decision repositories and existing SQL migrations;
- public inference handlers and D007 semantic model-router selector path.

Do not treat private helper names as the contract. Observe state, outputs, persistence, and ownership.

## 3. Canonical contract artifact

Create `migration-rs/coordinator-contract.md` during implementation. It must define:

- request and attempt lifecycle states;
- durable rows/columns written at each transition;
- local M5 claim components and when each is converted/released;
- retry categories/scopes/actions and maximum attempt semantics;
- response-start/handoff point-of-no-return;
- wire resolution/negotiation transitions;
- terminal outcomes and release reasons;
- request/attempt/reservation idempotency/conflict semantics;
- cancellation behavior by phase;
- streaming EOF/terminal classification mapping;
- retained finalization ownership and retry/backoff behavior;
- restart reconciliation ownership;
- exact vs semantic normalization rules.

## 4. Failure corpus

Build deterministic Python fixtures for success and failures at every meaningful phase. Use local fake provider servers, injected database faults, fake clocks, deterministic IDs, and explicit cancellation barriers. Required cases include:

- finite success and streaming success;
- invalid/adaptation-rejected client input before claim;
- routing exhaustion;
- failure before/inside/after durable dispatch transaction;
- post-commit interruption before runtime publication completes;
- connect/proxy/TLS/write/header/read/pool failure;
- 400/401/403/404/408/409/429/5xx and provider signal variants;
- numeric/date/invalid/missing Retry-After;
- model absence vs endpoint/wire mismatch;
- fixed wire, learned preference, deterministic rejection, negotiation throttling, leader/follower, leader cancellation, follower cancellation, and rate-limited negotiation;
- finite malformed body/provider error envelope;
- empty EOF, partial EOF, malformed stream, terminal failure/incomplete, compatibility EOF, midstream exception;
- client cancellation before and after downstream start;
- finalizer database interruption and duplicate/concurrent terminal commands;
- durable terminal conflict;
- restart snapshots containing active request/attempt/reservation permutations.

## 5. Fixture projection

Persist bounded structural observations only. Include IDs as synthetic stable fixtures, state/status fields, counters, retry decisions, selected provider/account/wire identity, timestamps normalized to injected clock values, release/effect progress, terminal evidence, and safe diagnostic classes.

Never commit auth values, proxy credentials, arbitrary prompt/response bodies, session headers, or raw provider error messages.

## 6. Concurrency observations

At minimum freeze:

- two requests racing for one account claim;
- duplicate finalization callers;
- finalization racing failed-attempt cleanup;
- negotiation leader/follower cancellation;
- cancellation at the durable commit/publication boundary;
- retry selection while previous attempt cleanup is incomplete.

## 7. Supported differences

No new supported difference may be invented merely to simplify Rust. If the Python oracle exposes contradictory legacy behavior, classify the behavior and stop for an ADR/plan amendment before weakening a correctness invariant.

## 8. Dependencies

No production Cargo dependency is expected. Reuse migration fixture infrastructure. Dev-only helpers may be added if they remain local/deterministic and do not introduce a second HTTP runtime.

## 9. Verification

Run the new deterministic Python oracle repeatedly, migration suite, targeted coordinator/finalization/retry/wire tests, and secret-marker checks. The generated observation bundle must be byte-repeatable after canonical timestamp/ID injection.

## 10. Acceptance criteria

C001 closes only when:

- every M7-owned transition has an observable contract;
- the failure corpus can distinguish pre-handoff retry from post-handoff terminal behavior;
- durable and local ownership components are explicitly enumerated;
- wire negotiation and retry are not conflated;
- cancellation/restart cases are represented;
- fixtures are deterministic and secret-safe;
- C002 can implement publication without reading the 300 KB coordinator monolith for basic semantics.

## 11. Stop conditions

Stop if existing durable schema/behavior is ambiguous about ownership, if a failure fixture cannot determine whether retry occurred before handoff, or if a required observation needs live paid-provider traffic. Resolve the contract with local fixtures/ADR rather than guessing.

## 12. Closure

Create `migration-rs/closure/coordinator/001-status.md`. Only accepted C001 closure promotes C002.