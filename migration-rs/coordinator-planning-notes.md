# M7 Coordinator Planning Notes

Status: planning evidence; implementation plans are authoritative for handoff

Repository baseline: `04820555479dc3ab86622d9c658c44c45c2c07e7`

## Python surface reviewed

The current Python coordinator is a large orchestration module (`src/eggpool/request/coordinator.py`, ~298 KB) but its correctness boundaries are already represented by smaller modules. M7 planning therefore follows those behavioral seams instead of preserving the monolithic structure.

Key sources:

- `request/coordinator.py` imports request/attempt/reservation/routing-decision repositories, M5 router/quota/health/effects state, M4 `ProviderClientPool`, provider header/auth contracts, response handoff, stream diagnostics/completion, retry classification, wire resolver/negotiation, and finalization machinery.
- `request/attempt_finalizer.py` terminalizes one failed attempt and conditionally releases its active reservation in one transaction; duplicate completion re-observes terminal state instead of double release.
- `request/claim_lifecycle.py` compensates pending load, active count, quota reservation, durable attempt/reservation, and circuit probe as independent progress components.
- `request/finalization_job.py` explicitly states that `asyncio.shield()` is not ownership: retained finalization must be independently referenced, registered before cancellation-sensitive awaits, resumable, bounded, and generation-ownable.
- `request/finalizer.py` owns idempotent overall request terminalization, usage/cost persistence, failure effects, and durable convergence of request/attempt/reservation rows.
- `request/response_handoff.py` is a monotonic response-start fact.
- `request/stream_completion.py` distinguishes complete, empty/premature/malformed/compatibility EOF, terminal failure/incomplete, and Gemini incomplete outcomes; EOF is not universal success.
- `retry/classification.py` adapts canonical failure effects into retry categories and parses numeric/HTTP-date Retry-After.
- `wire/resolver.py` owns bounded learned preference, deterministic candidate rejection, provider/model negotiation flights/gates, cancellation-safe leader/follower behavior, rate-limit delay, and candidate fingerprints; it performs no network I/O or status/body interpretation.
- `request/provider_bound_request.py` shows the important source-intent invariant: retries regenerate a provider-bound generation from the original client/canonical request rather than translating a previous attempt's provider body.
- `request/internal_dispatch.py` prepares concrete non-streaming selector requests below the public HTTP handler, supporting a typed internal semantic-router path rather than localhost HTTP recursion.

## Planning conclusions

1. The Rust coordinator should be a state machine plus narrow services, not a single large object.
2. Durable publication must be separated from provider submission so pre/post-commit compensation is testable.
3. Wire resolver state is M7, while static codec/profile semantics remain M6.
4. Retry legality belongs in one decision engine and is gated by downstream handoff.
5. Failed-attempt finalization and overall request finalization are separate operations.
6. Retained async terminal ownership is required; `Drop` cannot perform durable cleanup.
7. M7 implements a bounded supervisor interface, while M8 owns generation lifetime, rehash, signals, and recurring scheduling.
8. Public inference routes should be wired only after the lifecycle is qualified, keeping handlers policy-free.
9. Restart reconciliation must fail closed and never replay work whose provider-side completion is unknowable after process death.
10. Deterministic local provider/database/cancellation fixtures are sufficient for M7; live paid-provider qualification belongs later.

## Dependency posture

No new architecture dependency is expected. Existing Tokio/Axum, M4 Hyper/Rustls/Eggress, M5 domain state, M6 wire runtime, and F004 SQLite layer cover the required mechanisms. The plans explicitly reject an ORM, actor framework, workflow/task-queue system, second HTTP client/TLS stack, internal localhost RPC loop, or generic streaming framework.