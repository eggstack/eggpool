# Plan 127 — Durable In-Flight Lifecycle Necessity Decision

Date: 2026-08-14
Status: ready
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
