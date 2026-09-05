# M7 Coordinator Contract

Status: accepted with C001 closure

Repository baseline observed: `04820555479dc3ab86622d9c658c44c45c2c07e7`

This document freezes the externally meaningful Python coordinator boundary
for the Rust migration. It is an observation contract, not a prescription to
copy `request/coordinator.py`. The committed C001 oracle is
`fixtures/coordinator/c001-python-observations.json`; its executable source is
`tests/migration_rs/coordinator_fixtures.py`.

## 1. Ownership and oracle sources

The contract is assembled from these production Python surfaces:

| Boundary | Python oracle | M7 responsibility |
|---|---|---|
| Admission and request context | `request/coordinator.py`, `request/parsed_payload.py`, request limits | Accept or reject client input before a claim; preserve request identity and stream intent |
| Local selection claim | M5 routing/claim APIs, `request/claim_lifecycle.py` | Consume or compensate the claim; do not recreate M5 scoring |
| Durable publication | `db/repositories.py`, current SQL migrations | Publish the request/attempt/reservation/trace boundary atomically |
| Provider attempt | `request/provider_bound_request.py`, provider contracts/client pool | Construct one immutable send; M4 owns transport |
| Wire selection | `wire/resolver.py`, wire registry | Resolve candidates and coordinate negotiation; never inspect HTTP bodies itself |
| Failure policy | `retry/classification.py`, `failure/classifier.py`, `failure/effects.py` | Convert typed evidence into retry, suppression, and release effects |
| Handoff and streams | `request/response_handoff.py`, `stream_completion.py`, `stream_diagnostics.py` | Enforce the point of no transparent retry and consume terminal evidence |
| Terminal cleanup | `attempt_finalizer.py`, `finalizer.py`, `finalization_job.py` | Converge durable and process-local obligations independently of the client waiter |

M4 remains authoritative for provider HTTP, connection pooling, and transport
phase errors. M5 remains authoritative for account eligibility, scoring, and
local claim acquisition. M6 remains authoritative for canonical wire
transformation, SSE framing, usage, and terminal evidence. M8 owns runtime
generation publication, rehash, shutdown orchestration, and recurring
scheduling.

## 2. Lifecycle contract

The following states are exact vocabulary for the migration boundary. A Rust
implementation may use different internal types, but its observations must
map to these states and preserve their ordering.

| State | Durable facts | Local ownership | Retry/cancellation rule |
|---|---|---|---|
| `admitted` | No request, attempt, or reservation row | Parsed request and bounded admission state | Invalid/adaptation-rejected input ends here; no account effect |
| `locally_claimed` | No durable inference rows yet | M5 pending request/token load, active count, optional quota reservation and health probe | Cancellation releases only components acquired by this request |
| `durable_attempt_published` | Pending `requests`, incomplete `request_attempts`, active `reservations`, and frozen routing decision | The publication receipt identifies every converted component | A failed transaction compensates provisional state; no provider send is legal before this point |
| `wire_selected` | No additional required durable row | Ordered static/learned wire candidates and optional negotiation handle | Wire selection is not failure classification and performs no I/O |
| `dispatching` | Attempt identity is stable | M4 send ownership | Typed transport failure may retry before handoff; local preparation failure does not penalize a provider |
| `upstream_headers_received` | Attempt remains incomplete until terminal handling | Response/stream close ownership | Status/body evidence is classified; no client response start has happened yet |
| `downstream_started` | Attempt remains owned by the same request | Downstream response-start fact is monotonic | Transparent retry is forbidden once response start is sent or attempted, including an empty started stream |
| `streaming` | Attempt remains incomplete | Upstream response, decoder, observer, timers, and downstream iterator | Client cancellation and midstream failure are terminal for this attempt; EOF needs native terminal evidence |
| `terminal_command_registered` | Durable identity is retained by a finalization command | Retained job/command owns asynchronous cleanup | Client task cancellation cannot discard the command |
| `durable_terminal` | Attempt and, for a request terminal, request status are terminal; reservation is converged or has an explicit retry state | Runtime components may still need release | Duplicate terminal commands observe convergence; incompatible terminal facts fail closed |
| `runtime_released` | Durable state remains terminal | Pending/active/quota/probe components are released exactly once | Analytics is non-authoritative |
| `completed` | All correctness obligations converged | No retained correctness ownership remains | Bounded diagnostic history may remain scalar-only |

Retryable attempts follow `attempt_terminal -> locally_claimed` for a new
attempt, but the previous attempt must be durable-terminal and its reservation
must be converged before replacement ownership is accepted. The shared upstream
submission budget is `1 + max_retries_before_stream`. There is no retry after
handoff.

