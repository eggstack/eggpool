# Shared Provider Profile Contract Milestone 002 — OpenCode Go Wire Hints for the Current Endpoint Table

Status: ready
Repository baseline: `origin/main@e48f021e` (2026-10-22; inspect latest at handoff).

Source roadmap:

- `plans/subsystems/shared-provider-profile-contract-roadmap.md#7-milestones`
- Predecessor: `plans/implementation/shared-provider-profile-contract/001-neutral-provider-profile-contract-and-opencode-wire-hints.md`
- Predecessor closure: `plans/closure/shared-provider-profile-contract/001-status.md` (implementation `9ac6a131`)

Long-term requirements:

- `plans/000-long-term-specification.md` — deterministic provider configuration and protocol correctness.
- `plans/001-terminology-and-domain-model.md` — provider identity, wire surface, model catalog, and configuration authority.
- `plans/002-long-term-roadmap.md` — reusable Rust substrate and provider correctness.
- `plans/003-planning-process.md` — bounded infrastructure extraction and evidence-gated handoff.

Applicable ADRs:

- None required. M002 adds reviewed model-to-surface metadata to the existing shared contract. It does not publish a registry, move credential/account authority into the crate, add a wire surface, change a codec, or move runtime behavior across a process boundary. If implementation would add a fourth wire surface to answer `gemini-4-argon-contributor`, that is a contract change to `eggpool-wire`'s surface vocabulary: stop and write an ADR instead.

Primary class: infrastructure.

## 1. Objective

Bring the shared OpenCode Go model-wire map back into agreement with the
**current** first-party endpoint table, so a sibling consumer can resolve a
model the provider actually serves, and make the drift between the reviewed
table and the live catalog **visible and guarded** instead of silently
accumulating.

The concrete deliverable is narrow: the shared wire map currently has no entry
for `beam-free` or `minimax-m3.1-flash-free`, so a consumer that selects either
route fails closed at `wire_unresolved` even though the provider serves both
and the first-party documentation places both.

The durable deliverable is the guard: the reviewed table is a hand-maintained
transcription of a page that changes, and the current failure mode is that it
silently stops matching the thing it transcribes.

## 2. Why this milestone is ready

M001 closed and its implementation is an ancestor of this baseline. The shared
crate, the canonical asset, the wire-surface vocabulary and the reviewed-table
fixture all exist and are consumed by EggPool and by a sibling consumer
fixture. The interface M002 changes — the `model_wire` table shape — is
unchanged from M001; only its **contents** and the review record around it
change.

There is no hard dependency. The evidence M002 needs is the first-party
documentation page and the public model catalog, both of which are read-only
and unauthenticated.

## 3. Current implementation evidence

All observations below were taken on 2026-10-22 against
`https://opencode.ai/docs/go` and `GET https://opencode.ai/zen/go/v1/models`.

### 3.1 The first-party endpoint table has moved since the M001 review

M001's fixture transcribes the page's "Endpoints" section, reviewed 2026-10-07.
The page has since changed. The current table has **29** rows:

| Surface family | Models (current first-party table) |
|---|---|
| `openai_chat_completions` (18) | `beam-free`, `deepseek-v4-pro`, `deepseek-v4.1-flash`, `glm-5.2`, `glm-5.3`, `glm-5.3-flash`, `hy3`, `hy4-preview`, `kimi-k2.7-code`, `kimi-k3`, `longcat-2.0`, `longcat-2.5-preview-free`, `mimo-v2.5`, `mimo-v2.5-pro`, `mimo-v2.6-flash`, `mimo-v2.6-pro`, `minimax-m3.1-flash-free`, `step-5-preview` |
| `openai_responses` (5) | `gpt-5.6-luna`, `gpt-6-luna`, `grok-4.7`, `muse-spark-1.2-contributor`, `muse-spark-1.3-contributor` |
| `anthropic_messages` (5) | `claude-haiku-5-5`, `minimax-m3`, `qwen3.7-plus`, `qwen3.8-flash`, `qwen3.8-max` |
| Google Generative AI (1) | `gemini-4-argon-contributor` — a surface family the shared contract does not model |

### 3.2 The canonical wire map has 30 entries and is missing four placed models

`rust/crates/eggpool-provider-profile/assets/_provider_profiles.toml` carries
30 `[providers.opencode-go.model_wire."<id>"]` entries. Comparing it with the
current table:

- **Placed by the first-party table but absent from the wire map (4):**
  `beam-free` (chat completions), `minimax-m3.1-flash-free` (chat completions),
  `step-5-preview` (chat completions), `claude-haiku-5-5` (Anthropic messages).
- **Present in the wire map but no longer placed by the current table (6):**
  `deepseek-v4-flash`, `deepseek-v4-flash-vision-exp`, `space-bunny` (also
  absent from the live catalog), and `grok-4.6`, `kimi-k2.6`, `minimax-m2.7`
  (still present in the live catalog).
- **`gemini-4-argon-contributor` is placed on an unmodelled surface** — see
  §3.5.

### 3.3 The live catalog and the free routes

`GET https://opencode.ai/zen/go/v1/models` answers `200` without a credential
and returns **36** ordinary OpenAI-shaped model rows.

- `beam-free` is in the catalog.
- `minimax-m3.1-flash-free` is in the catalog.
- `longcat-2.5-preview-free` is **not** in the catalog, though the first-party
  table still lists it. Its upstream answers HTTP 402 to a request that carries
  a credential.

The three facts together are the whole story of the operator's two free routes:
both are **available** and **placed**, and neither is **hinted**. The consumer
therefore fails them closed at `wire_unresolved` — a finding about this
repository's shared profile, recorded on the CodeGG side at
`plans/closure/harness-lab/evidence/route-audit-20261022.json`, not a defect in
the consumer.

### 3.4 The catalog contains five models the first-party table does not place

`glm-5.1`, `minimax-m2.5`, `omen-alpha`, `qwen3.6-plus` and `qwen3.7-max` are
in the live catalog and are **not** placed by the current endpoint table. They
must stay unresolved. A catalog entry proves a model exists; it does not say
which endpoint family serves it.

### 3.5 One placed model needs a surface family the contract does not model

`gemini-4-argon-contributor` is placed by the first-party table on
`https://opencode.ai/inference/go/google/v1beta/models/gemini-4-argon-contributor`
— a Google Generative AI surface. `eggpool-provider-profile` models exactly
three surfaces, and `eggpool-wire` owns their codecs. There is no correct way
to represent this model in the current contract, and mapping it onto an
existing surface would be a **wrong** hint: the consumer would dispatch a
Google-native model to an OpenAI-compatible endpoint.

### 3.6 The reviewed-table fixture asserts equality in both directions

`rust/crates/eggpool-provider-profile/tests/opencode_go.rs` holds
`REVIEWED_TABLE` and `reviewed_table_is_covered_exhaustively_by_non_fixed_hints`
asserts that the declared hints and the reviewed table are the **same set**.
That guard is why the drift was survivable: any addition to one side without
the other breaks the test — but nothing detects that the table itself has
stale, which is the actual failure.

### 3.7 Endpoint URL families

The current first-party table publishes per-family URLs under
`https://opencode.ai/inference/go/{openai,anthropic,google}/v1/...`, while the
profile's `base_url` remains `https://opencode.ai/zen/go/v1` with per-surface
path templates. The `/zen/go/v1` family is still live: the catalog GET above
answers through it, and a direct `POST` of `beam-free` to
`https://opencode.ai/zen/go/v1/chat/completions` with a credential and an
`x-opencode-session` header answered `200` on 2026-10-22.

The **model-to-surface-family** mapping is what M002 changes. The endpoint URL
metadata is a separate fact and M002 does not change it — unless a live probe
at implementation time shows the `/zen/go/v1` family no longer serves Go
models, which would be a separate finding, not a silent edit.

## 4. Invariants that must not regress

- The shared crate stays secret-free and sans-I/O: no API-key values,
  credential stores, environment reads, filesystem reads at runtime, HTTP,
  async runtime, database, logging, clocks, or random state.
- EggPool remains the runtime authority for accounts, credentials, catalog
  refresh, routing, quota, health/quarantine, retries, transport, persistence,
  and wire negotiation.
- `eggpool-wire` remains the authority for canonical request/response/event
  semantics and wire codecs. Provider profiles reference wire surfaces; they
  do not duplicate codecs.
- One canonical embedded provider-profile data source. Do not land two
  independently edited copies of endpoint/auth/profile metadata.
- Existing provider IDs and EggPool operator configuration semantics remain
  compatible.
