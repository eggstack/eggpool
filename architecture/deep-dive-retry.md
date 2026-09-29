# Deep Dive: Retry and Failure Classification

Back to [Architecture](README.md). See also the review index in
[overview.md](overview.md).

`rust/src/coordinator/failure.rs` owns classification and retry legality for both
the finite (`coordinator/finite.rs`) and streaming (`coordinator/streaming/coordinator.rs`)
paths. `rust/src/health/` applies the narrowest matching quarantine, backoff, and
breaker effect, while the coordinator that owns the attempt loop owns every retry and
failover decision. `server/*` stays thin: each inference handler makes exactly one
coordinator call per request and holds no retry or finalization logic.

## Classification inputs and outputs

Every attempt failure is first shaped into a `FailureObservation` and then mapped once by
`classify()` into `FailureEffects`. `FailureObservation::response` builds the provider-response
case from an `http::StatusCode`; `FailureObservation::local` builds the local-preparation case
with `status` left as `None`, so health and quarantine can never record a phantom upstream
failure for work that never reached a provider. `.signal()` attaches one normalized hint, and
only the four genuine wire signals (`wire_auth_mismatch`, `wire_surface_unsupported`,
`wire_schema_mismatch`, `model_unsupported_on_surface`, recognized by `is_wire_rejection_signal`)
force the `WireRejected` category — rate-limit, quota, credential, and model-absence hints keep
their own backoff and quarantine path instead of hijacking wire handling.

`FailureSource` covers transport, provider response, client, client validation, local
preparation, database, and cancellation origins. `FailureCategory` covers bad request,
authentication, quota, rate limit, temporary, transient transport, model unavailable,
wire-rejected, cancelled, and fatal outcomes. `RetryScope` (`None`/`Account`/`Wire`/`Wait`) says
where a retry may go; `NextAction` (`Complete`/`RetryAccount`/`RetryWire`/`WaitRateLimit`/`Exhaust`)
says what the loop does next. The string labels (`retry_action`, `retry_scope_label`,
`evidence_class`, `client_outcome`, `account_effect`, `model_effect`, `circuit_effect`,
`wire_effect`) are the stable contract consumed by later coordinator slices; the enums drive
control flow.

Status mapping stays explicit. 401 without credential evidence is ambiguous (`http_401_ambiguous`)
rather than forced auth-disable, while credential-invalid text disables with `explicit_credential_invalid`
evidence; 402 and quota-evidenced 403/409/422 map to quota; 408 maps to a timeout path; 429/425 and
rate-evidenced 409/422 map to rate limit with `parse_retry_after` (delay-seconds or RFC-1123 dates)
clamped to `max_retry_after`; 5xx maps to temporary with a bounded backoff; transport failures map to
transient transport with a 30 s backoff. Model-absent 404 quarantines, or terminally withdraws only on
authoritative catalog presence (`ProviderModelPresence::AbsentAuthoritative`).

## One shared bounded budget

`RetryPolicy` bounds the single shared upstream-submission budget: `max_attempts` (default 3) gates
both `RetryAccount` and `RetryWire` through one `attempt_number < max_attempts` check, and
`max_retry_after` (default 1,800 s) clamps every `Retry-After` before storage. Each coordinator
holds its own `FailureDecisionEngine`, which pairs the policy with an `EffectLedger` (default
capacity 256, `EffectLedgerError::Capacity` when full): `decide()` classifies once per attempt and
reports whether the caller owns the first effect application, so retried finalization observes the
same decision without applying account or model effects twice (`try_apply_once`/`apply_once`,
`retire` on release).

`prepare_next` is the only retry-state mutation: a wire-scoped decision pins `preferred_account` to the
same account so the next iteration tries the next untried `WireSurface`, while an account-scoped decision
excludes the failed account (`excluded_accounts`) and clears the pin. `should_retry` admits only
`RetryAccount` and `RetryWire`; any other action completes instead of redialing upstream.

## Finite retry loop

`FiniteCoordinator::execute` loops one attempt at a time: `select_and_claim` (or
`select_and_claim_for_account` when a wire retry pinned the account) draws one `SelectionClaim`;
`WireResolver::resolve` orders that provider's wire candidates; the per-account `attempted_wires` set
skips already-tried surfaces; `PublicationService::publish` durably records the attempt; the borrowed
`AttemptPreparation` is prepared synchronously into a fully owned `PreparedUpstreamAttempt` before
`submit_once` is awaited, so no generation or request borrow crosses provider I/O.

Every failure funnel — `submit_once` transport errors, bounded body reads (`max_provider_body_bytes`;
`TransportError::ResponseBodyTooLarge` is `Fatal`, never retried), wire-preparation failures
(`wire_prep_failure` maps overflow to 413 and adaptation rejection to 502), unadaptable bodies, and
provider error statuses — builds an observation (`observation()`), runs `decide()`, applies effects once
(`apply_effects()` into `router.apply_failure_effects`), and either converges the failed attempt
(`cleanup_failed_attempt` registers `FinalizationCommand::FailedAttempt` and awaits the handle) plus
`prepare_next()` and `continue`, or returns a terminal `FiniteExecution` via `pending_terminal()`. Wire
rejection additionally calls `WireResolver::reject` (`reject_candidate`); success calls `accept` and
`router.record_success`.

