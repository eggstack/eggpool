# Plan 079 — Quarantine Durability and Generation Publication

Date: 2026-08-05
Status: ready for implementation
Parent roadmap: `plans/077-sbc-lifecycle-simplification-and-runtime-correctness-roadmap.md`
Planning baseline: `cd8967799e6613f3a5965af8cd15ce3c5269aaa8`

## Purpose

Make model-quarantine state publication fail closed and restart truthful. A generation must not silently publish an empty quarantine when durable state could not be read, and model reappearance must not clear in-memory suppression before the durable row has converged.

This plan is intentionally narrow. It does not redesign the quarantine state machine, add a new durable queue, expand corroboration rules, or change provider failure classification.

## Confirmed defects

### Hydration failure publishes empty state

`RuntimeGenerationFactory.prepare()` currently catches broad exceptions while reading `ModelQuarantineRepository.list_all()`, logs the failure, and continues with a newly constructed empty `ModelQuarantine`.

Consequences:

- startup can temporarily route through models that durable state still marks quarantined;
- rehash can replace a healthy active generation with a candidate missing quarantine state;
- schema/row conversion defects are misrepresented as “no quarantine rows”;
- the runtime violates fail-closed publication semantics used elsewhere.

### Reappearance clear is ordered in memory first

The authoritative model-reappearance callback currently clears the in-memory state before attempting durable clearing. A durable failure is logged and suppressed.

Consequences:

- the current process routes the model as healthy while the database remains quarantined;
- restart can rehydrate the stale row and unexpectedly suppress the model again;
- operator-visible state differs across restart despite no new provider evidence.

## Governing decisions

1. Durable hydration is a generation-publication prerequisite.
2. “No rows” and “could not read rows” are distinct outcomes.
3. Startup failure keeps readiness closed and lets the normal process supervisor handle restart/operator repair.
4. Rehash candidate failure leaves the active generation unchanged.
5. Durable clear commits before in-memory clear.
6. A durable clear failure leaves the current in-memory quarantine intact.
7. No generic cache-coherency framework or event-sourcing layer is added.
8. Existing bounded quarantine identities, TTLs, corroboration, and provider-scoped semantics remain unchanged.
9. Local database failures never become provider health evidence.
10. Verification remains focused and deterministic.

## Workstream A — Make hydration explicit and typed

### Repository contract

Audit `ModelQuarantineRepository.list_all()` and row conversion.

Required contract:

- return a complete list of durable rows on success;
- return an empty list only when the query succeeded and no rows exist;
- propagate database, schema, and conversion failures with a typed local exception;
- never suppress malformed rows individually unless the repository already has a documented safe compatibility rule.

If old rows can be invalid because of a prior released schema, add one bounded migration/normalization in the existing schema path. Do not skip arbitrary rows at runtime.

### Generation factory behavior

During `RuntimeGenerationFactory.prepare()`:

1. create the in-memory quarantine object;
2. read all durable rows;
3. convert and hydrate every row;
4. fail candidate preparation if any required step fails;
5. allow the existing candidate abort path to close newly created resources;
6. do not mutate the active generation or process-owned state before successful preparation.

Remove the broad “log and start empty” fallback.

Use a bounded operator-facing error class/message that identifies quarantine hydration as the stage without including row contents or secrets.

### Startup behavior

Confirm startup ordering:

- migrations complete before quarantine hydration;
- readiness is not admitted before initial generation installation;
- hydration failure terminates/aborts startup through the existing fatal startup path;
- no background task later replaces the missing state silently.

Do not add a hydration retry loop. A supervised restart is sufficient for transient startup failures.

### Rehash behavior

Confirm failed candidate preparation:

- returns the existing structured rehash failure;
- does not call generation commit/publication;
- does not retire the active generation;
- closes candidate-owned clients/resources through the existing abort contract;
- leaves active quarantine and routing behavior unchanged.

No special quarantine-specific rollback framework is needed.

## Workstream B — Order authoritative clears durably first

### Required clear sequence

For each authoritative model reappearance identity:

1. resolve the exact durable quarantine key;
2. execute the durable clear/terminal update inside the existing database transaction boundary;
3. confirm the repository result represents durable convergence or idempotent already-cleared state;
4. only then clear the matching in-memory quarantine entry;
5. clear matching transient account/model backoff after durable quarantine convergence;
6. emit bounded diagnostics.

If the durable clear fails:

- do not clear in-memory quarantine;
- do not clear matching transient backoff as if recovery succeeded;
- propagate or return a typed local failure to the catalog refresh caller;
- do not mark the provider/account unhealthy;
- preserve the prior catalog/quarantine state according to existing non-destructive refresh semantics.