- **Hints are advisory.** A hint is `fixed = false` unless first-party evidence
  plus EggPool runtime semantics justify a hard pin. M002 adds no fixed hint.
- **Unknown models stay unresolved.** A model that the first-party table does
  not place gets no hint, however suggestive its name looks. `minimax-m2.5`
  does not inherit `minimax-m3`'s surface.
- **A missing hint and a stale hint are not symmetric.** A hint for a model
  the provider no longer serves is harmless — it documents how to talk to a
  model that may return. A missing hint for a model the provider does serve is
  what breaks a consumer. M002 must not perform a symmetric cleanup that
  removes evidence to make a set comparison tidy.
- The crate remains usable by a sibling Rust consumer without importing the
  EggPool root package or daemon runtime.
- Initial downstream use is by immutable Git revision. Crates.io publication or
  a semver stability promise is separate work.
- **No credential, prompt, raw response body, or cache key** enters the asset,
  the fixture, the snapshot, the plan, or the closure record.

## 5. Scope

### In scope

- Re-review the first-party endpoint table and re-transcribe `REVIEWED_TABLE`
  with the current rows and the new review date, keeping per-row provenance.
- Add non-fixed hints for the placed models that lack one: `beam-free`,
  `minimax-m3.1-flash-free`, `step-5-preview`, `claude-haiku-5-5`.
- Record an explicit, per-row disposition for the six entries the current
  table no longer places.
- Add a **catalog-drift guard** that compares the wire map, the reviewed
  table, and a checked-in dated snapshot of the live catalog, and fails on any
  unexplained delta.
- Add a **manual** refresh path that updates the snapshot. It is never run by
  CI and never runs automatically.
- Record `gemini-4-argon-contributor` and the five unplaced catalog models as
  named findings with their exact status.
- Update the consumer fixture, the provider/catalog documentation, and the
  registry.

### Explicitly out of scope

- **Adding a Google Generative AI wire surface.** That is a change to
  `eggpool-wire`'s surface vocabulary and a codec decision. M002 records the
  finding and leaves `gemini-4-argon-contributor` unresolved.
- **Model availability.** The wire map says *how* to talk to a model; the
  catalog says *whether* it is available. M002 does not add, remove, reorder or
  resurrect a catalog entry, and cannot make `longcat-2.5-preview-free`
  available again — only its upstream can.
- **Any live inference call to determine a surface.** The first-party table is
  the evidence source, as in M001. If the table does not place a model,
  a bounded probe is the fallback and its result becomes the recorded evidence
  source — but no credential appears in any artifact.
- **`fixed = true` hints.**
- **Changing the endpoint URL metadata.** See §3.7.
- **Any CodeGG-side change.** CodeGG consumes the closure revision.
- **Runtime catalog drift detection.** The crate is sans-I/O and CI must stay
  credential-free and network-free. The guard compares committed artifacts.

## 6. Required production changes

### The canonical asset

`rust/crates/eggpool-provider-profile/assets/_provider_profiles.toml` gains
four `model_wire` entries, each `fixed = false`, each placed on the surface the
current first-party table gives it:

```toml
[providers.opencode-go.model_wire."beam-free"]
preferred_surface = "openai_chat_completions"
fixed = false

[providers.opencode-go.model_wire."minimax-m3.1-flash-free"]
preferred_surface = "openai_chat_completions"
fixed = false

[providers.opencode-go.model_wire."step-5-preview"]
preferred_surface = "openai_chat_completions"
fixed = false

[providers.opencode-go.model_wire."claude-haiku-5-5"]
preferred_surface = "anthropic_messages"
fixed = false
```

The implementation must confirm each placement against the first-party table at
implementation time rather than transcribing this block: the table is the
authority, and the plan records what it said on 2026-10-22.

### The reviewed-table fixture

`rust/crates/eggpool-provider-profile/tests/opencode_go.rs` gains:

- the current rows, with the review date updated in the file's evidence-source
  comment;
- **per-row provenance**, so a row carried forward from an earlier review
  (because the current table no longer places it) is distinguishable from a row
  placed by the current table. This keeps the two-direction equality assertion
  meaningful instead of forcing a choice between "drop the evidence" and
  "break the guard";
- an explicit record of the two deliberately-unresolved sets: the catalog ids
  the current table does not place, and the placed model whose surface the
  contract cannot express.