Exhaustion prefers evidence over synthesis: when no eligible account remains but a real upstream response
was retained (`LastUpstream`), the coordinator passes that status through with the body truncated to
`MAX_CLIENT_ERROR_BYTES` (512) instead of inventing an envelope, marking upstream-fault ceilings with an
`attempt_ceiling_reached` header. Cancellation cannot release capacity it did not own: the local sources
(`Client`, `ClientValidation`, `LocalPreparation`, `Database`, `Cancellation`) classify as `BadRequest` or
`Cancelled` with `RetryScope::None`, and `ResponseHandoffState` monotonicity keeps the finite completion
path (`FiniteExecution::mark_started`/`complete`) from ever re-entering the loop after a write failure.

## Streaming handoff boundary

`StreamingCoordinator` retries only before the downstream handoff. Its pre-handoff loop covers routing,
publication, dispatch, upstream header wait (bounded by `StreamTimeoutPolicy::header_timeout`, drawn from
the provider `read_timeout_s`), status decode, first-byte prefetch (bounded by `first_byte_timeout`), and
retry or alternate-wire decisions — all while `response_started` and `downstream_started` are false. Its
`observation()` threads the live `downstream_started` flag into both observation fields, and `classify()`
forces `RetryScope::None` / `NextAction::Complete` whenever either is set. Returning `StreamingExecution`
closes the retry window structurally: no second dispatch path exists after handoff.

`streaming/execution.rs` owns the single post-handoff body and cancellation lifecycle: `StreamingExecution`
carries filtered `headers`, an optional pre-handoff `error_body`, `AttemptStreamFacts`, and the retained
`PendingStreamFinalization`. The caller marks `mark_started` immediately before sending response start,
pulls chunks until clean terminal or a typed `StreamChunkError` (`IdleTimeout`, `EmptyEof`, `PrematureEof`,
`MalformedEof`, `UpstreamTransport`, `Translation`), then awaits `complete()`. Every chunk error is terminal
for that client request — provider body released, retained finalization stored, upstream never redialed —
and dropping the execution without completing schedules an interrupted or cancelled retained command so a
converted claim cannot strand. `ActiveStream` holds only scalar progress plus the current chunk; provider
bytes are pushed through and encoded per chunk, never accumulated.

`streaming/terminal.rs` consumes the wire `StreamTerminalSummary` without reparsing events. `classify_eof`
maps each `StreamTerminalOutcome` (with `TerminalEvidence` and the `saw_usage_completion` compat allowance)
into one `StreamEofClass` (`Complete`, `Compatibility`, `TerminalFailure`, `TerminalIncomplete`, `EmptyEof`,
`PrematureEof`, `MalformedEof`); `completion_compat_allowed` admits only `compatible` and
`permissive_observe` policies; `provider_error_signal` re-derives the narrow credential, wire, model,
rate, and quota hints. Bounded scalar helpers (`bounded_request_id`, `bounded_i64`, `bounded_usize`,
`duration_i64`, `cache_status`, `category_label`) keep every retained fact secret-free, `StreamTimeoutPolicy`
has no whole-stream deadline (`max_lifetime_s` parsed but never enforced), and `StreamDiagnostics` records
only `KNOWN_OUTCOMES` counters plus one scalar last-event.

## Narrowest health effect and durability

`apply_effects` forwards only narrow facts — account-penalty flag, model-quarantine flag, model-effect label,
backoff reason with its relative `backoff_until` duration, circuit penalty — and `rust/src/health/` applies
the narrowest matching consequence: pair quarantine versus account breaker (`HealthEffectApplier`), bounded
reason-specific backoff (`compute_backoff_seconds` over `BackoffReason`, capped by
`MAX_NONTERMINAL_BACKOFF_SECONDS`), exact-key model quarantine (`ModelQuarantine`: `Suspected` to
`Quarantined` promotion, `TerminalWithdrawn` only from authoritative `EvidenceProvenance`). The coordinator
never reinterprets those outcomes as fresh retry evidence, and release-probe-only outcomes
(`release_probe_only`) free capacity without recording a failure.

Publication runs in a spawned worker so caller cancellation cannot strand a claim mid-commit (a dropped
receiver triggers `compensate_lost_delivery`); each retry attempt publishes its own durable attempt and
reservation rows and requires the prior attempt to have converged (`PriorAttemptNotFinalized` otherwise).
Failed attempts converge through `DurableFinalizer::finalize_failed_attempt` (request stays `pending`); only
the terminal path uses `finalize_request`. `request::admission` stays out of all of this — stateless policy
plus the preservation envelope only (one bounded parse, `store`/`previous_response_id`/conversation/background
rejection, token and context estimates, routing facts) — and the compact operation reuses the same ownership
via the private single-owner `execute_compact_admitted`, without rebuilding the public `FiniteRequest` view.

## Invariants

- `classify()` is total and single-application: one `FailureEffects` per observation, applied once per `attempt_id` via the `EffectLedger`.
- One shared budget: `RetryAccount` and `RetryWire` both require `attempt_number < max_attempts` (default 3); wire retry additionally requires a wire-signal failure with an untried alternate surface.
- No retry after downstream start: `response_started || downstream_started` forces `RetryScope::None` / `NextAction::Complete`, and returning `StreamingExecution` or `FiniteExecution` closes the loop structurally.
- Wire rejection never hijacks quota, auth, or model-absence handling: only `is_wire_rejection_signal` hints map to `WireRejected` with `reject_candidate`.
- Local-only failures carry no synthetic upstream status and never retry; cancellation cannot release capacity it did not own.
- Every failed attempt converges durably (`FailedAttempt`) before the next publication; terminal ownership is retained, never inferred.
- Post-handoff stream failures are terminal for the client request and classify from `StreamTerminalSummary` without reparsing wire events.
