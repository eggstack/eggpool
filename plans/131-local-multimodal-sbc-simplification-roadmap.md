# Plan 131 — Local LLM, Multimodal, and SBC Simplification Roadmap

## Status

In progress. Phases 1, 2, and 7 are complete. Phases 3–6 and 8 remain.

## Goal

Make EggPool a first-class pool/router for both hosted and locally served LLMs while improving multimodal OpenAI ↔ Anthropic interoperability and reducing complexity that does not materially serve the proxy's core job.

The target remains a single-node, private/LAN deployment suitable for Raspberry Pi-class SBCs. This is not a public multi-tenant gateway and plans must not introduce production-cloud machinery merely because it is conventional elsewhere.

## Baseline

The current repository already has the important foundations:

- generic provider contracts and persistent HTTPX clients;
- OpenAI Chat Completions and Anthropic Messages endpoints;
- bidirectional text/tool/thinking transcoding with partial image/PDF handling;
- quota-aware multi-account routing and provider priority/fairness;
- model discovery and optional collapsed-model routing;
- bounded request-body reads and one-worker/one-thread SBC defaults;
- SQLite WAL with correctness-critical request/reservation/attempt persistence;
- a lean mandatory CI job that runs format, lint, typecheck, and smoke tests;
- Ollama local as an existing provider template, but not yet as a complete first-class local-runtime experience.

The main gaps are local endpoint onboarding, multimodal semantic coverage, serialized upstream size accounting, excessive optional compression surface, and an oversized request coordinator/finalization implementation.

## Global constraints

1. Reuse `ProviderConfig`, provider contracts, `ProviderClientPool`, catalog discovery, existing routing, and `collapse_models`. Do not add a separate local-router subsystem.
2. Represent different local hosts as separate provider instances. Do not add per-account `base_url` only to support local pooling.
3. Do not add OpenAI, Anthropic, Ollama, LM Studio, vLLM, llama.cpp, or LocalAI SDK dependencies. HTTP contracts remain generic.
4. Native protocol passthrough remains preferred over transcoding.
5. Multimodal support must be capability-gated. Never infer that a provider supports a modality/source form merely because it uses an OpenAI- or Anthropic-compatible endpoint.
6. Keep the mandatory dependency set approximately unchanged.
7. Do not add permanent CI jobs, plan-numbered test suites, soak infrastructure, evidence frameworks, benchmark services, or matrices. Add regression tests to existing capability-based suites only.
8. Keep current CI approximately as-is. Each phase should run only the relevant focused tests locally plus the existing smoke/lint/type gates.
9. Avoid speculative micro-optimization. SBC/database changes require measurements against representative local workloads before changing defaults.
10. Do not weaken existing failure isolation, response-handoff, durable reservation, or bounded 1,800-second transient suppression invariants merely to simplify code.

## Phase order

### Phase 1 — Local runtime provider foundation

Plan 132. Add first-class presets and instance-oriented onboarding for Ollama, LM Studio, llama.cpp, vLLM, and LocalAI, plus a generic compatible endpoint path. Correct Ollama verification so it does not depend on one fixed installed model.

### Phase 2 — Multimodal capability model and narrow content IR

Plan 133. Introduce only the minimum canonical representation needed for content blocks and granular modality/source capabilities. Do not canonicalize whole provider requests.

### Phase 3 — Multimodal transcoding and upstream-size enforcement

Plan 134. Use the Phase 2 representation/capabilities to close image, document/PDF, and tool-result media gaps where target protocols can represent them, preserve explicit loss behavior where they cannot, and validate serialized upstream request limits.

### Phase 4 — Semantic compression de-scope

Plan 135. Separate native prompt-cache compatibility from semantic prompt compression. Remove semantic compression from the core path unless repository evidence demonstrates material value that justifies its implementation/test surface.

### Phase 5 — RequestCoordinator decomposition

Plan 136. Reduce the 280+ KB coordinator into ordinary lifecycle components without introducing a framework, orchestration DSL, or additional state-machine layer.

### Phase 6 — SQLite/SBC write-path characterization and narrow tuning

Plan 137. Measure correctness-critical write overhead with local-model workloads, bound WAL residue if useful, and make only evidence-backed default changes.

### Phase 7 — Provider registry/verification cleanup

Plan 138. Audit fixed probe models and experimental templates, prefer catalog-driven verification, prune misleading templates, and keep the connect surface curated rather than endlessly additive.

### Phase 8 — OpenAI Responses API evaluation

Plan 139. After multimodal/content work stabilizes, decide whether Responses materially improves compatibility with local coding-agent clients. This is an evaluation gate, not an automatic implementation mandate.

## Dependency graph

`132` can land independently. `133` must land before `134`. `135` should land after `134` so multimodal work is not performed against a moving compression surface. `136` should land after the protocol-facing changes because it is structural. `137` may measure earlier but should change defaults after `136` stabilizes. `138` can partially overlap `132` but should close after local onboarding behavior is known. `139` must come last.

## Success criteria for the roadmap

The line of work is complete when:

- at least Ollama, LM Studio, llama.cpp, vLLM, and LocalAI can be added as named local provider instances without hand-editing provider internals;
- two or more local hosts exposing the same model can participate in existing collapsed-model routing without a new routing subsystem;
- local verification discovers installed models instead of requiring a hardcoded model;
- OpenAI and Anthropic clients preserve supported image/document/tool-result media across protocol boundaries with explicit loss reporting for unsupported forms;
- upstream serialized request-size limits are enforced against the actual outgoing body, not only decoded attachment bytes;
- native prompt-cache compatibility remains intact while semantic compression is either removed/isolation-complete or retained only with explicit evidence and sharply bounded scope;
- `RequestCoordinator` is materially smaller and lifecycle ownership is easier to reason about without changing external behavior;
- SBC SQLite changes, if any, are justified by measured data and do not create an alternate consistency mode;
- provider onboarding is curated and fixed-model verification brittleness is reduced;
- no new mandatory runtime dependency and no new mandatory CI job are introduced;
- existing smoke/lint/type gates remain sufficient for repository-wide CI.

## Explicit non-goals

- automatic LAN scanning/discovery;
- distributed consensus or cross-node router coordination;
- provider SDK adoption;
- full OpenAI API parity;
- embeddings/audio/transcription routing unless required by a later separately approved scope;
- production-grade multi-tenant auth/rate limiting;
- replacing FastAPI/HTTPX/aiosqlite/Granian solely for theoretical efficiency;
- weakening crash-recovery and failure-containment invariants.
