# Plan 127 — Durable In-Flight Lifecycle Necessity Decision

Date: 2026-08-14
Status: complete
Parent roadmap: `plans/122-post-audit-correctness-and-sbc-simplification-roadmap.md`
Planning baseline: `c17bb84af6d737a8408cbcce4d2746caedee36e8`
Depends on: Plan 126 evidence when available
Priority: P1 architecture/product proportionality
Execution target: GPT-5.6 Luna or comparable implementation model

## Purpose

Make an explicit product/architecture decision about EggPool's durable in-flight
request lifecycle before any further database/finalization simplification.

Today EggPool creates durable request, attempt, and reservation identities around
the dispatch boundary, converges terminal state through a generation-owned
finalization supervisor, fails closed on ambiguous database outcomes, and repairs
leftover pending state at process startup. This provides strong accounting and
crash-convergence guarantees, but it also drives substantial request-path code,
SQLite write traffic, finalization machinery, and test surface.

For a private local/LAN Raspberry Pi/SBC proxy, a simpler product contract may be
acceptable: process death terminates all in-flight client requests, and only
completed usage/accounting plus durable provider/account state must survive.

This plan does **not** implement either outcome. It determines whether that
simpler contract is actually acceptable based on current product behavior,
operators, accounting requirements, and code/write-path evidence.

The final output is binary:

```text
decision: retain
```

or

```text
decision: simplify
```

Plan 128 is blocked unless this plan records `decision: simplify`.

## Governing constraints

1. No production code changes to durability/finalization architecture in this
   plan.
2. Do not begin deleting request/attempt/reservation persistence while auditing.
3. Do not introduce a configuration switch between "durable" and "ephemeral"
   modes.
4. Do not treat lower SQLite write count as sufficient justification by itself.
5. Preserve the intended local/LAN appliance scope; do not invent hypothetical
   multi-process/distributed requirements.
6. Distinguish durable **completed accounting/history** from durable **in-flight
   ownership**. The decision concerns the latter.
7. Distinguish process crash semantics from ordinary provider/client failures.
   Normal failures must remain correctly finalized within a running process.
8. Do not weaken provider failure isolation, retry rules, health suppression,
   authentication/redaction, rehash generation safety, or SQLite integrity.
9. Use Plan 126 measurements only as contextual evidence, not as the sole basis
   for an architecture decision.
10. Record concrete source/database invariants. Avoid generic "production best
    practice" arguments.

## Workstream A — Define the current durable guarantee precisely

Trace the current request lifecycle from accepted client body through terminal
completion. Identify the exact point each durable identity is created and why:

- `requests` row;
- `request_attempts` row;
- `reservations` row;
- routing decision row when enabled;
- account backoff/suppression persistence;
- usage/price/accounting rows;
- finalization job/lease/terminal command ownership;
- runtime active-count/reservation publication;
- startup crash recovery;
- database ambiguity fail-closed path.

Produce a concise invariant map in this plan's closure, for example:

```text
accepted request before upstream dispatch
 -> durable request + attempt + reservation transaction
 -> runtime publication
 -> upstream dispatch
 -> downstream handoff
 -> terminal finalization transaction
 -> runtime release
```

Do not commit a separate architecture report.

## Workstream B — Separate product-essential durability from implementation
history

For each persisted/in-memory element classify its value as one of:

1. required for routing correctness while the process is alive;
2. required for completed usage/cost/history after successful terminalization;
3. required specifically to recover **in-flight** state after process death;
4. diagnostic/operator convenience;
5. legacy/implementation coupling with no independent product requirement.

Answer specifically:

- What user-visible failure occurs after process crash today?
- What would be lost if all in-flight requests were simply considered failed at
  process death without durable pre-dispatch rows?
- Are provider API charges for a request that completed upstream after local
  process death expected to be perfectly accounted locally?
- Is exact pending reservation recovery required to prevent future routing
  starvation, or can in-memory reservations disappear with the process?