## 3. Durable publication and row contract

The existing schema is the only schema. C001 adds no migration. The selected
columns below are the durable compatibility boundary; nullable historical
columns and unrelated analytics columns remain outside the coordinator
identity contract.

### `requests`

The parent row carries `id`, `account_id`, `model_id`, `provider_id`,
`protocol`, `streamed`, `proxy_request_id`, `status`, `first_attempt_at`,
`last_attempt_id`, `completed_at`, `status_code`, `error_class`,
`retry_count`, `input_tokens`, `output_tokens`, and `cost_microdollars`.
Creation is pending and occurs only after a concrete selected account exists.
Terminal mutation is conditional on the row still being pending. An
intermediate retry must not finalize the parent or claim the winning-attempt
backlink.

### `request_attempts`

Each provider submission has its own `id`, `request_id`, `attempt_number`,
`account_id`, `provider_id`, `model_id`, `protocol`, and `streamed` facts.
Terminal update records `status_code`, safe `error_class`,
`upstream_request_id`, `bytes_emitted`, `bytes_received`, `latency_ms`,
`retry_category`, `release_reason`, `is_retry_outcome`, and `completed_at`.
`finalize_if_incomplete` is the idempotency boundary: only an incomplete row
may transition.

### `reservations`

An active row links `request_id`, `account_id`, `model_id`, estimated tokens,
and reserved microdollars. Release sets terminal status, released time, and a
bounded release reason. Repeated release is a no-op observation, not a second
quota decrement. Expiry/reconciliation cannot erase a pending request's
active reservation without the explicit recovery rule that owns that state.

### `routing_decisions`

The frozen M5 decision is written with the attempt: request/attempt number,
model/provider/protocol, selected account identity, selected tier/score,
eligible/scored/excluded counts, score context, and bounded exclusion JSON.
The trace and attempt cannot disagree because they share the publication
transaction. It is diagnostic evidence of the selection, not a second routing
authority.

## 4. Local ownership receipt

`RuntimePublicationReceipt` is the explicit conversion record. Its required
facts are:

- pending request load acquired and pending token load acquired;
- exactly one of pending load converted or pending load released;
- active request count acquired;
- quota reservation acquired;
- health/circuit probe acquired and, when needed, released;
- durable request, attempt, and reservation identities after publication.

Before commit, compensation is synchronous and database-free. After commit,
post-commit interruption retains the immutable
`FinalizationIdentity`/`ClaimCompensationSubmission` and converges durable and
local components through a retained command. No component may be inferred
from a later mutable request context.

## 5. Failure, retry, and effect contract

`FailureEffects` is the one policy result carried from classification through
cleanup. It includes retry boolean, retry scope/action, client outcome,
account effect, model effect, circuit penalty, durable backoff, probe
convergence, evidence class, wire effect, and parsed retry-after value.

The deterministic corpus freezes these rules:

| Evidence | Retry action | Shared-state effect |
|---|---|---|
| Client validation or local preparation | none | Client/local error; no provider penalty |
| Transport connect/proxy/TLS/write/read/pool failure before handoff | another account, same wire | Account failure/backoff and circuit penalty |
| Bare/ambiguous 401 | none | Do not disable credentials or advance health |
| Explicit invalid/expired/revoked credential evidence | another account, same wire | Disable only the selected account credential |
| Deterministic wire auth/surface/schema rejection with an alternate and response status | alternate wire, same account | Reject only the candidate in the bounded wire cache |
| Strong model absence | another account, same wire | Model-scoped quarantine/withdrawal; never wire enumeration |
| 429/rate pressure | bounded retry policy | Rate-limit delay; stop negotiation discovery without candidate suppression |
| 408/5xx provider failure before handoff | another account, same wire | Provider/account effects as classified; model quarantine when the current classifier says so |
| Any failure after downstream start | none | Terminal stream/request outcome; no transparent replay |

`Retry-After` accepts numeric seconds and HTTP-date forms. Invalid or missing
values remain absent; any later policy default is bounded and is not persisted
as provider prose. HTTP status, typed signal, dispatch phase, provider-scoped
model presence, alternate availability, and downstream-start fact are all
required context for interpreting ambiguous responses.

Malformed finite bodies and local adaptation failures are typed terminal
observations. Raw provider bodies, arbitrary error messages, credentials, and
session headers are discarded after bounded signal extraction.

## 6. Wire negotiation contract

