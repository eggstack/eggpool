# Plan 108 — Compression/Cache Surface Simplification

Date: 2026-08-11
Status: implemented
Parent roadmap: `plans/103-sbc-protocol-parity-and-runtime-efficiency-roadmap.md`
Planning baseline: `de3eeea5936c964ffa33b7939c791e98d35cfcbb`
Depends on:

- `plans/106-provider-native-prompt-cache-translation.md`
- `plans/107-request-memory-and-body-limit-reduction.md` only where shared request/token-estimation helpers are touched.

## Purpose

Reduce optional compression/cache configuration and runtime complexity that is disproportionate for EggPool's local/SBC scope, while preserving actually supported user-visible compression behavior and the provider-native cache translation established in Plan 106.

This plan is a deletion/correction pass, not an adaptive optimization project. Compression remains optional/default-off. Provider-native caching should be preferred over EggPool heuristics when explicit source intent exists.

## Confirmed review findings

### 1. Cross-scope static-prefix validation is implemented at the wrong level

A `CompressionPolicyOverride` validator rejects `compress_static_prefix = true` in a safe-mode override while telling the operator to enable a global `allow_static_prefix_override` setting. A child override validator cannot know the resolved global opt-in, so a configuration that should be legal after global+override resolution can still be rejected prematurely.

### 2. Tuning configuration claims more than the runtime implements

`CompressionTuningConfig` documentation/comments indicate an `apply` mode is accepted for forward compatibility while also describing runtime override behavior. If no production path actually applies those recommendations, accepting the mode creates a misleading configuration contract.

### 3. Compression includes duplicated cheap token-estimation logic

The main request/context estimator now has an ASCII fast path from Roadmap 093, while compression analysis retains its own per-character estimator. If compression is enabled on large coding-agent prompts, this duplicates Python work and implementation semantics.

### 4. Synthetic cache/compression coupling predates current native cache controls

Plan 106 adds provider-native prompt-cache translation. The existing synthetic cache policy may still contain Anthropic-oriented placement/TTL assumptions and compression coupling that are unnecessary for explicit source cache intent.

## Governing constraints

1. Keep compression disabled by default unless current authoritative config says otherwise.
2. Preserve safe supported suffix compression and any other transform with active production callers and documented user-facing behavior.
3. Do not delete a working documented feature merely because it is optional; delete/reject only dormant, unreachable, contradictory, or superseded mechanics.
4. Provider-native explicit cache intent from Plan 106 takes precedence over synthetic cache heuristics.
5. Do not add adaptive tuning, machine learning, background benchmarking, persisted recommendation history, or dynamic policy feedback loops.
6. Do not add a dependency, DB migration/table, background task, CI job, benchmark gate, or metrics subsystem.
7. Disabled compression/cache features must remain cheap: no clients/tasks and no unnecessary full-request scans.
8. Preserve existing request/transcode/failure-isolation/finalization semantics.
9. Local compression policy rejection is a local configuration/request error, not a provider failure.
10. Prefer moving validation to the correct resolved-config boundary over adding cross-object backreferences.
11. Prefer one shared cheap estimator or in-place ASCII fast path over a new generalized tokenization abstraction.
12. Keep configuration migration simple; this is a local appliance project, not a long-lived SaaS compatibility platform.

## Workstream A — Inventory active versus dormant compression/cache surface

Before editing:

```bash
rg -n \
  'Compression|compression|compress_static_prefix|allow_static_prefix_override|CompressionTuningConfig|tuning.*mode|recommend|apply|cache_synthesis|synthetic.*cache|_cheap_tokens|min_stable_tokens' \
  src tests config*.toml docs architecture AGENTS.md README.md
```

For each field/mode/helper classify:

- actively read by production runtime;
- config-only but never consumed;
- observability/recommendation only;
- test-only historical surface;
- documented user-facing supported feature;
- superseded by provider-native cache translation;
- disabled by default but valid when explicitly enabled.

Record the classification in this plan closure. Do not create a permanent feature matrix.

## Workstream B — Fix static-prefix override validation at resolved-policy boundary

Move the legality check to the point where global compression policy and per-provider/per-model override are both available.

Required rule:

- `compress_static_prefix = true` remains forbidden by default if the safe policy intentionally protects cache-stable prefixes;
- it becomes legal only when the authoritative global opt-in permits it;
- a child validator must not reject a value solely because it cannot see the global opt-in;
- final resolved configuration must still reject contradictory/unsafe combinations with a precise error.

Implementation preference:

1. keep local field-shape/type validation inside the child model;
2. perform cross-field/global+override semantic validation in the parent/resolution step already used to compile compression policy;
3. do not add global mutable state or validator context plumbing solely for this rule.

Tests:

- global disallow + override true → reject;
- global allow + override true → accept;
- override false/default → preserve behavior;
- global safe/observe/off mode interactions behave according to current intended semantics;
- `check-config` and runtime policy resolution agree.

## Workstream C — Resolve dormant tuning modes truthfully

Trace every `CompressionTuningConfig` mode into production:

```bash
rg -n 'CompressionTuningConfig|tuning.*(recommend|apply|off)|\.tuning|mode == "apply"|mode == "recommend"' src tests
```

For each accepted mode:

### If runtime behavior exists

Keep it, document the exact bounded effect, and ensure tests exercise the real path.

### If the mode is accepted but dormant

Prefer one of:

- remove it from the accepted enum/config surface;
- reject it during validation with an actionable message if parsing backward compatibility requires retaining the literal temporarily.

Do **not** implement the missing adaptive runtime merely to make the existing config comment true.

If `recommend` is diagnostics-only and actively useful, it may remain without `apply`. If both are dormant or only test scaffolding, remove both.

Update examples/docs so they describe only executable behavior.

## Workstream D — Remove dead tuning state/persistence/observability

After deciding mode disposition, delete fields/helpers/tests used only by removed dormant modes.

Candidates may include:

- target/bounds/cooldown fields never read by production;
- recommendation-to-runtime-override adapters with no callers;
- persistence fields or dashboard rows whose sole consumer is a removed tuning path;
- comments describing future automatic application.

Do not delete generic compression effectiveness observability if it is still used by an active manual diagnostic path.

If deleting a persisted schema field would require a database migration for negligible benefit, leave the historical column frozen and remove only active production writes/reads when safe. Do not create a cosmetic migration solely to reclaim columns.

## Workstream E — Reconcile synthetic cache policy after Plan 106

Plan 106 makes explicit source cache intent provider-native and authoritative.

Audit synthetic policy behavior for requests that already carry/translate native cache controls:

- native explicit boundaries must bypass conflicting synthetic insertion;
- synthetic policy must not rewrite TTL/boundary semantics from Plan 106;
- remove duplicate code that independently walks the same cache controls only to decide something now encoded in the native translation result;
- remove Anthropic-only assumptions that are no longer true across the supported cache path.

For requests **without** explicit source cache intent, preserve any documented synthetic cache feature that is actively used and has clear behavior. Do not auto-enable it.

### Decision rule for synthetic feature retention

Retain a synthetic feature only if all are true:

1. it has a production caller;
2. it is documented/configurable intentionally;
3. it does something not already covered by native source intent;
4. its disabled path is cheap;
5. its semantics are understandable without provider-specific false equivalence.

Delete unreachable/superseded branches that fail these criteria.

## Workstream F — Simplify compression/cache coupling

Review rules that require a matching compression policy before synthetic cache placement or otherwise tie two optional systems together.

Keep coupling only where there is a direct correctness reason, such as preventing compression from mutating an explicitly cache-stable prefix.

Delete coupling that exists only because earlier synthetic cache insertion happened inside compression policy plumbing.

Do not introduce a new policy-composition abstraction. One resolved provider-bound transform sequence with explicit ordering is preferable.

## Workstream G — Centralize/accelerate cheap token estimation

Locate `_cheap_tokens` and the Roadmap 093 estimator.

Preferred order:

1. if the compression estimator can safely call/reuse the existing cheap estimator without creating dependency cycles or changing semantics, use it;
2. otherwise apply the same exact ASCII fast-path logic locally and add a comment linking the semantic invariant;
3. only create a tiny shared helper module if both production paths already need identical semantics and the dependency direction is clean.

Do not introduce a tokenizer dependency or precise provider tokenization library.

Acceptance is semantic equivalence plus removal of obvious per-character ASCII work, not matching provider billing token counts.

## Workstream H — Keep transform ordering explicit

After simplification, document/test the provider-bound order relevant to these features, for example:

1. source parse/canonicalization;
2. protocol translation;
3. native cache-control preservation/translation;
4. compression transforms that are permitted not to invalidate protected prefixes;
5. final provider serialization.

Use the actual existing ordering if different. The key requirement is that native cache boundary correctness is not accidentally changed by cleanup.

Do not build a generic transform graph/planner.

## Workstream I — Remove obsolete tests/config permutations

During implementation, update tests needed to reflect removed configuration values/branches. Do not perform the broader corpus reduction here; Plan 109 owns that.

Required focused tests:

- resolved global/override static-prefix validation;
- every retained tuning mode has a production behavior test;
- removed/dormant mode is rejected or no longer parses as designed;
- native cache boundaries suppress conflicting synthetic insertion;
- retained synthetic cache behavior for no-native-intent requests still works;
- compression cannot silently invalidate explicit native cache boundaries;
- disabled compression/cache path constructs no new runtime tasks/clients and does not scan full request content unnecessarily;
- ASCII estimator equivalence for representative strings.

Delete tests whose sole subject is a removed dormant mode; do not replace them with equivalent history tests.

## Workstream J — Documentation/config cleanup

Update:

- config model descriptions;
- `config.example.toml`/SBC example only if the affected fields are shown there;
- bundled config copies;
- active compression/cache docs/architecture notes;
- `AGENTS.md` only if operator/developer invariants change.

Remove future-tense claims that `apply` or another mode will become active later. Planning documents can preserve history; active config docs should describe current executable behavior only.

## Verification

