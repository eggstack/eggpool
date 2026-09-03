# Plan 163 — Model-Router Configuration, Registry, and Virtual-Model Foundations

Date: 2026-09-03
Status: complete (verified 2026-09-03)
Planning baseline: `525189763a3a6d506e9e8001e2426c9bd9a247fe`
Parent roadmap: `plans/162-optional-llm-model-router-selection-roadmap.md`
Priority: P1 configuration/runtime correctness
Execution target: GPT-5.6 Luna or comparable implementation model

## Objective

Build the typed, deterministic, live-reloadable foundation for optional LLM model routers without yet redirecting client requests through them.

This phase must make the feature safe to configure and safe to leave completely unconfigured. It establishes the vocabulary and runtime ownership that later phases depend on: virtual model ID, selector concrete model, route label/description, concrete target, default target, compiled compact route ID, router fingerprint, and generation-owned registry.

The central regression rule is that `AppConfig()` with no `model_routers` input behaves exactly as it did before this phase.

---

## Files expected to change

Primary implementation surfaces:

- `src/eggpool/models/config.py`
- new `src/eggpool/model_router/__init__.py`
- new `src/eggpool/model_router/config.py`
- new `src/eggpool/model_router/registry.py`
- `src/eggpool/runtime_manager.py`
- `src/eggpool/generation_factory.py`
- `src/eggpool/config_reload_policy.py`

Tests should be added to existing capability-based suites where they naturally fit, plus narrowly scoped model-router unit tests such as:

- `tests/unit/test_model_router_config.py`
- `tests/unit/test_model_router_registry.py`

Do not modify `src/eggpool/routing/router.py`, `quota/scorer.py`, provider health/backoff code, or DB migrations in this phase.

---

## 1. Typed configuration

Add an empty-by-default top-level field to `AppConfig`:

```python
model_routers: dict[str, ModelRouterConfig] = Field(default_factory=dict)
```

Keep the Pydantic `extra = "forbid"` posture.

Recommended models:

```python
class ModelRouteConfig(BaseModel):
    model: str
    description: str

class ModelRouterConfig(BaseModel):
    selector_model: str
    default_model: str
    routes: dict[str, ModelRouteConfig]
    sticky: bool = True
    affinity_ttl_s: float = 43_200.0
    selector_timeout_s: float = 2.0
    max_input_bytes: int = 2_048
    repair_attempts: Literal[0, 1] = 1
```

Exact numeric bounds should be conservative and SBC-friendly. Suggested validation ranges:

- `affinity_ttl_s`: `1 .. 604800` seconds;
- `selector_timeout_s`: `0.05 .. 30` seconds;
- `max_input_bytes`: `128 .. 16384` bytes;
- `repair_attempts`: exactly 0 or 1.

Do not add temperature, top-p, seed, tokenizer, selector concurrency, chain depth, or arbitrary generation-option dictionaries in v1. The selector protocol should stay narrow enough to reason about and test.

TOML shape:

```toml
[model_routers.implementer]
selector_model = "qwen3-0.6b/local"
default_model = "muse-spark-1.3"
sticky = true
affinity_ttl_s = 43200
selector_timeout_s = 2.0
max_input_bytes = 2048
repair_attempts = 1

[model_routers.implementer.routes.Implementer-hard]
model = "muse-spark-1.3"
description = "Use for the most difficult queries."
```

The TOML key `implementer` is the exact client-visible virtual model ID. The route key `Implementer-hard` is an operator-facing policy label, not the upstream model identifier.

---

## 2. Structural validation

Perform validation that depends only on operator configuration at config-parse time. Do not require network/catalog health to parse a valid config.

Required checks:

### Virtual model IDs

- non-empty after trimming;
- bounded length (128 UTF-8 bytes is sufficient for the initial contract);
- reject CR/LF/NUL and other control characters;
- reject `/` because existing `parse_model_provider()` treats suffixes as provider qualification and virtual aliases must remain unambiguous;
- exact/case-sensitive identity, consistent with ordinary model IDs.

Do not silently normalize/case-fold the public alias.

### Selector/default/route targets

