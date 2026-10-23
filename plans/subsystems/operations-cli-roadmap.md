# Operations CLI Roadmap

Status: active

Long-term references:

- plans/000-long-term-specification.md — §2 end-state invariants, §3 ownership boundaries
- plans/001-terminology-and-domain-model.md — normative language for operator surfaces and observed facts
- plans/002-long-term-roadmap.md — sustaining work must preserve a working proxy and current compatibility
- plans/003-planning-process.md — evidence-gated polish and bounded implementation plans

Related ADRs:

- None required for M001–M002. Both are local correctness corrections that preserve every public CLI, HTTP, and config contract.
- Stop for architecture review if a correction requires changing the server's published status schema, the operator authorization boundary, or the process-supervision contract that `rust/src/runtime_lifecycle/` owns.

## 1. Purpose and ownership boundary

This subsystem owns the local operator command surface and the local process
lifecycle it drives:

- `rust/src/runtime.rs` — the CLI adapter that maps `rust/src/cli.rs` commands
  onto `rust/src/operations/` and prints operator-facing projections;
- `rust/src/operations/` — local lifecycle, status/metrics aggregation,
  configuration mutation, integration rendering, provenance, catalog, and
  update services;
- `rust/src/cli.rs` — the command and argument surface itself.

It consumes:

- the typed configuration and reload policy owned by `rust/src/config.rs` and
  `rust/src/config_reload_policy.rs`;
- the live runtime snapshot published by the running server generation, read
  back over the bounded private control socket;
- durable account, catalog, and identity facts owned by `rust/src/db/`.

It must not own:

- HTTP request admission, retries, or finalization (`rust/src/coordinator/`);
- wire adaptation (`rust/src/wire/`);
- provider transport (`rust/src/providers/`);
- routing and selection policy (`rust/src/routing/`, `rust/src/quota/`);
- generation publication, reload, or task supervision
  (`rust/src/runtime_lifecycle/`, `rust/src/task_supervisor.rs`);
- the installed-owner detection, installer, and release artifact contracts
  owned by the deployment and packaging subsystem.

The ownership gap this roadmap closes: no other subsystem roadmap claims
`rust/src/operations/` or `rust/src/runtime.rs` as a primary owner. The
deployment and packaging roadmap owns three named `rust/src/operations/`
modules for release purposes only; the on-prem user access roadmap scoped
`rust/src/operations/` plus `runtime.rs` to its own admin CLI workstream. As
a result, operator-facing projection and local process lifecycle defects have
no planning home, and the review index in `architecture/overview.md` has no
entry for this surface.

## 2. Work classification

### Invariants

- The CLI never mutates runtime state that the running server generation owns;
  it reports observations and issues bounded control-socket requests.
- An offline CLI fallback projection is structurally identical to the server's
  published status schema. A field added to the server schema is either
  present in the fallback or explicitly absent with a recorded reason.
- A CLI value that can be printed as a duration, count, or instant never
  silently converts a non-finite or out-of-range input into a different
  meaning.
- Local process supervision never removes a lock file whose owner state is
  unknown.
- Deadline arithmetic never panics on an unrepresentable interval.
- No credential, prompt, raw body, or cache key is printed, persisted, or
  included in an operator projection.

### Capabilities

- Operator status and runtime-status commands report the same facts with the
  same field set whether the server is reachable or not.
- `eggpool status`, `eggpool runtime-status`, and `eggpool stats` remain
  usable against an isolated home without a running server.

### Infrastructure

- The typed status projection owned by `rust/src/operations/status.rs` and the
  CLI's offline fallback projection.

### Polish

- Decomposing the largest dispatch functions in `rust/src/runtime.rs` when a
  correctness correction already touches them.

## 3. Non-goals

- No new CLI command surface in this roadmap. `eggpool users` already exists
  and is owned by the on-prem user access subsystem.
- No change to the server's published `/api/status` schema or to the CLI's
  human-readable output wording, except where a field's value was previously
  wrong or where the fallback projection was structurally incomplete.
- No change to `rust/src/cli.rs` argument parsing or to the command tree.
- No change to the update, provenance, catalog, or installer behavior owned by
  the deployment and packaging subsystem.
- No migration to a new configuration format.
- No removal or deprecation of a CLI command.

## 4. Current state

At baseline `e6822d8f78` the operator surface has two correctness defects
that are invisible to the current test and guard matrix.

The offline status fallback in `rust/src/runtime.rs` builds its payload with a
hand-written `json!` literal whose `"proxy"` object mirrors the field set of
`ProxyHealthSummary` in `rust/src/operations/status.rs` field-for-field, but
with no `Serialize` linkage. Adding a field to the typed struct silently
omits it from the fallback, and the two shapes can drift without any
compiler or test signal.