The two-direction assertion in
`reviewed_table_is_covered_exhaustively_by_non_fixed_hints` must keep holding
and must keep meaning "the hints and the reviewed record are the same set".

### The catalog snapshot and the drift guard

A checked-in, dated snapshot of the live catalog — the model ids and the
retrieval date — lives outside the canonical asset directory so that
`exactly_one_canonical_provider_profile_asset_exists` keeps its single
meaning. The snapshot is **not** provider-profile metadata; it is review
evidence.

A drift guard compares three committed artifacts:

1. the wire map in the canonical asset;
2. the reviewed record in the fixture;
3. the dated catalog snapshot.

It fails when:

- a snapshot id has no hint and no recorded "unplaced" entry;
- a hinted model is absent from the snapshot and has no recorded "withdrawn"
  entry;
- a hint's surface disagrees with the recorded placement.

A refresh script rewrites the snapshot from the live catalog. It is a manual
step with an explicit date stamp, it is never invoked by CI, and it prints the
delta it would introduce so a reviewer can see it before it is committed.

## 7. Ordered work packages

### Work package A — Re-review the first-party table

*Intent:* establish the current reviewed record, with provenance, before any
metadata changes.

*Changes:* read the first-party "Endpoints" section; transcribe every row with
its model id, its surface family and its endpoint URL; record the review
date; classify each row as *placed by the current table* or *carried forward
from the 2026-10-07 review, not placed by the current table*.

*Acceptance evidence:* the fixture's evidence-source comment states the review
date and the source, and every row carries its provenance.

### Work package B — Add the missing hints

*Intent:* unblock the models the provider serves and the table places.

*Changes:* add the four entries in §6 to the canonical asset, each `fixed =
false`, each on its reviewed surface.

*Acceptance evidence:* a test resolves each of the four ids through
`model_wire_preference` and asserts the expected surface and `!fixed`; the
two-direction equality assertion stays green.

### Work package C — Disposition the entries the table no longer places

*Intent:* decide, per row and on the record, what happens to an entry whose
placement the current table dropped — without deleting evidence to make a
comparison tidy.

*Changes:* each of the six gets an explicit disposition in the reviewed record:

- `deepseek-v4-flash`, `deepseek-v4-flash-vision-exp`, `space-bunny` — absent
  from both the current table and the live catalog. The hint is **retained**:
  it documents how to talk to a model that was once served and may return, and
  a stale hint costs a consumer nothing. The record states that they are
  withdrawn, so the drift guard's "withdrawn" set explains them.
- `grok-4.6`, `kimi-k2.6`, `minimax-m2.7` — **still in the live catalog**, but
  the current table no longer places them. These are the consequential rows:
  a consumer selecting one would fail at `wire_unresolved` if the hint were
  dropped, and the hint's provenance is a review that has not been superseded
  for them. The default disposition is to **retain** the hint and mark it
  carried-forward, so a catalog model that is served but no longer placed
  keeps working. If implementation evidence shows the placement changed
  (not merely stopped being documented), the hint is updated to the new
  surface and the record says so; if the model no longer answers on that
  surface, the hint is removed and the removal is recorded.

*Acceptance evidence:* each of the six appears in the reviewed record with a
disposition and a reason; the drift guard's withdrawn/placed sets explain
every delta without a suppression list.

### Work package D — Add the catalog snapshot and the drift guard

*Intent:* make the next drift loud instead of silent.

*Changes:* add the dated snapshot, the refresh script, and the guard described
in §6. The guard is a test in the provider-profile crate's test tree and must
read committed files only.

*Acceptance evidence:* the guard passes on the current artifacts; a
deliberately injected delta — a snapshot id with no hint and no disposition —
makes it fail, and the injection is demonstrated before the guard is cited as
evidence.

### Work package E — Record the unmodelled surface and the unplaced models

*Intent:* make the two honest limits visible instead of silently absent.

*Changes:* record `gemini-4-argon-contributor` as placed by the first-party
table on a surface family the shared contract does not model, with the exact
URL, and state that it is deliberately unresolved and why. Record
`glm-5.1`, `minimax-m2.5`, `omen-alpha`, `qwen3.6-plus` and `qwen3.7-max` as
catalog ids with no first-party placement, deliberately unresolved.

