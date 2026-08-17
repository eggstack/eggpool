# Plan 135 — Phase 4: Semantic Compression De-scope

## Objective

Reduce EggPool's implementation and test surface by separating protocol-level prompt-cache compatibility from semantic request compression and removing the latter from the core product unless measured evidence justifies retention.

## Rationale

Native prompt-cache controls are protocol/provider compatibility facts and belong in EggPool. Semantic prompt compression is optional request mutation, is disabled by default, and substantially expands policy, analysis, copy-on-write, segmentation, diagnostics, and testing complexity.

This plan is reductive. It must not replace the existing compressor with a new plugin framework.

## Decision gate

Before implementation, inspect current runtime use, documentation promises, and retained tests. If there is no strong evidence that users depend on semantic compression, remove it.

If retention is justified, isolate it behind one optional narrow interface and ensure the disabled path imports/constructs essentially none of the semantic analyzer/apply machinery. Do not add packaging/plugin infrastructure merely to retain it.

## Work items if removing

1. Preserve native cache-boundary translation and cache usage accounting.
2. Remove semantic compression analyzer/apply/policy-resolution code and configuration that only serves semantic mutation.
3. Remove segmentation paths that have no remaining consumer.
4. Remove compression-specific request diagnostics/schema writes only when current schema-freeze/migration policy permits; historical columns can remain inert rather than creating destructive migrations.
5. Remove documentation/dashboard controls that imply semantic compression remains supported.
6. Fold any generally useful small helper into an appropriate existing module rather than preserving an empty subsystem.

## Work items if retaining narrowly

1. One top-level optional feature gate.
2. No work, segmentation, hashing, analyzer allocation, or imports on the disabled request path beyond constant-time flag checks.
3. No routing influence.
4. No new tuning registry or adaptive controller.
5. Keep only deterministic suffix-safe behavior that can be tested with a small capability-based suite.

## Tests

For removal, preserve regression tests for:

- native Anthropic/OpenAI cache-boundary translation;
- cache usage normalization/accounting;
- no cache/compression field entering `QuotaFairScorer`;
- ordinary requests no longer invoking segmentation/compression machinery.

Delete plan/history-specific compressor tests that no longer protect a shipped capability.

## Acceptance criteria

- Native provider prompt caching semantics remain supported at the same or better compatibility level.
- Semantic compression is either removed or reduced to a sharply isolated optional surface justified by evidence.
- Disabled/default request paths do not segment or semantically inspect content for compression.
- No replacement plugin architecture, worker, registry, or CI job is introduced.
- Request schema history is not churned merely for cosmetic cleanup.
- Repository source/test surface is materially smaller if the feature is removed.

## Out of scope

Provider-side context caching, application-level summarization, RAG compression, tokenizer-aware prompt rewriting, and automatic adaptive prompt optimization.
