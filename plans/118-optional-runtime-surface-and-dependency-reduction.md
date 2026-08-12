# Plan 118 — Optional Runtime Surface and Dependency Reduction

Date: 2026-08-11
Status: ready
Parent roadmap: `plans/113-sbc-hotpath-reduction-and-protocol-clarity-roadmap.md`
Planning baseline: `6f4df9bd42b5ca336d3da5ef458ab1793e515185`
Depends on:

- `plans/114-provider-payload-copy-on-write.md`
- `plans/117-provider-cache-dialect-correctness.md`

## Purpose

Reduce optional configuration/runtime/dependency surface that is disproportionate to EggPool's local/SBC role while preserving features that are demonstrably useful and already cheap when disabled.

This is a deletion/simplification pass, not a feature-development pass.

The principal candidates are:

- recommendation-only compression tuning;
- reserved/future compression placement/configuration values;
- synthetic cache controls after provider-native cache dialect correction;
- custom DNS cache and its detailed diagnostic surface;
- legacy/deprecated observability/config aliases touched by these systems;
- `granian[pname]` if the process-name extra is not actually used by EggPool.

The core runtime dependency set and SQLite/network architecture are protected.

## Governing constraints

1. Do not replace FastAPI, Granian, HTTPX/httpcore, aiosqlite, Pydantic, Click, or SQLite.
2. Do not add any runtime dependency while simplifying optional features.
3. Do not add an alternative DNS library, compression library, cache server, telemetry library, or configuration framework.
4. Prefer deletion/rejection over implementing dormant/reserved features.
5. Disabled optional features must remain effectively zero-work: no task, timer, segmentation, resolver wrapper, or database write solely because a config object exists.
6. Preserve deterministic safe suffix compression if it remains an actively documented/useful feature.
7. Preserve cache-boundary protection for any retained compression.
8. Preserve provider-native cache intent/capability semantics from Plan 117.
9. Do not remove per-account proxy support or optional `pproxy`; that is a concrete provider-routing feature.
10. Do not remove optional `orjson`; it is a useful existing hot-path acceleration with stdlib fallback.
11. Do not change provider connection pool defaults in this plan.
12. Do not change SQLite pragmas, writer topology, analytics durability, routing, finalization, rehash, or retry behavior.
13. User-visible config removal must fail clearly or be migrated/documented; do not silently reinterpret old keys.
14. Do not add deprecation infrastructure more complex than the feature being removed.
15. No permanent benchmark/telemetry framework.

## Workstream A — Build a production-reference inventory

For each candidate subsystem/field, use `rg` to identify:

- configuration model;
- startup/generation construction;
- background tasks;
- request hot-path callers;
- dashboard/stats/CLI consumers;
- documentation/examples;
- tests;
- migration/database fields if any.

Classify each candidate as:

1. active user-visible behavior;
2. active observability-only behavior;
3. dormant/reserved config with no production behavior;
4. compatibility alias;
5. dead/unreachable implementation;
6. optional but justified by a concrete deployment use case.

Do not commit a permanent inventory document. Summarize decisions in closure.

## Workstream B — Compression placement/config truthfulness

Current compression policy has historically accepted placement/config concepts beyond the actually supported safe suffix path.

Audit the current implementation after Plans 114/117 and enforce a truthful schema:

- values with real production behavior remain accepted and documented;
- reserved/future values with no active behavior are rejected at config validation or removed;
- config comments must not imply a runtime mode exists when production never executes it;
- do not implement `after_cache_boundary`, `anywhere`, static-prefix expansion, or other dormant modes merely to justify their schema presence.

If static-prefix compression remains available as an explicit dangerous opt-in from Plan 108, retain it only if there is a real supported implementation and focused contract. Otherwise reject/remove it rather than carrying a safety switch for nonexistent behavior.

## Workstream C — Recommendation-only compression tuning

The tuning subsystem is currently recommendation/observability-oriented rather than an automatic runtime controller.

Trace end-to-end consumers:

- what constructs the tuning state;
- what observations it stores in memory;
- whether CLI/dashboard/users actually consume recommendations;
- whether recommendation outputs influence any supported operator workflow;
- how many config models/targets/bounds/tests exist solely for recommendations.

### Default decision rule

If tuning has no concrete active operator consumer beyond diagnostic output, delete the tuning subsystem rather than preserving a large targets/bounds/window/cooldown configuration surface.

Deletion should include:

- configuration models/fields used only by tuning;
- runtime tuning objects/state;
- recommendation calculation helpers;
- recommendation-only diagnostics/API fields with no stable consumer;
- tests that exist solely for deleted tuning;
- stale docs/examples.

Do not create a replacement simpler tuner.

If a concrete active dashboard/CLI workflow clearly depends on recommendations and the code is small/cheap after audit, retain only the minimal recommendation behavior actually consumed and delete unused target/bound knobs.

## Workstream D — Synthetic cache controls after Plan 117

Reassess synthetic cache controls against the corrected provider capability model.

Keep synthetic insertion only when all are true:

1. an actively supported provider/model benefits from explicit controls;
2. the control is not naturally conveyed by client/native source intent;
3. the selected provider capability explicitly verifies the inserted field;
4. the behavior is documented and intentionally opt-in;
5. implementation/test surface is proportionate.

If those conditions are not met, remove synthetic cache insertion and retain only native pass-through/translation plus cache usage observability.

If retained:

- simplify placement/policy options to the actually supported set;
- native source intent always wins;
- disabled path does no segmentation solely for synthetic cache;
- no cache keys/content are stored/logged.

Do not create new synthetic controls to compensate for deleted provider-extension assumptions.

## Workstream E — Custom DNS cache value audit

EggPool's custom DNS cache implements positive/negative caching, stale-if-error, singleflight, resolver timeouts, bounded host/error metrics, and a custom httpcore network backend integration. The normal/SBC profile disables it.

Determine whether it should remain by checking:

- any shipped config/example that enables it;
- documented deployment scenarios requiring it;
- live/manual evidence of DNS churn/resolver failure that motivated it;
- current tests/maintenance size;
- whether HTTPX/httpcore connection reuse already makes DNS cost negligible in intended long-lived provider sessions;
- whether account-proxy paths depend on it.

### Decision rule

If no concrete current use case/evidence exists, remove the custom DNS cache and its config/diagnostics/backend wiring. Rely on the OS resolver + HTTP connection reuse. Do not replace it with another cache.

If retained because there is a documented/reproducible resolver reliability need:

- keep it optional/default-off;
- reduce diagnostics to bounded aggregate counters actually consumed by runtime-status/dashboard;
- remove per-host/worst-misser/error-cardinality detail if no supported operator view consumes it;
- preserve singleflight/negative/stale behavior only if each is justified by the retained use case;
- do not expand the feature.

A retained custom DNS cache must have a one-paragraph closure justification tied to actual EggPool deployment behavior, not generic DNS best practices.

## Workstream F — Verify `granian[pname]`

Inspect packaging and installed dependency metadata to determine what the `pname` extra adds and whether EggPool uses its process-title functionality.

Required verification:

- inspect Granian's current package extra/dependency definition for the pinned compatible major line;
- search EggPool for process-title APIs/configuration;
- install/sync with plain `granian>=2,<3` in the normal local environment;
- run CLI/config checks and a short foreground startup/health smoke with plain Granian if feasible.

If the extra is unused, change the dependency from `granian[pname]` to `granian` and refresh the lockfile through the repository's normal dependency workflow.

If it is required indirectly for an active feature or Granian startup behavior, retain it and record why.

Do not remove Granian itself.

## Workstream G — Deprecated/tombstone config cleanup

Within only the touched optional subsystems, identify compatibility fields that have served their documented transition period and now merely increase schema/tests/docs.

Candidates may include old tuning aliases or observability aliases. Remove them only when:

- current config examples no longer use them;
- active docs identify the replacement;
- there is no project policy requiring indefinite compatibility;
- removal produces a clear `extra=forbid`/validation error rather than silent misinterpretation.

Do not sweep every deprecated field in the repository. Keep this bounded to compression/cache/DNS/dependency-related surface changed here.

## Workstream H — Preserve a minimal useful optional-feature profile

At the end, the expected shape should be close to:

- compression: off by default; optional deterministic safe behavior with only real supported placements/transforms;
- cache: provider-native pass-through/translation first; synthetic insertion only if a proven provider use case survives;
- DNS: OS/httpcore default unless custom cache has demonstrated value;
- JSON: stdlib default-compatible path + optional `orjson` fast extra;
- proxy: optional `pproxy` only for accounts that need outbound proxying;
- observability: coarse/default-low-overhead, no tuning machinery solely for recommendations.

This is a target shape, not a requirement to delete a useful proven feature.

## Focused tests

For every deleted/retained subsystem, maintain one or a few semantic contracts rather than its historical configuration matrix.

At minimum:

- compression disabled path performs no segmentation/analyzer/tuning work;
- retained safe compression still produces correct deterministic output and preserves protected cache/stable regions;
- removed/reserved compression modes fail config validation clearly;
- if tuning is removed, old tuning-only config fails clearly and no runtime tuning object/task is constructed;
- if synthetic cache is removed, native cache translation/pass-through remains correct and disabled config does not trigger segmentation;
- if synthetic cache is retained, one capability-gated insertion and one native-precedence case survive;
- if DNS cache is removed, ordinary provider connection creation/resolution/startup tests pass without custom backend wiring;
- if DNS cache is retained, disabled path constructs no custom resolver backend and retained resolver semantics have compact coverage;
- plain Granian dependency install/startup works if `[pname]` is removed;
- optional `orjson` and `pproxy` extras remain intact.

Delete tests for removed fields/modes rather than preserving compatibility code solely for tests.

## Verification

Run focused config/compression/cache/DNS/client-pool/runtime-startup tests affected by the actual decisions.

If `granian[pname]` is removed, refresh dependency lock state and verify an installation/startup path using the repository's normal toolchain.