Run focused compression/cache/transcode/config tests identified by `rg`, then:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

Use a temporary config for the global-allow + static-prefix-override case and the disallowed case so `check-config` exercises the actual resolved boundary.

No performance threshold or full-suite run is required.

## Acceptance criteria

- [x] Every compression/cache configuration field/mode is classified as active, intentionally diagnostic, or removed/rejected; no accepted mode remains knowingly dormant while claiming runtime behavior.
- [x] `compress_static_prefix = true` is validated only after the relevant global opt-in is available.
- [x] Global disallow + static-prefix override true is rejected with a clear configuration error.
- [x] Global allow + static-prefix override true is accepted when all other policy conditions are valid.
- [x] Child override validation no longer rejects a combination that the resolved global policy explicitly permits.
- [x] `check-config` and runtime policy resolution agree for these combinations.
- [x] Dormant tuning `apply` behavior is not implemented merely to preserve an overgrown config surface; it is rejected.
- [x] Active `recommend`/diagnostic behavior is retained only if it has a real caller and clear bounded operator value.
- [x] Production generation state used only by removed dormant tuning behavior is no longer constructed.
- [x] No new database migration is created solely to remove historical diagnostic columns.
- [x] Provider-native cache intent from Plan 106 always takes precedence over synthetic cache insertion.
- [x] Synthetic cache branches superseded by native translation are not duplicated.
- [x] Any retained synthetic cache behavior remains opt-in, documented, and cheap when disabled.
- [x] Compression/cache coupling remains only where required for correctness, not historical plumbing convenience.
- [x] Compression token estimation no longer uses an avoidable Python per-character ASCII path.
- [x] Transform ordering keeps explicit native cache boundaries semantically attached and protected.
- [x] Supported safe compression behavior remains functional.
- [x] No adaptive tuning framework, background benchmark, new dependency, DB service, metrics subsystem, CI job, or generic transform planner is added.
- [x] Focused compression/cache/config/transcode tests pass.
- [x] Ruff, Pyright, smoke tests, and both config checks pass.

## Rejection conditions

Reject the implementation if:

- the static-prefix validator still makes a global-policy decision without access to global state;
- dormant `apply` behavior is "fixed" by building a new runtime adaptive controller;
- working documented compression functionality is removed without proving it is dormant/unreachable or handling its config contract clearly;
- native and synthetic cache controls can both insert conflicting boundaries;
- compression silently moves/deletes native cache boundaries;
- simplification adds a generalized policy engine/transform graph;
- a cosmetic DB migration is added just to delete old optional diagnostics;
- disabled feature paths become more expensive;
- CI/dependency surface expands.

## GPT-5.6 Luna implementation sequence

1. Read Plan 103, completed Plan 106 behavior, relevant Plan 107 ownership helpers, this plan, compression/cache config/models, and active docs.
2. Inventory every compression tuning/cache-synthesis field and production caller before deletion.
3. Move static-prefix cross-scope validation to the resolved-policy boundary and add four focused config cases.
4. Trace `recommend`/`apply` end to end; remove/reject dormant modes rather than implementing a new controller.
5. Delete production state/helpers used only by removed modes, avoiding cosmetic schema migrations.
6. Reconcile synthetic cache insertion with Plan 106 native source intent; native wins.
7. Remove correctness-unnecessary compression/cache coupling.
8. Reuse or mirror the exact ASCII fast path for compression token estimation without new tokenizer dependencies.
9. Update focused tests/config/docs only for the final supported surface.
10. Run focused tests and ordinary repository gate.
11. Record implementation SHA, active/removed surface classification, validator resolution rule, retained synthetic behavior, and exact verification results in this plan.
12. Stop; leave broad test deletion to Plan 109.

## Closure

Implementation completed on 2026-08-11.

- Static-prefix compression is active only as an explicit safe-mode opt-in.
  Child rows perform field-shape validation; the parent `CompressionConfig`
  validates the effective global mode and `allow_static_prefix_override`.
- Compression tuning is active recommendation/observability output only.
  `off` and `recommend` are accepted; `apply` is rejected. The generation
  factory no longer constructs a runtime override registry, and no tuning
  state is persisted beyond the existing recommendation diagnostics.
- Safe suffix compression, observe-mode analysis, policy overrides, and
  synthetic cache controls are active documented features. Synthetic cache
  controls remain opt-in and native provider cache boundaries are preserved
  and excluded from duplicate synthetic insertion.
- The shared `request.limits.estimate_text_tokens` helper now supplies the
  compression analyzer and applier, including the exact ASCII fast path.
- No compression/cache fields were added to routing; no migration, task,
  dependency, benchmark gate, or transform planner was introduced.

Verification:

- `uv run ruff format --check src/ tests/ scripts/`
- `uv run ruff check src/ tests/ scripts/`
- `uv run pyright src/ scripts/`
- Focused compression/config/cache suite: 311 passed
- `PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1`: 14 passed
- `uv run eggpool --config config.example.toml check-config`: passed
- `uv run eggpool --config config.sbc.example.toml check-config`: passed
