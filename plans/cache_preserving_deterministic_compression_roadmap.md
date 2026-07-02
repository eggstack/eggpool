# Cache-Preserving Deterministic Compression Roadmap

Date: 2026-07-01

## Purpose

EggPool is a lightweight provider/account aggregation proxy. Its primary routing goal is to balance requests predictably across many interchangeable accounts, especially same-provider pools such as multiple Opencode Go subscriptions. Compression and provider-cache optimization must therefore be implemented as request shaping and observability features, not as a replacement for existing quota/fairness routing.

This roadmap adds cache-aware metrics and deterministic compression while preserving the current routing model. The guiding rule is:

> Preserve provider-cacheable stable prefixes by default. Compress only volatile suffixes and pathological payloads unless context pressure or explicit operator policy says otherwise.

This avoids breaking provider-side prompt caches to save a smaller number of tokens. Provider caches are usually prefix-sensitive; mutating the stable prefix can turn an otherwise cheap cached request into a fully uncached request. EggPool should make this visible, preserve cache locality, and then apply deterministic compression where it is unlikely to harm cache hits or semantic correctness.

## Non-goals

- Do not add a learned/semantic compressor to the core hot path.
- Do not make LLMLingua-style, embedding, or model-based compression a required dependency.
- Do not add semantic response caching in this line of work.
- Do not route same-provider accounts based on recent cache-hit economics by default.
- Do not let compression silently alter system/developer instructions, tool schemas, provider-native cache controls, or stable conversation prefixes.
- Do not turn EggPool into the coding agent's memory manager. Agent-side compaction remains the responsibility of clients such as Opencode, Claude Code, Aider, CodeGG, and similar agents.

## Design principles

1. Metrics first. Measure provider-reported cache behavior and request shape before mutating traffic.
2. Cache preservation before compression. Stable prefixes should remain byte/canonical-equivalent through parsing, segmentation, transcoding, and compression.
3. Deterministic transforms only in core. The first mutating compressor should fold logs, repeated lines, search results, blobs, and other structurally obvious waste.
4. Same-provider account fairness remains primary. Cache and compression data may be displayed and used for operator tuning, but must not silently skew account selection.
5. SBC-safe implementation. Keep CPU, memory, dependency, and latency overhead low; all expensive features must be bounded and optional.
6. Full provenance. Every compression decision should record what changed, why it changed, how many tokens were estimated/saved, and whether cache-protected content was preserved.
7. Provider-specific behavior is isolated. OpenAI, Anthropic, Gemini-like, OpenRouter-like, and other provider cache counters should normalize into a common internal shape without forcing all providers to support every field.

## Proposed user-facing configuration shape

Initial defaults should be non-mutating and cache-preserving.

```toml
[cache]
mode = "preserve" # off | preserve | optimize
track_usage = true
preserve_stable_prefix = true
synthetic_cache_controls = false
stable_prefix_strategy = "detect" # none | detect | explicit
min_stable_prefix_tokens = 1024

[compression]
enabled = false
mode = "observe" # observe | safe | balanced
placement = "suffix_only" # suffix_only | after_cache_boundary | anywhere
respect_cache_boundaries = true
compress_static_prefix = false
min_candidate_tokens = 2048
min_savings_tokens = 1024
max_compression_latency_ms = 25

[compression.transforms]
fold_repeated_lines = true
compact_logs = true
compact_search_results = true
elide_base64_blobs = true
minify_machine_json = true
compact_stack_traces = true
```

`cache.mode = "optimize"` and `synthetic_cache_controls = true` should remain later-phase opt-ins. They should not be required for deterministic compression.

## Internal request model

Introduce cache/compression annotations on the canonical request representation. The representation does not need semantic model understanding; it needs structural segmentation.

Segments:

- `stable_prefix`: system/developer instructions, tool schemas, stable provider-native cache blocks, persistent project rules, long-lived static context.
- `semi_stable_context`: prior conversation turns, selected file snippets, repo summaries, current task state.
- `volatile_suffix`: latest user turn, latest tool output, command logs, test output, grep/search results, generated blobs, repeated stack traces, one-off retrieved context.

Compression policy should default to `volatile_suffix` only. `stable_prefix` should be protected unless an operator explicitly enables non-default aggressive behavior or the request cannot otherwise fit within the selected model context.

## Metrics model

Normalize provider and local metrics into per-request records. Suggested fields:

- provider ID
- account name or account ID
- requested model
- routed upstream model
- client protocol
- upstream protocol
- transcoded boolean
- stream boolean
- request shape hash
- stable prefix hash
- estimated input tokens before compression
- estimated stable prefix tokens
- estimated semi-stable tokens
- estimated volatile suffix tokens
- estimated input tokens after compression
- provider-reported input tokens
- provider-reported output tokens
- provider-reported cached input tokens when available
- provider-reported cache read input tokens when available
- provider-reported cache creation/write input tokens when available
- compression mode
- compression applied boolean
- compression reason codes
- compression latency milliseconds
- compression warnings
- cache-boundary preservation status

Dashboard and API views should display cache hit ratio and compression savings separately. Avoid reporting a misleading single "tokens saved" metric that treats cached prefix savings and volatile suffix compression as equivalent.