*Acceptance evidence:* both sets are asserted as **unresolved** by a test, so a
future change that guesses a surface for one of them fails.

### Work package F — Documentation and the downstream pin

*Intent:* keep the documented contract true and give the consumer something
pinnable.

*Changes:* update the provider/catalog architecture docs to state the current
reviewed date, the unmodelled-surface limit, and the rule that an unplaced
catalog model is unresolved. Update the consumer fixture if it asserts on the
reviewed set. Record the immutable revision for the downstream consumer.

*Acceptance evidence:* the docs name the reviewed date; the consumer fixture
compiles and passes.

## 8. Failure, cancellation, restart, and contention semantics

- A malformed or missing catalog snapshot is a **hard failure** of the guard,
  not an empty set. An absent snapshot must never read as "the catalog is
  empty" and silently pass.
- A missing or malformed canonical asset is a hard failure, as today.
- A model that is neither hinted nor recorded as unplaced nor recorded as
  withdrawn is a **guard failure**, not a warning. The guard is the only
  mechanism that makes the reviewed record trustworthy, so it fails closed.
- The refresh script is a read-only-until-committed operation: it writes the
  snapshot and prints the delta, and a human decides whether to commit. It
  never edits the canonical asset, and it never edits the reviewed record.
- Two concurrent refreshes must not interleave into one snapshot. The script
  writes atomically (write to a temporary file, then rename).
- A live probe, if one is needed for a model the table does not place, is
  bounded: one request per candidate surface, an explicit timeout, and no
  credential recorded in any artifact.

## 9. Compatibility and migration

The asset change is **additive**: four new `model_wire` entries with
`fixed = false`. No existing entry's surface changes under the default
disposition of Work package C, so no consumer's dispatch changes. A consumer
that previously failed closed on `beam-free` or `minimax-m3.1-flash-free` now
resolves; that is the intended effect and the reason for the milestone.

The reviewed-table fixture change alters no runtime behavior; it alters the
evidence record and the drift guard's inputs. The two-direction equality
assertion keeps its meaning.

The new snapshot asset and refresh script add no runtime dependency to the
shared crate: the crate's dependency list stays exactly
`eggpool-wire`, `serde`, `thiserror`, `toml`, and its sources still import no
runtime module. The boundary test must stay green.

There is no storage migration: the shared profile asset is embedded metadata,
not a persisted row format, and the reviewed catalog snapshot is review
evidence, not runtime state.

## 10. Required tests

- `rust/crates/eggpool-provider-profile/tests/opencode_go.rs`:
  - the two-direction exhaustive assertion continues to hold, with the new rows;
  - every hint is `fixed = false`;
  - `beam-free`, `minimax-m3.1-flash-free`, `step-5-preview` resolve to
    `openai_chat_completions` and `claude-haiku-5-5` resolves to
    `anthropic_messages`;
  - `gemini-4-argon-contributor` and the five unplaced catalog models resolve
    to **nothing**;
  - the drift guard passes on the committed artifacts and fails on an injected
    delta.
- `rust/crates/eggpool-provider-profile/tests/boundary.rs` stays green: the
  dependency list is unchanged, the crate sources import no runtime module,
  exactly one canonical provider-profile asset exists, and the runtime plus
  the consumer fixture remain the only readers.
- `rust/crates/eggpool-provider-profile/tests/contract.rs` stays green.
- The consumer fixture still builds and passes.
- EggPool's own provider-profile and wire-resolution tests stay green.
- The focused suite runs with and without default features, so the
  `--no-default-features` parity that the closure process requires is
  preserved.