Then run:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1
uv run eggpool --config config.example.toml check-config
uv run eggpool --config config.sbc.example.toml check-config
```

No full retained-suite gate.

## Explicit acceptance criteria

- [x] Every compression placement/mode accepted by current config has real production behavior; dormant/reserved values are rejected or removed rather than implemented for completeness.
- [x] Compression remains off by default and disabled path performs no request segmentation/compression work solely for compression.
- [x] Retained safe compression remains deterministic and protects cache/stable regions.
- [x] Recommendation-only tuning is removed; no recommendation-only runtime state or dashboard/API surface remains.
- [x] No automatic/apply tuning controller is introduced.
- [x] Synthetic cache controls and insertion are removed; native cache translation/pass-through remains contract-gated.
- [x] Synthetic cache cannot emit provider-extension fields without Plan 117 capability verification.
- [x] Custom DNS cache and its custom network backend/diagnostics are removed; transport uses the OS resolver and HTTPX pooling.
- [x] OS/httpcore resolution and provider/client-pool behavior remain correct.
- [x] `granian[pname]` necessity is verified against current package metadata and EggPool usage.
- [x] `granian[pname]` is retained because `cli_full.py` actively sets Granian's `process_name="eggpool"`, and the `pname` extra supplies `setproctitle`.
- [x] `orjson` fast extra and optional `pproxy` support remain available.
- [x] No core dependency/framework is replaced and no new runtime dependency is added.
- [x] No SQLite/provider-pool/routing/finalization behavior is changed; rehash test fixtures only gain isolated safe socket directories.
- [x] Removed config fields fail clearly rather than silently changing meaning.
- [x] Tests/docs for deleted optional surfaces are removed rather than maintaining dead compatibility apparatus.
- [x] Focused affected tests pass.
- [x] Ruff, Pyright, 14 smoke tests, and both config checks pass.

## Rejection conditions

Reject the implementation if:

- dormant modes are implemented rather than deleted/rejected merely because the schema exposed them;
- recommendation tuning is replaced by another tuning framework;
- custom DNS cache is replaced by another DNS dependency/cache;
- deleting DNS cache breaks per-account proxy or normal provider transport behavior;
- cache simplification sends unverified provider fields;
- safe compression mutates cache-protected/stable content unexpectedly;
- config deletion silently reinterprets old values;
- Granian extra is removed without verifying install/startup;
- FastAPI/Granian/HTTPX/Pydantic/SQLite are replaced;
- dependency or CI surface grows.

## Handoff sequence

1. Read Plan 113, completed Plans 114/117, this plan, optional subsystem config/runtime constructors, pyproject/lockfile, and owning tests/docs.
2. Inventory production consumers before deleting anything.
3. Make compression schema truthful first; reject dormant placement/mode values.
4. Decide recommendation tuning by concrete consumer value; delete if diagnostic-only/no consumer.
5. Reassess synthetic cache after Plan 117's capability model.
6. Decide custom DNS cache from actual EggPool use evidence; do not generalize from generic resolver theory.
7. Verify `granian[pname]` and remove only if truly unused.
8. Delete orphaned tests/docs/helpers/config for removed surface.
9. Run focused verification and ordinary gate.
10. Record implementation SHA, retained/removed feature table, DNS/tuning/dependency decisions and evidence, and exact verification results.
11. Stop. Broader dependency/framework replacement is explicitly out of scope.

## Implementation closure

Implemented on `main` in the changes following baseline `6f4df9bd42b5ca336d3da5ef458ab1793e515185`.

| Surface | Decision | Evidence |
| --- | --- | --- |
| Compression | Retained only `suffix_only` observe/safe behavior; old static-prefix/reserved placement language removed. | Compression policy/analyzer/apply contract tests and config validation. |
| Tuning | Deleted recommendation state, configuration, queries, dashboard/API exposure, and tuning-only tests. | No production references remain. |
| Synthetic cache | Deleted controls, insertion modules, runtime wiring, persistence writes, dashboard/API exposure, and tests. | Native cache translation and boundary-preservation tests remain. Historical migrations remain frozen and unwritten. |
| DNS | Deleted the custom resolver cache, backend, config, metrics, dashboard, and tests. | HTTPX transport/client-pool tests use ordinary OS resolution and connection reuse. |
| Dependencies | Kept `granian[pname]`, `orjson`, and `pproxy`; added no dependency. | `cli_full.py` uses Granian `process_name`; installed metadata identifies `pname` as `setproctitle`. |

Verification completed:

- `uv sync --frozen --extra ci`
- `uv run ruff format --check src/ tests/ scripts/`
- `uv run ruff check src/ tests/ scripts/`
- `uv run pyright src/ scripts/`
- `PYTHONHASHSEED=0 TZ=UTC uv run pytest tests/smoke/ -q --tb=short --maxfail=1` — 14 passed
- `uv run eggpool --config config.example.toml check-config` — passed
- `uv run eggpool --config config.sbc.example.toml check-config` — passed
- Focused compression/dashboard/reload/protocol/rehash suites passed, including 44 rehash acceptance/operator tests after isolated socket-fixture corrections.