The same fallback routes through `sanitize_status_value` in
`rust/src/runtime.rs`, which returns its input unchanged. The accompanying
comment states that the server snapshot is already secret-free by
construction, which is an assertion about a different component rather than a
sanitizer.

Local process supervision in `rust/src/operations/process.rs` reads a PID
lock file and removes it when the parsed owner is absent or the owner process
is gone. A PID file that cannot be read or parsed yields no owner, which is
indistinguishable from a stale lock; the file is then removed while another
process may be about to create it, which is a time-of-check-to-time-of-use
window on the start path.

Duration conversions in `rust/src/runtime.rs` cast a clamped floating-point
seconds value directly to `u64`. A non-finite input converts to `0`, which
reads as "no delay" rather than "unknown".

Deadline arithmetic in `rust/src/operations/process.rs`,
`rust/src/operations/lifecycle.rs`, `rust/src/server/mod.rs`,
`rust/src/runtime_lifecycle/manager.rs`, and `rust/src/task_supervisor.rs`
adds a duration to `Instant::now()` without a range check, so an
unrepresentable interval panics instead of failing closed.

`rust/src/runtime.rs` is a large file with a broad dispatch surface. The
`users` dispatch function in particular is long. That shape is a known
maintenance cost, but it is not itself a defect and is not scheduled here
unless a correctness correction already touches the function.

## 5. Target architecture

- `rust/src/operations/status.rs` remains the single typed authority for the
  server's status schema. The CLI offline fallback constructs the same typed
  structs with explicit "unavailable" values and serializes them through the
  same `Serialize` implementation, so the two projections cannot drift.
- A single redaction helper owns what an operator projection may contain, and
  both the online and offline paths route through it.
- The PID lock file is only removed when the file was read successfully and
  parsed into an owner that is confirmed absent. An unreadable or unparsable
  file is reported as a held lock, not treated as stale.
- One checked-deadline helper computes `Instant` deadlines and returns a
  typed error instead of panicking. Callers convert that error into their
  existing failure category.
- Non-finite and out-of-range numeric inputs are rejected or reported, never
  coerced to zero.

## 6. Dependency graph

- M001 had no hard dependency. It was a local projection correction and is now closed.
- M002 has no hard dependency. It was a local process-lifecycle correction and is now closed.
- M002's deadline helper is consumed by `rust/src/server/mod.rs` and
  `rust/src/runtime_lifecycle/manager.rs` under an interface dependency: those
  modules do not own the helper, they consume a stable contract.
- No plan in the routing selection, request admission and wire, dashboard, or
  deployment and packaging subsystems depends on this roadmap's milestones.

## 7. Milestones

### Milestone 001 — Operator status projection fidelity

Class: invariant

Objective:

Make the CLI's offline status fallback structurally identical to the server's
published status schema by constructing the typed projection structs instead
of a hand-written JSON literal, and make the redaction boundary real instead
of a no-op passthrough.

Dependencies:

- None beyond the current stable `rust/src/operations/status.rs` types.

Deliverable boundary:

- The offline fallback builds `ProxyHealthSummary` and the runtime projection
  from `rust/src/operations/status.rs` and serializes them through the same
  `Serialize` path the server uses.
- The redaction helper performs an actual, secret-free projection and is
  covered by a test that fails if a known secret-shaped value survives.
- No change to the server's published schema, the human-readable rendering,
  or the CLI argument surface.

User or operator value:

An operator comparing `eggpool status` with `GET /api/status` sees the same
field set in both, and an offline fallback cannot silently leak a value the
online path would have redacted.

Exit conditions:

- A structural guard asserts the offline fallback and the server projection
  share one schema source.
- A redaction test covers a secret-shaped value.
- Default and no-default workspace suites pass.

Deferred work:

- Decomposing `rust/src/runtime.rs` dispatch functions.
- Human-readable output wording changes.

### Milestone 002 — Local process lifecycle safety

Class: invariant

Objective:

Remove the time-of-check-to-time-of-use window on the PID lock file, make
duration and deadline conversions reject non-finite or unrepresentable
inputs instead of coercing or panicking, and remove the arithmetic overflow
panic sites.

Dependencies:

- None. The deadline helper consumed by the server and runtime lifecycle
  modules is an interface dependency, not a hard dependency.

Deliverable boundary:

- The PID lock file is removed only when it was read and parsed into an owner
  that is confirmed absent. Unreadable and unparsable files report a held
  lock.
- One checked-deadline helper replaces the unchecked `Instant::now() +
  duration` additions.
- Non-finite seconds inputs are rejected rather than converted to zero.

User or operator value:

Two concurrent `eggpool serve` invocations cannot race through the stale-lock
removal path, and a misconfigured duration cannot crash the process or
silently become a zero delay.

Exit conditions:

- A contention test drives the stale-lock removal path against a
  simultaneously created lock and asserts a held-lock error rather than
  removal.
- A deadline test asserts a typed error for an unrepresentable interval and
  no panic.
- Default and no-default workspace suites pass.

Deferred work:

- `rust/src/runtime.rs` size reduction.
- Process-supervision redesign owned by `rust/src/runtime_lifecycle/`.

### Milestone 003 — Watchdog start-guard ownership correction

Class: invariant

Objective:

Make the watchdog start guard recoverable from every state a crashed or failed
publication can leave behind, and make the lock's removal decision report
what actually happened when the lock changes underneath the decision.

Dependencies:

- None. This is a corrective pass over Milestone 002; the lock file format,
  the PID file behaviour and the CLI contract are unchanged.

Deliverable boundary:

- A lock file that names no owner is reclaimable, while a lock whose owner
  cannot be determined is still held and left in place.
- A publisher that finds the lock path no longer names its own file reports a
  held lock instead of claiming ownership.
- A lock that vanishes during the removal decision is free, not held.
- Two lock files of the same length are distinguishable by the identity check.

User or operator value:

A watchdog or a manual start that is interrupted mid-publication no longer
leaves the server permanently unable to start.

Exit conditions:

- A contention test drives the ownerless reclaim against a publisher that
  names an owner in the meantime and asserts the lock is restored.
- A contention test drives the publication against a reclaim that moved the
  lock aside and asserts a held-lock error rather than ownership.
- Default and no-default workspace suites pass.

Deferred work:

- `rust/src/runtime.rs` size reduction.
- Process-supervision redesign owned by `rust/src/runtime_lifecycle/`.

## 8. Cross-cutting requirements

Storage/migration: none. Neither milestone introduces or alters a migration.

Protocol/compatibility: the CLI command tree, argument surface, human-readable
output wording, and the server's published status schema are unchanged.

Security/auth: the redaction boundary must not weaken the existing
server-side secret-free guarantee. No credential, prompt, raw body, or cache
key may appear in any operator projection.

Concurrency/cancellation: the PID lock fix must preserve the existing
`ProcessError::LockHeld` contract and must not introduce a new blocking wait
on the start path.

Observability: existing operator projections keep their field names and
value semantics. Only previously-absent fields may be added, and only where
the typed source already defines them.

Performance: neither milestone may add work to the server request path. The
deadline helper is called on lifecycle paths only.

Docs/ops: `docs/cli-reference.md` remains accurate for every command touched.
`architecture/overview.md` gains an entry for this surface.

## 9. Verification strategy

Focused suites:

~~~bash
cargo test --manifest-path rust/Cargo.toml --test operations_status -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_lifecycle -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_status -- --test-threads=1
~~~

M001 adds a structural schema-parity guard and a redaction regression test.
M002 adds a lock-contention test and a deadline-rejection test.

Run strict Clippy and the full serial default and no-default workspace suites
before closure.

## 10. Risks and decision points

- The offline fallback intentionally reports `"status": "unavailable"`. A
  structural guard must accept that semantic difference rather than forcing
  the fallback to claim a live server.
- The server's published schema is a compatibility surface for the embedded
  dashboard assets and for `scripts/dashboard_parity/`. Any schema change
  needs coordination with the dashboard subsystem.
- Removing the PID stale-lock path entirely would be safer but would strand
  operators after an unclean shutdown. The corrected path must keep the
  operator-recovery behavior while removing the race.
- The deadline helper crosses into `rust/src/server/mod.rs` and
  `rust/src/runtime_lifecycle/manager.rs`. If those owners require a
  different contract, stop and split the milestone rather than forcing a
  shared helper across an ownership boundary.

## 11. Completion definition

This roadmap closes when M001 and M002 are closed with correctness evidence.
No operator-facing CLI contract may regress, and the offline and online status
projections must remain schema-identical.

## 12. Milestone status

| Milestone | Status | Implementation plan | Closure record | Blockers |
|---|---|---|---|---|
| 001 — operator status projection fidelity | closed | plans/implementation/operations-cli/001-operator-status-projection-fidelity.md | plans/closure/operations-cli/001-status.md | none |
| 002 — local process lifecycle safety | closed | plans/implementation/operations-cli/002-local-process-lifecycle-safety.md | plans/closure/operations-cli/002-status.md | none; closed through M003 |
| 003 — watchdog start-guard ownership correction | closed | plans/implementation/operations-cli/003-watchdog-start-guard-ownership-correction.md | plans/closure/operations-cli/003-status.md | none |
