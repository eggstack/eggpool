# D009 — Selection Fairness and Frozen Routing-Trace Correction

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/routing-domain-roadmap.md`

Primary class: invariant/corrective

Historical predecessors: D001-D008 are closed and their closure records remain append-only evidence. This plan is a post-D008 corrective pass discovered by independent review of the implemented Rust selection path.

## 1. Objective

Correct two coupled D006/D008 parity defects before M6 is allowed to rely on the M5 routing-domain interface:

1. `fairness_mode = "random"` is represented in Rust configuration and diagnostics but does not affect actual `RoutingRouter::select_and_claim` selection; and
2. the Rust routing trace can be rebuilt after claim publication, so its candidate scores/order/fairness metadata may describe post-claim state rather than the state that actually produced the accepted selection.

Keep this pass narrow. Do not reopen catalog, quota, health, model-router, transport, request-codec, coordinator, retry, or finalization architecture.

## 2. Why D009 is required

### 2.1 Random fairness is construction-qualified but not selection-qualified

Python `routing.router.Router` shuffles a non-empty near-tie fairness band when `fairness_mode == "random"`; the first shuffled candidate is the selected account.

Rust `RoutingRouter::select_and_claim` currently calls the fairness ordering helper in preview/non-committing mode. In that mode the random branch returns identity order rather than invoking `FairnessRandom::choose_index`. The later fairness commit path advances only round-robin state. Therefore a configured Rust random mode can remain deterministically score/name ordered while diagnostics still identify the mode as random.

The D006 plan explicitly required all supported fairness modes, an injectable RNG, differential selected-account parity, and exactly-once fairness application for the accepted claim. D006 closure proved the injectable dependency exists, but the routing-claim tests use round-robin and do not prove the random branch participates in a real accepted claim.

This is a mandatory parity defect, not an optional improvement.

### 2.2 Routing trace is not frozen at accepted selection

D006 intended the accepted local claim to retain the score/fairness snapshot needed by later durable routing observability.

The current Rust `SelectionClaim` retains selected identity/load/probe ownership but not the accepted score ordering, exclusions, fairness decision, or best-score band. `RoutingRouter::trace_for` rebuilds a fresh routing plan and then annotates it with the already-selected claim.

Claim publication changes mutable routing inputs immediately: active ownership increments and pending request/token load becomes visible. A fresh post-claim plan can therefore legitimately prefer a peer account, change score components, or produce a different fairness preview. Persisting that rebuilt state later would answer “why was this account selected?” with evidence that did not cause the selection.

M7 must receive an immutable, secret-free selection snapshot that represents the exact accepted decision, not a later rescore.

## 3. Preserve historical closure evidence

Do not edit D006 or D008 closure records to pretend this issue was previously covered. Their verification results remain historical evidence.

D009 closure must explicitly state that it supersedes the D006/D008 aggregate conclusion only for:

- actual random fairness selection semantics; and
- frozen selection/trace evidence.

All other accepted M5 evidence remains valid unless D009 implementation uncovers a directly related contradiction.

## 4. Correct random fairness selection

Refactor fairness selection so the read-only and mutating paths are explicit.

Required semantics:

- `build_routing_plan` and readiness remain side-effect free;
- read-only plan construction must not consume random numbers or mutate the round-robin rotor;
- `select_and_claim` must apply random fairness to the actual claimable near-tie band;
- random fairness must remain inside the highest eligible priority tier;
- random fairness must not mix native/transcode groups when `prefer_native` separates them;
- random fairness must not include candidates outside the configured epsilon band;
- random mode must not create or advance round-robin rotor state;
- round-robin behavior must remain one commit per accepted claim;
- `off` remains pure score/name order.

Do not emulate Python's process-global PRNG implementation. Keep Rust's injectable `FairnessRandom` boundary and compare deterministic fixture choices. The compatibility contract is the selected candidate/distribution policy under a controlled source, not Python's internal Mersenne-Twister bitstream.

## 5. Exactly-once random consumption

Random choice is part of the accepted local selection mutation, not diagnostics.

Design the claim path so one accepted random fairness decision consumes exactly one controlled RNG choice for the final claimable band. In particular:

- no RNG consumption from `build_routing_plan`;
- no RNG consumption from readiness;
- no RNG consumption merely to build a routing trace;
- a candidate rejected by a half-open probe race must not leave an observable random-choice side effect for a selection that was never accepted;
- failed pending-claim publication must not report/commit a fairness choice as accepted;
- rollback after an accepted claim does not rewind RNG state, matching the fact that a real selection occurred;
- retries in M7 will be separate explicit selection operations and are out of D009 scope.

If implementation simplicity requires choosing only after probe revalidation, construct the current claimable band under the existing selection mutex, acquire/revalidate the selected probe safely, and redraw/rebuild only when a rejected candidate changes the eligible band. The final observable decision must correspond to the accepted claim and must not leak probe ownership.

## 6. Freeze a selection snapshot

Introduce an immutable value such as `SelectionSnapshot` or `AcceptedRoutingDecision` owned by `SelectionClaim`.

It should retain only non-secret information needed to explain the accepted decision, including at minimum:

- requested canonical model/provider/protocol/surface facts required by the routing trace;
- selected account/provider/model/protocol/tier/native-vs-transcode facts;
- selected score and score components already available from D004;
- best/top score and account before claim publication;
- eligible/scored candidate count;
- stable exclusions relevant to the accepted selection;
- ordered candidates or a bounded trace projection sufficient for current `routing_decisions` semantics;
- fairness mode/scope/key, applied flag, epsilon-band size, ordered band or chosen account/index as appropriate;
- local claim ID when assigned.

Do not store API/proxy credentials, raw request bodies, raw provider errors, session content, or unbounded arbitrary payloads.

Prefer one immutable `Arc`/owned snapshot over retaining references into mutable router state.

## 7. Capture timing and atomicity

The snapshot must represent the same in-memory state that produced the accepted claim.

Capture it inside the local selection transaction after final candidate/probe revalidation and fairness choice, but before publication of active/pending load can affect a later rescore. Assign or attach the final local claim ID deterministically before returning the claim.

The selection mutex must still contain no SQLite or network await. Snapshot construction must be bounded local work.

If score/candidate inputs need cloning, keep the trace projection bounded and avoid copying large catalog metadata. This is SBC-oriented state; do not introduce a general event-sourcing layer.

## 8. Trace API correction

Change `RoutingRouter::trace_for` or replace it with an API that serializes the frozen claim snapshot.

Required behavior:

- no call to `build_routing_plan` is needed to explain an existing accepted claim;
- trace serialization after pending/active publication cannot alter candidate ranking or selected score;
- rollback, conversion to reserved load, and active release do not mutate the snapshot;
- trace creation is read-only and consumes no fairness RNG/rotor state;
- M7 can persist the snapshot into the existing `routing_decisions` representation without rescoring.

For “no candidate” diagnostics, a separate read-only plan/trace may still be built because no accepted claim exists. Do not force every failed selection through an accepted-claim DTO.

## 9. Differential tests — random mode

Add direct tests around the actual `select_and_claim` API, not only fairness helper construction.

Minimum cases:

1. two equal same-tier peers, injected RNG chooses a non-first account, and the accepted claim matches that choice;
2. another controlled RNG value selects the expected peer on the next independent selection;
3. read-only `build_routing_plan` does not advance the RNG source;
4. readiness does not advance the RNG source;
5. `off` remains deterministic;
6. round-robin still alternates/rotates exactly once per accepted claim;
7. strict priority prevents random mode from choosing a lower tier;
8. material score difference prevents random mode from crossing the fairness band;
9. native/transcode separation is preserved;
10. half-open probe contention/reselection produces one final accepted random decision and no leaked probe/pending ownership.

Use a test RNG that records invocation count and supplied candidate count, not only a deterministic sequence.

## 10. Differential tests — frozen trace

Add a regression fixture where claim publication changes the next routing score/order.

Example:

- accounts A and B begin as equal peers;
- the accepted fairness/score decision selects A;
- A's active/pending claim becomes visible, making a newly built plan prefer B;
- the claim's frozen trace must still report the pre-publication accepted ordering/score/fairness facts and selected A;
- calling trace serialization multiple times produces identical output and does not change RNG/rotor/quota/health state.

Also test:

- rollback keeps the snapshot unchanged;
- pending-to-reserved conversion keeps it unchanged;
- active release keeps it unchanged;
- a selected half-open probe snapshot remains stable after probe state later changes;
- no secrets/request content appear in debug/serialized trace values.

## 11. D008 regression rerun

D009 is a corrective closure over M5, so rerun the existing integrated D008 qualification after the targeted tests.

Required gates:

```text
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets
```

Also run:

- targeted `routing_claims` and routing-domain suites;
- model-router/affinity suite;
- quota and health suites;
- D008 integrated qualification suite;
- D001 Python-oracle migration tests relevant to fairness/routing;
- existing targeted Python routing/fairness tests for the frozen oracle;
- migration smoke and `git diff --check`.

If the previously documented all-target fixture hang still exists independently of D009, isolate and document it with the exact reproducer rather than declaring broad success. If D009 touches that path or makes it worse, do not close.

## 12. Acceptance criteria

D009 closes only if all of the following are true:

- configured Rust random fairness materially affects actual accepted selection;
- controlled RNG evidence proves the choice is made through the claim path;
- read-only plan/readiness/trace operations consume no randomness;
- failed/non-accepted claim paths do not falsely record an accepted fairness decision;
- round-robin/off behavior remains unchanged except for any explicitly demonstrated bug fix;
- every accepted claim owns an immutable bounded selection snapshot;
- routing trace for an accepted claim is derived from that snapshot, not a post-claim rescore;
- trace selected/top score, tier, fairness, candidates/exclusions, and account identity are internally consistent with the accepted selection;
- claim rollback/conversion/release preserve the snapshot;
- D008 integrated tests remain green;
- no M6 request parsing, M7 persistence/retry/finalization, schema migration, new HTTP stack, or broad framework is introduced.

## 13. Stop conditions

Do not close D009 if:

- random mode can still return deterministic identity order without consulting the injected source;
- a read-only call consumes RNG or advances the rotor;
- a probe-rejected/non-accepted choice is reported as the final fairness decision;
- accepted trace data is rebuilt from mutable post-claim routing state;
- trace construction can disagree with selected account/score/tier;
- fixing the issue requires moving SQLite/network work under the selection lock;
- a new schema or coordinator implementation is introduced to solve an in-memory snapshot problem.

## 14. Closure and next-plan transition

Create `migration-rs/closure/routing-domain/009-status.md` with the exact implementation commit(s), targeted random-selection evidence, frozen-trace regression evidence, D008 rerun, and unresolved findings.

On accepted D009 closure:

- mark the M5 routing-domain roadmap `closed after D009 corrective pass`;
- keep D001-D008 closure records unchanged as historical evidence;
- move D009 from dependency-ready to completed in the registry;
- re-unblock M6 implementation handoff;
- do not promote M7 until M6 itself closes.

Until D009 closes, M6 implementation handoff is blocked again. M6 research/planning may continue, but no implementation plan should be registered as dependency-ready against a routing selection interface known to have these parity defects.