- Which account/provider suppression states genuinely need persistence across
  restart?
- Which dashboard/history views require a row for a request that never completed?
- Does any external tool/API consume durable pending request/attempt identities?
- Are there multi-process writers/readers? The current product should be treated
  as a single-worker local proxy unless source/docs say otherwise.

Search README/docs/architecture/CLI/API/dashboard/tests for explicit product
promises rather than inferring them from implementation complexity.

## Workstream C — Quantify request-path cost qualitatively and where possible

Without creating a benchmark, inventory for one normal successful request:

- mandatory pre-dispatch SQLite statements/transaction boundaries;
- post-response/finalization SQLite statements/transaction boundaries;
- request/attempt/reservation object construction;
- finalization job submission and supervisor ownership;
- compensation/reconciliation paths exercised only because durable pre-dispatch
  ownership exists;
- background/startup recovery code required only for in-flight durability;
- migrations/tables/indexes/tests tied primarily to the invariant.

Use Plan 126 WAL/write observations if available.

Do not create an arbitrary LOC or write-count threshold. The purpose is to know
what complexity would actually disappear if the invariant changes.

## Workstream D — Enumerate failure scenarios under both contracts

Compare current `retain` semantics with candidate `simplify` semantics for:

### Ordinary successful non-stream request

Both must preserve completed usage/accounting and response correctness.

### Ordinary successful stream

Both must retain active runtime ownership until stream terminalization/client
cancellation while the process is alive.

### Upstream failure before handoff

Both must preserve retry/failure-isolation behavior and terminal client response.

### Failure after downstream handoff

Both must preserve no-retry semantics and best-effort/required terminal
accounting while process remains alive.

### Client cancellation

Both must release in-memory routing/reservation pressure and record completed
accounting/terminal state according to the chosen persistent contract.

### Process crash before upstream dispatch

Candidate simplify semantics may lose the in-flight request entirely; on restart
there should be no stale in-memory ownership to repair.

### Process crash after upstream accepted request but before local terminalization

This is the hardest tradeoff. Determine whether local usage/accounting may be
missing/partial under simplify semantics and whether that is acceptable for the
product.

### SQLite commit/rollback ambiguity

Even if in-flight rows are simplified, completed/account/backoff/config DB writes
may still require fail-closed handling. Do not assume the entire DB ambiguity
architecture disappears.

### Rehash during active stream

Generation lease/retirement safety may remain necessary independent of durable
request rows. Do not conflate them.

## Workstream E — Decision criteria

### Select `retain` if any of these are true

- exact recovery/accounting for in-flight requests across process death is an
  explicit product requirement;
- external APIs/operators depend on durable pending attempt identities;
- losing an accepted upstream request's accounting on local crash is considered
  unacceptable;
- removing durable pre-dispatch state would make routing/account limits
  incorrect after restart in a way that cannot naturally reset with process
  memory;
- the actual code/write cost attributable solely to this invariant is small
  enough that deletion risk outweighs benefit;
- simplification would require a parallel compatibility mode or broad schema
  rewrite to preserve supported users.

### Select `simplify` only if all of these are true

- process death is allowed to fail all in-flight client work;
- missing/partial local accounting for the rare crash window is acceptable and
  clearly documented;
- no external consumer requires pending request/attempt/reservation durability;
- routing pressure/reservations can safely be process-local and naturally reset
  on restart;
- completed usage/accounting/history can still be durably recorded at terminal
  completion without pre-dispatch ownership;
- provider failure/backoff state that should persist can remain independently
  durable;
- rehash/generation/stream ownership can remain correct without durable
  pre-dispatch identities;
- a deletion-oriented implementation can materially reduce code/write/test
  surface without adding modes or replacement frameworks.

When evidence is mixed, choose `retain`. Simplification requires affirmative
proof that the weaker crash contract is acceptable.

