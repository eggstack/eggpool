# W007 Closure — Multimodal, Documents, Cache Controls, and Provider Adaptation

Status: closed

Implementation commit: [`b11bf5bdc15ecc23af2545c33188ad14f6d25612`](https://github.com/eggstack/eggpool/commit/b11bf5bdc15ecc23af2545c33188ad14f6d25612)

Plan: [W007 — Multimodal, documents, cache controls, and provider adaptation](../../implementation/canonical-wire/007-multimodal-documents-cache-and-provider-adaptation.md)

Contract and oracle: [M6 canonical wire contract](../../canonical-wire-contract.md),
the W001 observations under `fixtures/canonical-wire/`, W002 bounded admission
and canonical IR, W004-W006 finite codecs/adaptation policy, and the Python
multimodal/document/cache/transcoder tests.

## Outcome

W007 extends the Rust canonical boundary with bounded, typed media sources and
provider-neutral cache markers. Request admission now accepts and validates
inline data URIs/base64, external URL references, provider file identities,
image detail hints, document/file forms, and audio as an explicit unsupported
semantic form. Media count, aggregate estimate, encoded-size, reference-length,
MIME, detail, and marker bounds are applied before expensive copying or target
encoding. Tool-result media is retained as canonical media when the target
grammar can carry it.

Finite OpenAI Chat, OpenAI Responses, Anthropic Messages, Gemini Interactions,
and Gemini generateContent paths now preserve supported image/document forms,
map image hints where available, relocate compatible cache markers, and return
shared typed warning/rejection outcomes for unsupported forms. Provider response
image/document parts decode to canonical output media and finite client
responses preserve those parts rather than converting them to misleading text.
Audio remains an explicit unsupported transformation wherever the finite target
contract cannot carry it.

The implementation is synchronous and value-only. It performs no URL fetch,
filesystem read, upload, OCR, archive/decompression processing, provider HTTP,
retry, database write, background task, auth-header construction, or usage
accounting mutation. `Debug` implementations expose only byte lengths and
presence flags for media and marker-bearing structures.

## Fixture and requirement-to-evidence matrix

| W007 requirement / fixture | Evidence | Result |
|---|---|---|
| External URL image is preserved without dereference | `admission_preserves_bounded_media_forms_without_dereferencing_them`; `cross_wire_media_preserves_inline_and_reference_semantics` | Pass |
| Inline image/data URI is preserved with media type and detail | `cross_wire_media_preserves_inline_and_reference_semantics`; `MediaSource.detail` admission path | Pass |
| Malformed and oversized base64 fails before adaptation | `malformed_and_oversized_media_is_rejected_before_encoding`; `validate_base64` limit path | Pass |
| Count and aggregate media bounds | `validate_media_limits` with `MAX_MEDIA_ITEMS` and `MAX_MEDIA_AGGREGATE_BYTES`; admission limit tests | Pass |
| Documents remain distinct from images | document/file/file-id admission, target-specific document encoders, finite response media test | Pass |
| Provider file/reference identity passes through without upload | `MediaSource.file_id`, bounded `valid_reference`, OpenAI/Anthropic/Responses/Gemini pure encoders | Pass |
| Tool-result media is not silently flattened when representable | `tool_result_media_stays_nested_when_target_supports_it`; canonical attachment in tool-message admission | Pass |
| Image hints and unsupported-target behavior | detail preservation for OpenAI targets; `image_detail_not_representable` notice and W006 policy | Pass |
| Cache marker placement and unsupported controls | `cache_markers_are_relocated_or_rejected_by_target_policy`; `request_notices` validation and target mapping | Pass |
| Finite response media is not textified | `finite_response_media_is_not_textified`; OpenAI Responses and Gemini response media branches | Pass |
| Redaction and diagnostic safety | `media_debug_never_contains_inline_data`; custom `Debug` for `MediaSource`, content, output, and response | Pass |
| No network/filesystem/media-processing side effect | synchronous source inspection plus external-reference fixtures; no transport, filesystem, archive, or media dependency in W007 paths | Pass |

## Differential result

The targeted Python oracle and contract suites passed 237 tests with 3 skips.
The Rust implementation matches the oracle's canonical semantic decisions and
explicit loss behavior; it does not attempt byte-for-byte identity where the
target grammars intentionally differ. The Rust finite suite covers all 148
tests across 17 suites. Existing W002 admission and W006 loss behavior remains
green, including explicit rejection rather than silent text substitution for
unsupported finite media/cache transformations.

## Verification commands actually run

Passed:

```text
rtk cargo fmt --manifest-path rust/Cargo.toml
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check
rtk cargo check --manifest-path rust/Cargo.toml
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1 -q  # 148 passed (17 suites)
rtk uv run pytest tests/migration_rs tests/unit/test_wire_ir.py tests/unit/test_wire_codecs.py tests/unit/test_transcoder/test_multimodal.py tests/unit/test_transcoder/test_cache_stability.py tests/unit/test_transcoder/test_cache_stability_integration.py tests/unit/test_transcoder/test_sensitive_media.py tests/unit/test_prepared_transcode.py tests/unit/test_prepared_transcode_reuse.py tests/contract/test_transcoder_contract.py -q --tb=short --maxfail=1  # 237 passed, 3 skipped
rtk uv run ruff format --check src/ tests/ scripts/
rtk uv run ruff check src/ tests/ scripts/
rtk uv run pyright src/ scripts/
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
rtk git diff --check
```

No live provider, credential, database migration, network, filesystem, upload,
or paid inference call was used. No dependency or lockfile change was needed.

## Resource, security, and dependency review

- Admission validates encoded sizes before any base64 decode/copy and uses
  checked/saturating aggregate accounting for media limits.
- URI/file identity handling is bounded and opaque; no URL or file identity is
  dereferenced.
- Media bytes, data URIs, prompt content, and marker payloads are absent from
  `Debug` diagnostics and typed codec errors.
- MIME/detail/cache metadata is structurally validated and bounded; arbitrary
  provider extensions are not treated as executable behavior.
- No new Cargo dependency, async task, thread, HTTP client, filesystem API,
  archive processor, OCR path, credential path, or persistence write was added.

## Supported differences and deferred work

- W007 implements pure target-surface adaptation. Provider/model capability
  facts remain explicit caller/profile context; dynamic provider negotiation,
  learned preferences, alternate-wire retry, and account-specific capability
  selection remain outside M6 and are not inferred from account names or error
  history.
- Audio is retained in the canonical request long enough to produce an
  explicit unsupported outcome; W007 does not invent a provider audio mapping.
- Cache token accounting, SSE framing, incremental event assembly, usage
  merging, and native terminal evidence remain W008 responsibilities.
- Selected-profile runtime composition remains W009. Provider submission,
  retry, health/failure effects, persistence, handoff, cancellation, timeout,
  and finalization remain M7 responsibilities.

No unresolved mandatory W007 requirement remains.

## Registry transition and future-plan audit

W007 is removed from the dependency-ready table and recorded in the completed
table in `migration-rs/registry.md`. Its plan is marked closed, and this file
is the accepted historical evidence.

W008 is the only future plan unblocked by W007 under the repository's serial
handoff policy, so it is promoted to `dependency-ready; W007 closure accepted`
in the plan header/index, handoff sequence, roadmap, and registry. W009 remains
blocked on W008, W010 remains blocked on W009, and M7 implementation handoff
remains blocked on accepted W010 closure. No other future plan can be safely
unblocked by W007 alone.

Recommendation: accept W007 closure and proceed with W008 only.