Wire candidate resolution is a process-local bounded operation keyed by
provider, model, and structural candidate fingerprint. Candidate ordering may
come from operator-fixed preference, metadata/bundled hint, learned success,
or deterministic configured order. Learned success is recorded only after
ordinary completed success. A deterministic rejection records a bounded
cooldown for one candidate; it does not mutate account health or model state.

Negotiation has one provider/model flight:

- the leader owns the provider gate and candidate-dispatch admission;
- followers await the shared result and submit no discovery candidate;
- follower cancellation does not cancel the leader or release its permit;
- leader cancellation releases only the permit it acquired and resolves the
  flight with a bounded rejection result;
- rate limiting ends discovery and delays future negotiation, capped at the
  configured ceiling;
- flight, gate, learned, rejection, metric, and provider state are bounded;
- the resolver never performs provider I/O, database writes, or raw-body
  inspection.

## 7. Finite and streaming terminal contract

Finite success must adapt before the request can become durably completed.
Native provider errors and malformed success bodies remain typed evidence.

Streaming completion requires native terminal evidence. The accepted terminal
vocabulary is:

| Native evidence | C001 observation |
|---|---|
| OpenAI `[DONE]` | `openai_done` / `complete` |
| Anthropic `message_stop` | `anthropic_message_stop` / `complete` |
| Responses `response.completed` | `responses_completed` / `complete` |
| Responses `response.failed` | `responses_failed` / `terminal_failure` |
| Responses `response.incomplete` | `responses_incomplete` / `terminal_incomplete` |
| Gemini completion (`interaction.completed` or `finishReason=STOP`) | `gemini_completed` / `complete` |
| Gemini non-success finish/status | `gemini_incomplete` / `terminal_incomplete` |

Empty EOF, partial EOF, malformed frames, and transport midstream exceptions
are not successful completion. A configured compatibility policy may classify
EOF after usage completion as `compatibility_eof`; strict mode remains
`premature_eof` or `malformed_eof`. The stream decoder is incremental and
bounded; it does not invent a terminal event at transport EOF.

## 8. Cancellation, finalization, and restart

Cancellation is phase-aware:

1. before claim acquisition: no durable or provider effect;
2. while waiting for claim/publication/negotiation: release only owned local
   coordination state and retain any committed identity;
3. during connect/write/header wait: classify the attempt at its actual
   transport phase and preserve terminal identity;
4. before downstream response start: retry is possible only when policy and
   budget permit;
5. after response start, during stream body, or after downstream disconnect:
   the attempt is terminal and cannot be replayed;
6. during finalization: the retained finalization command continues until
   durable and runtime obligations converge or enter bounded retry state.

The immutable `FinalizationIdentity` contains proxy request ID, database
request ID, attempt ID, reservation ID, account ID/name, provider/model IDs,
client/upstream protocols, and attempt number. The retained command tracks
durable transition, reservation convergence, runtime release, failure/effect
progress, retry count, and a scalar diagnostic record. Duplicate compatible
commands share the retained work. Incompatible terminal outcomes raise a
terminal conflict and do not overwrite durable truth.

Restart reconciliation explicitly examines pending requests, incomplete
attempts, active reservations, terminal attempts with unreleased reservations,
replayed terminal commands, and requests with multiple attempts. M7 exposes a
bounded one-shot reconciliation/drain interface. M8 later owns when that
interface is scheduled and which generation retains it.

## 9. Parity and security rules

Exact facts are state/status vocabulary, identity relationships, retry action
and category, response-start monotonicity, terminal evidence kind, release and
effect component names, and idempotent/conflict outcomes. Semantic differences
are injected clock representation, synthetic SQLite rowids, exception/body
wording, and scheduler/task identity. No normalization may erase model,
wire, tool, reasoning, media, terminal, retry, or ownership meaning.

Default observations contain only synthetic stable IDs, status/category labels,
counts, bounded durations, and safe diagnostic classes. They never contain
API keys, proxy credentials, authorization values, raw provider bodies,
prompts/responses, session identities, process IDs, or host paths.

## 10. C001 evidence and next boundary

The C001 oracle covers 23 failure/effect cases, 11 stream terminal/EOF cases,
the durable row projection, the runtime publication receipt and finalization
progress fields, wire learning/rejection and leader/follower roles, and a
local HTTP provider stub. Its full runtime bundle is repeatable under injected
clock values; the committed JSON is a reviewed scalar projection.

C002 may now implement durable dispatch publication and lifecycle identity
against this contract. C001 does not authorize provider dispatch, Rust
coordinator capability, M8 runtime lifecycle work, or live provider traffic.