## Workstream F — Record decision and consequences

Append a closure section with:

```text
decision: retain|simplify
```

and concise evidence:

- current invariant summary;
- explicit product promises/consumers found;
- crash-window accounting consequence;
- request-path writes/complexity attributable to the invariant;
- Plan 126 relevant observations if available;
- what remains durable under either outcome;
- Plan 128 disposition.

### If decision is retain

- mark Plan 128 `not applicable`;
- add a short architecture/AGENTS clarification only if current rationale is
  unclear;
- do not create another optimization plan aimed at the same durability machinery
  absent new evidence.

### If decision is simplify

- mark Plan 128 unblocked;
- state the exact new process-death contract Plan 128 must implement;
- list protected behavior that remains mandatory;
- do not make partial production edits in this plan.

## Verification

This is primarily a source/product audit. Required verification is evidence
quality, not tests.

At minimum inspect:

- request coordinator selection/persistence/finalization paths;
- DB repositories/schema/migrations involved;
- finalization supervisor/job code;
- runtime routing reservation/active-count logic;
- crash recovery;
- rehash generation leases;
- dashboard/stats/CLI consumers;
- architecture/README/AGENTS product claims;
- protected tests that encode the current invariant;
- Plan 126 evidence when available.

No ordinary gate is required if this plan changes only its own planning record
and narrow documentation. If docs/AGENTS are edited, no CI expansion is needed.

## Explicit acceptance criteria

- [ ] Current durable request/attempt/reservation lifecycle is traced end to end.
- [ ] Each relevant persistent/in-memory element is classified by actual product
  value rather than implementation history.
- [ ] External/API/dashboard consumers of pending identities are searched for and
  recorded.
- [ ] Normal success/failure/stream/cancellation/process-crash/database-ambiguity/
  rehash scenarios are compared under retain versus simplify contracts.
- [ ] Request-path SQLite/write/finalization complexity attributable to durable
  in-flight ownership is identified without arbitrary thresholds.
- [ ] Plan 126 observations are incorporated if available and clearly labeled as
  contextual.
- [ ] The closure records exactly one `decision: retain` or `decision: simplify`.
- [ ] `simplify` is selected only if all affirmative criteria are met; ambiguous
  evidence defaults to retain.
- [ ] No durability production code, DB schema, runtime mode, dependency, or CI
  change occurs in this plan.
- [ ] Plan 128 is explicitly marked unblocked or not applicable based on the
  decision.
- [ ] Decision evidence is appended to this plan; no separate decision/closure
  plan is created.

## Rejection conditions

Reject execution if it:

- edits production durability before the decision is recorded;
- assumes local deployment means accounting correctness never matters;
- assumes "production-grade" behavior is automatically over-engineering without
  tracing its consumers;
- chooses simplify solely from LOC/write count;
- creates durable/ephemeral configuration modes;
- conflates generation lease correctness with database durability;
- proposes replacing SQLite instead of evaluating the specific invariant;
- uses speculative multi-process/distributed requirements not present in the
  product.

## Handoff sequence

1. Read Roadmap 122, this plan, Plan 126 closure if complete, `AGENTS.md`, and
   current request/database/finalization architecture docs.
2. Trace current durable identities and their consumers before forming a view.
3. Separate completed-history durability from in-flight crash durability.
4. Compare required failure scenarios under retain/simplify contracts.
5. Apply the strict decision criteria; default to retain when evidence is mixed.
6. Append the binary decision and supporting evidence to this file.
7. Mark Plan 128 unblocked or not applicable.
8. Stop. Do not implement durability changes here or create another decision
   plan.

## Closure — 2026-08-15

Audit baseline: `612f68d42dec09564a61a343a1f44c381c371d0c`.

decision: retain

The evidence is affirmative for retaining durable in-flight ownership. The
decision is based on the current product contract and concrete consumers, not
on a generic expectation that every service must recover requests after a
crash. The audit found no need for a second durability mode, schema rewrite,
or distributed/multi-process requirement.