### Batch behavior

If one catalog refresh reports several model reappearances, process each identity deterministically. Prefer one caller-owned transaction for the batch if the repository architecture already supports it and the operation remains small. Otherwise use the existing per-row transaction pattern.

Do not partially publish in-memory clears before the durable batch has committed.

If an all-or-nothing durable batch would require a broad repository rewrite, use per-identity durable-first publication and return a structured partial local failure. Document the exact behavior.

## Workstream C — Preserve authoritative identity semantics

Audit the key used for hydration and clear. It must consistently include the existing authoritative dimensions, such as:

- provider ID;
- account identity;
- canonical model ID;
- upstream model ID;
- upstream protocol.

Do not widen an exact model clear into account-wide or provider-wide recovery.

Do not use display aliases or unsuffixed client model names when a durable canonical identity is available.

Confirm that successful provider traffic and authoritative catalog reappearance retain their distinct recovery semantics.

## Workstream D — Diagnostics and operator behavior

Update runtime/rehash diagnostics to distinguish:

- quarantine hydration succeeded with zero rows;
- quarantine hydration succeeded with N rows;
- quarantine hydration failed and candidate was rejected;
- durable reappearance clear failed;
- durable and in-memory clear converged.

Keep diagnostics scalar and bounded. Do not expose row payloads, provider credentials, or traceback text.

Update current architecture documentation and `AGENTS.md` to state:

- quarantine hydration is fail closed;
- rehash never publishes a candidate without complete quarantine state;
- authoritative clear is durable-first;
- durable failure preserves in-memory suppression.

## Focused verification

Extend existing generation/quarantine/catalog tests.

Required cases:

1. successful zero-row hydration publishes an empty quarantine;
2. successful non-empty hydration reproduces exact durable entries;
3. repository read failure aborts initial generation preparation;
4. repository row-conversion failure aborts preparation rather than skipping the row;
5. failed rehash candidate leaves the active generation and quarantine object unchanged;
6. candidate resources close after hydration failure;
7. durable reappearance clear succeeds before in-memory clear;
8. already-cleared durable state permits idempotent in-memory convergence;
9. durable clear failure leaves in-memory quarantine intact;
10. durable clear failure does not clear matching backoff;
11. exact model/account/protocol identity is preserved;
12. local database errors do not create provider penalties.

Use injected repository failures or an in-memory SQLite database. Do not use live providers or timing sleeps.

Suggested commands:

```bash
uv run ruff format src/eggpool/generation_factory.py src/eggpool/failure src/eggpool/db tests/unit tests/integration
uv run ruff check src/eggpool/generation_factory.py src/eggpool/failure src/eggpool/db tests/unit tests/integration
uv run pyright src/eggpool/generation_factory.py src/eggpool/failure src/eggpool/db
uv run pytest <affected quarantine/generation/catalog tests> -q --tb=short --maxfail=1
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

## Acceptance criteria

- [ ] Quarantine hydration failure aborts candidate preparation.
- [ ] Successful zero-row hydration remains distinguishable from failure.
- [ ] Startup never admits readiness with unknown quarantine state.
- [ ] Failed rehash hydration leaves the active generation untouched.
- [ ] Candidate resources are closed through the existing abort contract.
- [ ] Authoritative reappearance clears durable quarantine before in-memory quarantine.
- [ ] Durable clear failure preserves in-memory quarantine and matching backoff.
- [ ] Exact provider/account/model/protocol scope is preserved.
- [ ] Diagnostics are bounded and secret-free.
- [ ] Focused tests and the existing smoke gate pass.
- [ ] No hydration retry service, durable queue, or generalized cache framework is introduced.

## Rejection conditions

Do not close this plan if:

- any broad exception path still logs and publishes an empty quarantine;
- malformed durable rows are silently skipped without an explicit released compatibility rule;
- a failed candidate can mutate active quarantine state;
- in-memory clear precedes durable convergence;
- local database failure clears suppression or penalizes a provider;
- verification depends on live provider behavior.

## Implementation sequence for GPT-5.6 Luna

1. Read the quarantine repository, state machine, factory, catalog callback, rehash transaction, and current tests.
2. Make the repository hydration contract explicit.
3. Remove fail-open factory fallback and add startup/rehash tests.
4. Reorder reappearance clear to durable-first.
5. Add exact identity and failure-order tests.
6. Update bounded diagnostics and current architecture docs.
7. Run focused checks, then smoke.
8. Mark complete only with exact command results recorded.