- non-empty bounded strings;
- may use the existing `model/provider` concrete reference form;
- `default_model` must exactly equal at least one route's `model` value;
- after the complete `model_routers` mapping is known, reject any `selector_model` or route target that equals a configured virtual model ID. This is the v1 recursion/cycle prevention rule;
- do not reject an otherwise valid concrete model merely because it is absent from the current catalog at startup. Model discovery may be transient and providers may populate later.

### Routes/descriptions

- at least one route;
- route labels are non-empty, bounded, and control-character free;
- route descriptions are non-empty after trimming;
- descriptions are bounded individually (for example 512 UTF-8 bytes) and the final compiled policy has an additional aggregate hard ceiling;
- do not impose a small fixed route-count maximum. The aggregate compiled-policy limit is the real resource guardrail.

Treat operator route descriptions as trusted configuration but not as authorization policy.

---

## 3. Deterministic compilation

`registry.py` should compile each validated `ModelRouterConfig` into an immutable request-path representation. Do not repeatedly parse Pydantic dictionaries or assign route IDs on every request.

Recommended immutable structures:

```python
@dataclass(frozen=True, slots=True)
class CompiledModelRoute:
    route_id: str
    label: str
    model: str
    description: str

@dataclass(frozen=True, slots=True)
class CompiledModelRouter:
    virtual_model: str
    selector_model: str
    default_model: str
    routes: tuple[CompiledModelRoute, ...]
    route_by_id: Mapping[str, CompiledModelRoute]
    config_fingerprint: str
    static_policy: str | bytes
    sticky: bool
    affinity_ttl_s: float
    selector_timeout_s: float
    max_input_bytes: int
    repair_attempts: int
```

Implementation details:

1. Sort routes by their operator label before assigning compact IDs so TOML insertion order cannot change selector semantics unexpectedly.
2. Use deterministic compact IDs. Decimal (`0`, `1`, ...) is acceptable and easiest to inspect. If the implementation uses base36 for very large route sets, test it as a stable encoding and do not expose the encoding as an operator API.
3. Normalize route descriptions only with a deliberately specified, deterministic operation (for example trim outer whitespace + collapse internal ASCII whitespace). Do not perform semantic rewriting.
4. Compile the static selector policy once per generation. Enforce an aggregate UTF-8 byte ceiling after compilation; reject the candidate config generation if it is exceeded.
5. Compute `config_fingerprint` from the semantic router configuration that affects decisions: virtual ID, selector model, default model, ordered labels/targets/descriptions, sticky flag, affinity TTL, selector timeout, input budget, repair count, and selector-protocol version. Use a stable standard-library hash such as SHA-256 over canonical length-delimited UTF-8 fields. Never rely on Python's randomized `hash()`.
6. Keep route lookups O(1) and immutable on the hot path.

The fingerprint is not a secret and is not an authorization token. It exists solely to invalidate affinity decisions when a router's semantics change.

---

## 4. Registry behavior

Provide a generation-owned `ModelRouterRegistry` with a minimal request-path API, for example:

```python
registry.get(virtual_model_id) -> CompiledModelRouter | None
registry.is_virtual(model_id) -> bool
registry.virtual_model_ids -> tuple[str, ...]
```

When the configured mapping is empty, prefer `None`/an immutable empty registry that requires no auxiliary allocation per request. The client hot path should ultimately be able to do one cheap feature-off branch and continue unchanged.

The registry must not query health, quota, provider clients, the database, or the network. It is configuration state only.

---

## 5. Runtime ownership and staged rehash

Add the compiled registry to `RuntimeGeneration` or its immutable request state. It belongs to the generation because the configuration can change atomically on `rehash`.

`generation_factory.build_runtime_generation()` should compile the candidate registry while constructing the candidate generation. A bad model-router configuration must fail candidate construction before publication; it must not partially mutate the current live generation.

Add `model_routers` to `config_reload_policy.py` as `ReloadDisposition.LIVE` only after the registry construction path is atomic and complete. Prefer classifying the whole mapping (`"model_routers"`) as one live field rather than manually enumerating dynamic TOML route keys.

Acceptance cases:

- no routers -> add routers via rehash;
- change route description/target/default/selector via rehash;
- remove a router via rehash;
- invalid candidate router -> rehash rejected, old generation continues serving;
- unrelated config rehash leaves unchanged compiled router fingerprint identical.