### Current invariant map

The current request path is:

```text
accepted and parsed client request
 -> in-memory routing plan and provisional request/token claim
 -> one DB transaction creates the pending request, active reservation,
    and incomplete attempt identities
 -> commit
 -> second claim-lock phase converts provisional runtime load to canonical
    active-count/quota ownership and retains the health probe lease
 -> optional routing trace submission (diagnostic only)
 -> upstream dispatch and downstream handoff
 -> generation-owned terminal command/finalization job
 -> one correctness transaction terminalizes request, attempt, and reservation
 -> usage/account-runtime/health convergence and exactly-once runtime release
```

The first-attempt transaction is implemented by
`RequestCoordinator._persist_dispatch_bundle()` in
`src/eggpool/request/coordinator.py:1777-1849`, and its caller owns the
transaction at `src/eggpool/request/coordinator.py:2613-2647`. A retry keeps
the parent request identity and adds a distinct attempt/reservation pair. A
routing trace, when enabled and accepted by its guard, is an asynchronous
observability write after the durable lifecycle bundle; trace-off and skipped
paths do not participate in correctness.

`AttemptRuntimeLease` separately owns active-request count, quota reservation,
and health-probe obligations (`src/eggpool/request/finalization_job.py:221-248`).
The retained finalization path persists terminal facts and then converges
those runtime obligations (`src/eggpool/request/finalizer.py:869-935`). This
separation means generation leases and live-stream ownership remain correct
even though the durable lifecycle decision is retained.

At process start, `_crash_recovery()` transitions every prior pending request
to `interrupted`, releases every prior active reservation with
`release_reason = 'crash_recovery'`, terminalizes incomplete attempts as
`process_interrupted`, and records recovery events in one transaction
(`src/eggpool/app.py:210-288`). Database commit/rollback ambiguity remains a
fail-closed worker boundary; startup integrity, migrations, and recovery are
not interchangeable with ordinary provider failure handling.

### Product and consumer audit

The following explicit promises and consumers were found:

| Evidence | Finding | Decision impact |
|---|---|---|
| `README.md:33-38` | Publicly promises restart-safe crash recovery for durable requests/reservations and fail-closed restart handling. | The weaker simplify contract would be a product change, not an internal optimization. |
| `docs/runbooks/database-recovery.md:18-22` | Operator contract says pending work from the previous process is repaired from durable request/attempt/reservation identities and is not retried across restart. | Operators are instructed to rely on this behavior. |
| `docs/deployment.md:816-830` | Runbook directs operators to inspect pending requests and active reservations, explains finalization failures, and says startup crash reconciliation repairs work left by an exited process. | Pending identities are an operator-facing lifecycle diagnostic. |
| `src/eggpool/api/stats.py:440-449,793-794` and `src/eggpool/stats/service.py:1751-1813` | Authenticated pending-health API reports pending count/age, stale pending count, and active reservation pressure. | The state is externally observable through a supported local API. |
| `src/eggpool/cli_full.py:4511-4520` and `src/eggpool/dashboard/render.py:1464-1525` | `runtime-status` and the dashboard render pending requests, active reservations, and crash-recovery activity. | Operators can diagnose leaked or recovered in-flight work without direct DB access. |
| `src/eggpool/stats/queries.py:1595-1668` and `src/eggpool/api/stats.py:294-315` | Authenticated request traces expose the parent request and complete attempt chain. | Retaining attempt identities preserves supported retry/failure history. |
| `tests/unit/test_db.py:154-229`, `tests/integration/test_phase12_end_to_end.py:1090-1128`, `tests/integration/test_phase13_end_to_end.py:1682-1715` | Tests assert restart transition, reservation release, attempt interruption, and recovery events. | The behavior is a maintained capability contract, not dead implementation detail. |
| `AGENTS.md:142,166` and `architecture/README.md:10-18` | Product is explicitly one-worker/local, with process-local routing pressure and startup repair of durable leftovers. | No speculative distributed requirement was used; retention is justified within the intended appliance scope. |