## Roadmap phases

### Phase 1: Cache and token observability  ✅ *Implemented*

Add provider usage normalization and persistence for cache-related token counters. Do not mutate requests. Do not alter routing. This phase establishes baseline provider-cache behavior and exposes unknown/missing cache metrics explicitly.

Implementation: `src/eggpool/proxy/normalized_usage.py`, migration `0040_cache_token_observability.sql`, finalizer+repository updates in `src/eggpool/request/`, dashboard panel under Runtime → Cache observability, JSON endpoint `GET /api/stats/cache-observability`. Routing isolation is pinned by `tests/unit/test_routing.py::test_scorer_does_not_consume_cache_counter_status`. Full design: `cache_compression_phase_01_cache_token_observability.md`.

Deliverables:

- Normalized cache usage dataclass/model.
- Provider-specific extraction for OpenAI-style, Anthropic-style, Gemini-style if already present or easy to support, and generic unknown fallback.
- SQLite migration for cache counters and request shape metadata.
- Stats/dashboard/API updates for provider/account/model cache ratios.
- Tests for usage normalization and backward compatibility with providers that do not emit cache fields.

### Phase 2: Canonical request segmentation

Add structural segmentation into stable prefix, semi-stable context, and volatile suffix. Do not compress yet. The output is annotation metadata used by later phases.

Deliverables:

- Segment model and annotation API.
- OpenAI Chat Completions segmentation.
- Anthropic Messages segmentation.
- Tool schema/system/developer/cache-control recognition.
- Volatile tool-output/log/search-result classification.
- Segment token estimates and stable prefix hashing.
- Tests demonstrating stable prefix detection and no request mutation.

### Phase 3: Transcoder cache stability and boundary preservation

Ensure identical client prefixes produce identical provider-visible prefixes when config and input are identical. Preserve native cache fields where possible. Do not inject variable metadata into model-visible stable prefixes.

Deliverables:

- Deterministic serialization/ordering audit.
- Cache-boundary preservation through OpenAI-to-Anthropic and Anthropic-to-OpenAI paths.
- Explicit loss accounting for cache fields that cannot be represented in the target protocol.
- Optional internal warnings, but no unstable model-visible warning injection in stable prefixes.
- Golden tests for provider-visible prefix stability.

### Phase 4: Observe-mode deterministic compression accounting

Run deterministic analyzers and record what would be compressed, without mutating request bodies. Use segmentation and cache boundaries to suppress candidates in stable prefixes.

Deliverables:

- `[compression]` config with observe mode.
- Compression candidate analyzers for logs, repeated lines, stack traces, grep/search results, base64/blob content, machine JSON, and generated/vendor-like blocks.
- Token savings estimates and reason codes.
- Dashboard/API metrics for compression opportunity versus cache-protected content.
- Tests that observe mode never changes outbound requests.

### Phase 5: Safe suffix-only deterministic compression

Enable the first request-mutating compressor under explicit config. Apply only to volatile suffix regions by default and preserve stable prefixes exactly/canonically.

Deliverables:

- Safe transform implementations.
- Stable model-visible elision markers with hashes/counts.
- Per-request compression metadata and warnings.
- Request-level override headers.
- Safety tests for no stable-prefix mutation.
- Replay fixtures covering coding-agent tool output, test logs, grep output, and large blobs.

### Later phases

Phase 6: Policy controls, per-client/per-provider overrides, and hard safety rails.

Phase 7: Dashboard/runtime optimization views, including compression latency, cache ratios, account distribution, suppressed candidates, and effective uncached-token savings.

Phase 8: Routing guardrails. Explicitly prevent same-provider cache-hit metrics from skewing account fairness by default. Add diagnostics if advanced cache-aware routing is ever enabled.

Phase 9: Opt-in synthetic provider cache controls, especially OpenAI-client to Anthropic-provider cache breakpoint synthesis. Begin with observe-only diagnostics.

Phase 10: Closed-loop runtime tuning of compression thresholds only; routing fairness remains separate.

Phase 11: Expanded replay fixtures and regression tests for prefix stability, compression correctness, and provider usage normalization.

Phase 12: Operator documentation and recommended profiles for SBC/default, debugging, and aggressive cost reduction.

## Implementation order summary

1. Record cache metrics.
2. Segment canonical requests.
3. Make transcoding cache-stable.
4. Observe compression opportunities.
5. Mutate only volatile suffixes under safe mode.
6. Add policy controls and dashboard views.
7. Add optional provider-specific cache optimizations.
8. Tune thresholds using runtime metrics while leaving fair account routing intact.

## Acceptance criteria for the full roadmap

- EggPool can report provider/account/model cache hit ratios without changing traffic.
- Stable prefixes remain unchanged through compression-safe mode.
- Safe compression reduces large volatile tool/log/search payloads deterministically.
- Compression is disabled or observe-only by default.
- Same-provider account routing remains quota/fairness-first and does not skew based on recent cache hit ratios.
- All request mutations are auditable with reason codes, token estimates, and warnings.
- SBC deployments can run the feature without heavy ML dependencies.