## 11. Required verification commands

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test -p eggpool-provider-profile
cargo test -p eggpool-provider-profile --no-default-features
cargo test -p eggpool-provider-profile --test opencode_go -- --nocapture
cargo test --workspace --locked
cargo build -p eggpool-provider-profile --no-default-features
```

Repository tests run serially. The refresh script is run **manually**, is not
part of any command above, and is not wired into CI.

## 12. Documentation updates

- `architecture/provider/catalog.md` (or the owning doc, confirmed at
  implementation time): the shared crate is the secret-free metadata owner;
  the reviewed table's current date; the rule that an unplaced catalog model
  is unresolved; the rule that a withdrawn model keeps its hint.
- The provider-profile crate's own README or module docs, if they state the
  reviewed set.
- `plans/subsystems/shared-provider-profile-contract-roadmap.md`: M002 status
  and the milestone status table.
- `plans/registry.md`: the M002 row and the subsystem row.
- `AGENTS.md` only if a reusable crate gained a durable capability other
  consumers need; per this repository's convention, link the registry rather
  than paste plan history.

## 13. Acceptance criteria

M002 may close only when:

1. `beam-free` and `minimax-m3.1-flash-free` resolve to a surface in the
   shared registry, with `fixed = false`, and the resolution is backed by the
   current first-party table.
2. Every placed model the current table gives a surface that the contract can
   express has a hint; the four added in §6 are the only ones missing.
3. `gemini-4-argon-contributor` and the five unplaced catalog models are
   asserted unresolved, and a test fails if a hint is ever added for one of
   them without new first-party evidence.
4. The two-direction exhaustive assertion between the hints and the reviewed
   record still holds and still means what it says.
5. Each of the six no-longer-placed entries has an explicit, recorded
   disposition with a reason; no entry was removed merely to make a set
   comparison tidy.
6. A drift guard compares the wire map, the reviewed record and a dated
   catalog snapshot, and fails on an unexplained delta in either direction.
7. The drift guard is demonstrated to **fail** on an injected delta before it
   is cited as evidence.
8. A missing or malformed snapshot fails the guard rather than passing an
   empty set.
9. The refresh path is manual, atomic, prints the delta it would introduce,
   and is not invoked by CI.
10. The shared crate's dependency list and sans-I/O property are unchanged,
    and the boundary test is green.
11. No credential, prompt, raw response body, or cache key appears in the
    asset, the fixture, the snapshot, the plan, or the closure record.
12. Focused and workspace tests are green, with `--no-default-features`
    parity preserved, and the closure records an immutable revision for the
    downstream consumer.

## 14. Stop conditions

Stop and report rather than improvise if:

- answering `gemini-4-argon-contributor` would require adding a wire surface
  to the shared contract or a codec to `eggpool-wire`;
- the first-party table places a model on a surface family other than the
  three the contract models;
- the table cannot be read at implementation time, and a bounded probe would
  be needed for a model whose surface is otherwise unknown — in particular,
  **do not** infer `minimax-m3.1-flash-free`'s surface from `minimax-m3`'s
  family; probe it or leave it unresolved;
- the live catalog endpoint used for the snapshot requires a credential, which
  would make the snapshot unreproducible in a credential-free environment;
- the drift guard cannot be satisfied without a hand-written suppression list
  that would let a real drift pass;
- closing the delta would require deleting a hint for a model the provider
  still serves, or adding a hint for a model with no first-party placement;
- the reviewed record would have to assert a placement that the current
  first-party table does not make;
- the milestone would require a runtime, network, or credential dependency in
  the shared crate, or a change to EggPool's runtime ownership of catalog
  refresh, routing, quota, health, or persistence.

## 15. Closure evidence required

Create `plans/closure/shared-provider-profile-contract/002-status.md`
containing:

- implementation commit(s) and the immutable downstream revision;
- the reviewed first-party table with its review date and per-row provenance;
- the full delta — current table, live catalog, wire map — before and after,
  with each row's disposition;
- the exact placement evidence for the four added hints, and the explicit
  statement that the `minimax-m3.1-flash-free` placement came from the table
  rather than from a family-name inference;
- the two deliberately-unresolved sets, with the reason for each;
- the drift guard's pass result and its demonstrated failure on an injected
  delta;
- the snapshot's retrieval date and id list;
- focused and broad commands with results, labeled local vs CI truthfully;
- compatibility and secret-safety review;
- unresolved findings by severity;
- the unblock audit for the registered downstream CodeGG plan.

## 16. Handoff notes

The downstream consumer is CodeGG, which pins an immutable revision and
resolves `opencode-go` routes from this contract. CodeGG's recorded finding is
that `beam-free` and `minimax-m3.1-flash-free` are served and free but
unmapped in the shared profile, so it fails them closed at `wire_unresolved`;
`longcat-2.5-preview-free` is withdrawn upstream and answers HTTP 402. M002
closes the first half. It cannot close the second: only the upstream can make
that route available, and a hint for an unavailable model is harmless rather
than harmful.

Do not optimize this milestone around CodeGG-specific connection types or TUI
behavior. The shared contract must remain provider-profile metadata only.