No third-party integration was found that requires a pending numeric identity,
and no multi-process writer/reader contract was found. That absence removes a
possible additional reason to retain, but does not override the explicit
restart/recovery and operator-facing product promises above.

### Classification of lifecycle elements

| Element | Actual value | In-flight crash value |
|---|---|---|
| `requests` row/status | Completed usage, latency, error, cost, cache, bandwidth, and request history; parent for attempt traces. | Preserves an accepted request as `interrupted` after process death instead of silently erasing it. |
| `request_attempts` rows | Retry exclusion/history, provider/account outcome, status, bytes, latency, and request trace. | Identifies attempts that did not reach local terminalization so startup can close them. |
| `reservations` rows | Durable reservation lifecycle and accounting audit; active rows mirror quota pressure. | Releases exact active ownership after restart and prevents stale pressure from starving routing. |
| Runtime claim receipt / `AttemptRuntimeLease` | Required for live routing correctness, health probe ownership, stream lifetime, and exactly-once release while the process is alive. | Naturally disappears on process death, but its durable counterpart is needed to reconcile DB state with that disappearance. |
| Routing decisions | Optional sampled diagnostic/operator history; never authoritative for routing or accounting. | None; it is not a retention driver and remains optional. |
| Account backoff/suppression rows | Provider-authoritative health state that should survive restart independently of request ownership. | Not request recovery; retain independently. |
| Finalization supervisor/jobs | Required while alive for bounded terminal work, retry/compensation convergence, stream generation ownership, and exactly-once terminal obligations. | Its durable identities provide the startup repair anchor; its in-memory job state itself is not restart durable. |
| Operational/account recovery events | Bounded operator/audit diagnostics for what startup repaired. | Makes restart disposition visible; not a substitute for lifecycle rows. |

The audit found no relevant element whose only value is historical
implementation coupling. Some finalizer progress and stale-sweep machinery is
implementation detail, but it is coupled to the retained terminal ownership
and durable convergence invariant and therefore cannot be classified as
removable under this decision.

### Request-path cost attributable to the invariant

For a normal first attempt, the mandatory durable pre-dispatch work is one
caller-owned SQLite transaction containing three inserts: one pending
`requests` row, one active `reservations` row, and one incomplete
`request_attempts` row. This is the source-inventory result also recorded by
Plan 126, not a benchmark or a write-count threshold. A retry reuses the
parent request and adds one attempt/reservation pair. With routing traces off,
there is no routing-decision write on the normal path; enabled traces are
asynchronous and diagnostic.

The request also constructs a selected-attempt identity, a runtime publication
receipt, and an `AttemptRuntimeLease`. Terminal work is submitted to the
generation-owned finalization supervisor, whose progress covers request,
attempt, reservation, runtime release, usage, health, and bounded retry state.
The durable pre-dispatch invariant additionally requires post-commit claim
compensation, failed-attempt cleanup before retry, expired-reservation
reconciliation, startup crash recovery, and the associated indexes, recovery
events, dashboard/API queries, and regression tests. These are meaningful
complexity and SQLite activity, but they are the cost of an explicitly
supported lifecycle contract rather than a sufficient reason by themselves to
delete it.

Plan 126 is contextual only: its Raspberry Pi-class run had no safe configured
provider account, so provider-backed request, stream, cross-protocol, and
post-request WAL dimensions were not measured. It observed an unchanged
16,512-byte WAL across idle and local invalid-model checks and recorded the
same three-row pre-dispatch source inventory. No flash-endurance, throughput,
or workstation-to-SBC inference is made.

### Failure-contract comparison

