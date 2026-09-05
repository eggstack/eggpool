# C008 — Streaming Handoff, Timeouts, Cancellation, and Terminal Policy

Status: planned; blocked on C007 accepted closure

Source roadmap: `migration-rs/subsystems/coordinator-roadmap.md`

Primary class: capability/invariant

Hard dependency: C007.

## Objective

Port the streaming lifecycle around M4 transport and M6 incremental stream runtime: response-header/first-byte/idle timeout policy, downstream handoff, chunk forwarding/adaptation, cancellation, terminal evidence, incomplete/malformed EOF, provider midstream errors, usage completion, and retained finalization.

## Python oracle

Use C001 plus coordinator streaming branches, `response_handoff.py`, `stream_completion.py`, `stream_diagnostics.py`, `proxy/sse_observer.py`, timeout configuration, C005 retry policy, C006 retained finalization, and M6 W008/W011/W012 evidence.

## Phase model

Distinguish at least:

1. waiting for upstream headers;
2. upstream headers accepted but downstream not started;
3. waiting for first provider body byte;
4. downstream response started;
5. streaming body in progress;
6. terminal provider evidence observed;
7. EOF/failure/cancellation;
8. retained finalization.

Header/first-byte failures before downstream handoff may be retryable only through C005. Idle timeout/midstream/provider terminal failure after handoff is terminal for this client request and must never transparently replay.

## Timeout ownership

M4 retains transport-level connect/write/read primitives; M7 owns response-header, first-byte, and stream-idle policy using injected clocks/timers. Do not add a whole-stream deadline that kills legitimately long active streams unless the frozen Python contract requires it.

Timeout cancellation must drop/close the active M4 response/body cleanly and then transfer terminal ownership to C006 before the client task can disappear.

## M6 integration

Feed provider bytes incrementally into one M6 selected-profile stream runtime and encode canonical events to the original client surface. Do not buffer the complete stream. Preserve ordering/tool IDs/reasoning/usage/terminal evidence already closed by M6.

Use M6 terminal evidence to classify complete, compatibility-complete, terminal failure/incomplete, malformed EOF, empty EOF, and partial premature EOF. EOF is never automatically success.

## Downstream cancellation

Detect client disconnect/write cancellation at all phases. Before handoff, C005 may choose retry only if the cancellation is not client-originated and policy authorizes it; client-originated cancellation terminates the request. After handoff, record `client_cancelled`/midstream outcome and finalize without retry.

## Resource bounds

No per-event tasks, no complete stream buffer, no unbounded partial tool/reasoning state, and no accumulating raw provider chunks. Byte counters/usage/terminal diagnostics are scalar/bounded. Release provider response resources promptly.

## Tests

Use deterministic chunk/timer barriers for header timeout, first-byte timeout, active stream with no idle timeout, idle timeout, terminal success, compatibility EOF, empty EOF, partial EOF, malformed SSE/UTF-8, Responses failed/incomplete, Gemini incomplete, upstream midstream exception, client disconnect before/after start, downstream write error, cancellation during finalization handoff, and all five upstream profiles to three client surfaces.

Assert attempt counts, handoff monotonicity, no post-handoff retry, bounded buffer state, transport closure, and eventual C006 convergence.

## Dependencies

Tokio timers/streams and existing M4/M6 types only. Do not add a generic streaming framework.

## Acceptance criteria

C008 closes when streaming is incremental and bounded, timeout/cancellation classifications match Python, terminal evidence cannot become false success, no post-handoff replay exists, and every exit transfers durable/runtime cleanup to C006.

## Closure

Create `migration-rs/closure/coordinator/008-status.md`. Accepted closure promotes C009.