Affinity continuity itself is Plan 165; this phase only establishes stable fingerprints and generation boundaries.

---

## 6. Virtual/concrete ID collision contract

The registry must define collision behavior before client integration lands.

Rules:

1. A configured virtual alias is an explicit operator declaration and wins exact unsuffixed lookup over a same-named discovered concrete catalog entry.
2. Do not fail or tear down a healthy running generation merely because a later background catalog refresh discovers the collision.
3. Record/log one bounded diagnostic for the conflict; avoid per-request warning spam.
4. Provider-qualified concrete model references remain concrete and reachable when the provider supports them.
5. Plan 166 will suppress/annotate ambiguous unsuffixed concrete exposure when synthesizing `/v1/models` output.

Add tests that make the precedence deterministic. Do not leave this as incidental dictionary-order behavior.

---

## 7. Feature-off characterization before later hot-path work

Before Plan 164/166 changes request dispatch, add characterization tests that capture the current behavior with `model_routers = {}`:

- `AppConfig()` parsing and TOML parsing still succeed without the new section;
- unknown fields remain rejected;
- the generation builds with no model-router service work required;
- background task registration/count is unchanged;
- no DB migration/version change is introduced;
- current provider/account router construction arguments are unchanged;
- existing `/v1/models` unit/integration expectations remain unchanged at this phase;
- configuration diff output does not invent dynamic per-route secret values or leak unrelated config.

Where practical, construct a sentinel model-router registry/service that raises if touched and prove existing concrete request tests never touch it after Plan 166 integration. Establishing the expected test hook now will make the final feature-off identity assertion stronger.

---

## 8. No catalog-availability validation at config parse time

Do not convert model-router configuration into an accidental startup dependency on remote/local model discovery.

For example, this config remains structurally valid even if `qwen3-0.6b/local` is temporarily offline during startup:

```toml
[model_routers.implementer]
selector_model = "qwen3-0.6b/local"
default_model = "muse-spark-1.3"

[model_routers.implementer.routes.default]
model = "muse-spark-1.3"
description = "General/default implementation model."
```

At request time, selector unavailability is Plan 164's fallback condition. If the configured default is also unavailable, the existing concrete-model error behavior applies. This is more robust than making the optional selector a startup/readiness dependency.

---

## Test requirements

Add focused tests for:

- empty/default config;
- multiple independent routers;
- exact public alias preservation;
- invalid slash/control/empty virtual aliases;
- empty routes;
- invalid descriptions;
- default not present among targets;
- selector -> virtual and target -> virtual recursion rejection, including cross-router cycles;
- provider-qualified concrete references accepted;
- route ordering stable across differently ordered source mappings;
- compact IDs stable;
- compiled static policy byte-for-byte stable;
- aggregate policy ceiling rejection;
- SHA-256 configuration fingerprint stability and meaningful invalidation;
- rehash classification and invalid-candidate rollback;
- concrete catalog absence not making structurally valid config invalid;
- virtual/concrete collision precedence;
- no dependency/DB schema/background-task changes.

Run the relevant config/runtime/reload unit and integration suites plus Ruff/Pyright for touched modules. Do not run live provider tests merely to validate configuration compilation.

---

## Acceptance criteria

Plan 163 is complete when:

1. `model_routers` is a fully typed optional AppConfig surface with empty default.
2. Invalid/cyclic/ambiguous structural router definitions fail deterministically.
3. Valid routers compile to immutable stable route IDs, static policies, and configuration fingerprints.
4. The registry is generation-owned and safe under staged rehash.
5. Empty configuration adds no DB state, background work, mandatory dependency, or provider/account routing behavior.
6. Collision precedence is explicitly tested.
7. Later phases can consume one immutable `CompiledModelRouter` without reparsing TOML or inventing their own validation.

## Closure evidence

Implemented the typed `model_routers` configuration, structural validation,
deterministic immutable compilation, generation-owned registry, atomic live
reload wiring, exact alias precedence contract, and feature-off characterization
without database migrations or background-task changes. The local CI-equivalent
gate passed, and the full suite passed with 7,843 tests passed and 42 expected
skips. Live provider tests were not needed for this configuration-only phase.