| Scenario | Retain contract | Simplify candidate | Required conclusion |
|---|---|---|---|
| Successful non-stream | Durable bundle exists before dispatch; terminal usage/history and runtime release converge. | Could persist only at terminal completion while alive. | Both preserve response and completed accounting while alive; retain additionally preserves accepted-work identity. |
| Successful stream | Pending rows and generation/runtime lease remain until canonical stream completion or cancellation; no retry after handoff. | In-memory ownership could protect the live stream. | Generation lease and handoff rules are independent and remain mandatory either way. |
| Upstream failure before handoff | Failed attempt and reservation converge before distinct-account retry; provider effects remain scoped. | In-memory attempt pressure could theoretically preserve this live behavior. | No simplification is authorized to weaken retry exclusion, cleanup ordering, or health isolation. |
| Failure after handoff | No retry; retained terminal owner converges bounded durable accounting and runtime state. | Same live-process behavior is possible. | Post-handoff no-retry and terminal accounting remain mandatory. |
| Client cancellation | Retained terminal command releases runtime ownership once and records terminal lifecycle facts. | Process-local release plus terminal history could work while alive. | Live cancellation correctness does not prove crash simplification is acceptable. |
| Crash before upstream dispatch | Startup marks request/attempt interrupted and releases reservation. | Accepted work would be forgotten. | Forgetting violates the documented restart-repair contract. |
| Crash after upstream acceptance before local terminalization | Startup closes the known local lifecycle and releases ownership; an upstream charge that never reached EggPool before death may still lack exact provider usage, but the local request is represented as interrupted. | The request, attempt, reservation, and any local terminal facts would disappear; any provider charge would be wholly unrepresented locally. | The retained contract materially reduces silent accounting/history loss in the hardest window. It does not claim impossible recovery of an unobserved provider response. |
| SQLite commit/rollback ambiguity | Fail closed; durable identities and startup integrity/recovery remain the repair boundary. | Surviving completed/backoff/config writes would still require the same fail-closed treatment. | Simplifying this invariant would not remove SQLite ambiguity architecture generally. |
| Rehash during active stream | Generation lease and generation-owned finalizer keep the old generation alive until terminal convergence. | Still required independently. | Do not conflate generation safety with durable request persistence. |

### Decision and dispositions

All `simplify` affirmative criteria are therefore not met: the weaker
process-death contract is not compatible with the explicit restart-safe
operator/product promise, and the repository already treats pending lifecycle
state as a supported API/dashboard/runbook capability. The evidence is not
ambiguous enough to justify weakening that contract merely to reduce request
path writes and implementation surface.

Plan 128 is **not applicable**. Its status has been updated to
`not applicable — Plan 127 retained durable in-flight ownership`; no
production, schema, runtime-mode, dependency, or CI changes from Plan 128 are
authorized.

Plan 129 is already `ready` and is now unblocked because Plans 123–127 have
final dispositions and Plan 128 has an explicit not-applicable disposition.
Plan 130 is already `ready` and was unblocked by Plan 123 independently. No
other future plan status required changing.

### Acceptance record

- [x] Current durable request/attempt/reservation lifecycle is traced end to end.
- [x] Persistent and in-memory elements are classified by product value and crash value.
- [x] External/API/dashboard/operator consumers of pending identities were searched and recorded.
- [x] Normal success, stream, failure, cancellation, crash, ambiguity, and rehash scenarios are compared under both contracts.
- [x] Request-path SQLite/finalization complexity attributable to in-flight ownership is identified without an arbitrary threshold.
- [x] Plan 126 observations are incorporated as contextual evidence and unavailable dimensions remain explicit.
- [x] The closure records exactly one operative decision: `decision: retain`.
- [x] Simplification was rejected because its affirmative criteria are not all met.
- [x] No durability production code, DB schema, runtime mode, dependency, or CI change occurred.
- [x] Plan 128 is explicitly marked not applicable.
- [x] Decision evidence is appended here; no separate decision/closure plan was created.
