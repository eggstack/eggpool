# Model-Info Suffix and Endpoint Hardening Plan

## Context

The suffix/benchmark/startup-task implementation pass substantially improved the live state:

- OpenRouter matching is broadly working; most models now show `partial` rather than `sparse`.
- A deployment-suffix matching tier was added for provider aliases such as `MiniMax-M2.7-highspeed`.
- Artificial Analysis source diagnostics are now surfaced through `/api/model-info/sources`.
- Background task snapshots now expose first-run state, and key tasks such as `model_info_refresh` and `checkpoint` are registered with `run_immediately=True`.

The repo is close, but there are two remaining hardening issues worth closing:

1. `generate_deployment_suffix_variants()` tokenizes with the broad separator regex but reconstructs stripped base names using only `raw.split("-")`. This works for the observed live MiniMax rows (`MiniMax-M2.7-highspeed`) but can fail for equivalent provider names using `_`, `.`, space, or `:` separators.
2. `/api/model-info/{model_id}/aliases` and `/api/model-info/{model_id}/matches` still decode `model_id` directly with `unquote(model_id)`, while detail lookup correctly uses `_decode_model_info_lookup_id()` to strip configured provider suffixes. This can make `/detail` work for provider-suffixed IDs while aliases/matches miss.

This plan is a narrow hardening pass around those edge cases plus final live verification.

## Goals

1. Make deployment-suffix stripping separator-agnostic across the same separator family used by `normalize_model_key()` and `tokenize_model_key()`.
2. Preserve the original namespace prefix for slash-delimited source IDs.
3. Keep semantic-variant safety unchanged: never strip `pro`, `mini`, `flash`, `lite`, `plus`, `preview`, `code`, `omni`, etc.
4. Align aliases/matches endpoint lookup with detail endpoint lookup for provider-suffixed path IDs.
5. Add tests that cover hyphen, underscore, dot, space, colon, and slash-prefixed deployment suffix variants.
6. Add API tests proving detail, aliases, and matches all resolve provider-suffixed IDs through the same canonical lookup key.
7. Re-run live highspeed, source diagnostics, and background task first-run checks.

## Non-goals

- Do not expand the safe suffix list unless there is live evidence.
- Do not enable similarity matching by default.
- Do not change dashboard table layout.
- Do not change Artificial Analysis source behavior beyond verifying the diagnostics already added.
- Do not change route shapes.

## Phase 1: Fix separator-agnostic deployment suffix stripping

### Current issue

`tokenize_model_key()` splits on the broad separator regex:

```python
_SEP_RE = re.compile(r"[-_:. /]+")
```

But `generate_deployment_suffix_variants()` reconstructs the base variant via:

```python
segments = [t for t in raw.split("-") if t]
base_value = "-".join(base_segments)
```

This means these may not strip correctly:

```text
MiniMax_M2.7_highspeed
MiniMax M2.7 highspeed
MiniMax.M2.7.highspeed
MiniMax:M2.7:highspeed
minimax/MiniMax_M2.7_highspeed
```

### Required behavior

All of the following should emit the original and a base variant:

```text
MiniMax-M2.7-highspeed      -> MiniMax-M2.7
MiniMax_M2.7_highspeed      -> MiniMax_M2.7 or MiniMax-M2.7, deterministic
MiniMax M2.7 highspeed      -> MiniMax M2.7 or MiniMax-M2.7, deterministic
MiniMax.M2.7.highspeed      -> MiniMax.M2.7 or MiniMax-M2.7, deterministic
MiniMax:M2.7:highspeed      -> MiniMax:M2.7 or MiniMax-M2.7, deterministic
minimax/MiniMax-M2.7-fast   -> minimax/MiniMax-M2.7
```

The emitted base does not need to preserve the exact separator if the normalized key is correct, but it must be deterministic and diagnostics-friendly.

### Suggested implementation

Prefer regex suffix removal over manual hyphen splitting.

Implementation sketch:

```python
_DEPLOYMENT_SUFFIX_RE = re.compile(
    r"(?i)(?P<sep>[-_:. /]+)(?P<suffix>highspeed|fast|turbo|speed|lowlatency|lowlat)$"
)


def _strip_deployment_suffix_segment(raw: str) -> tuple[str, str] | None:
    match = _DEPLOYMENT_SUFFIX_RE.search(raw)
    if match is None:
        return None
    suffix = match.group("suffix").casefold()
    if suffix not in DEPLOYMENT_SUFFIX_TOKENS:
        return None
    base = raw[: match.start("sep")]
    if not base or not has_digit_or_family_anchor(base):
        return None
    tokens = tokenize_model_key(raw)
    if set(tokens) & SEMANTIC_VARIANT_TOKENS:
        return None
    return base, suffix
```

Then in `generate_deployment_suffix_variants()`:

1. Split namespace with `split_source_id()` instead of raw `if "/" in raw` logic.
2. Apply `_strip_deployment_suffix_segment()` to the model segment only.
3. Reconstruct `f"{namespace}/{base}"` when namespace exists.
4. Return `(value, stripped)` if stripped exists and differs from `value`.

Important: avoid treating vendor namespace slashes as separators to strip. For `minimax/MiniMax-M2.7-highspeed`, only operate on `MiniMax-M2.7-highspeed`.

### Tests

Add/extend `tests/unit/test_model_info_deployment_suffix.py`.

Positive cases:

```python
@pytest.mark.parametrize(
    ("raw", "expected_norm"),
    [
        ("MiniMax-M2.7-highspeed", "minimaxm27"),
        ("MiniMax_M2.7_highspeed", "minimaxm27"),
        ("MiniMax M2.7 highspeed", "minimaxm27"),
        ("MiniMax.M2.7.highspeed", "minimaxm27"),
        ("MiniMax:M2.7:highspeed", "minimaxm27"),
        ("minimax/MiniMax-M2.7-fast", "minimaxminimaxm27"),
    ],
)
def test_deployment_suffix_variants_strip_all_supported_separators(raw, expected_norm):
    variants = generate_deployment_suffix_variants(raw)
    assert len(variants) == 2
    assert normalize_model_key(variants[1]) == expected_norm
```

For slash-prefixed source IDs, also assert namespace preservation:

```python
assert generate_deployment_suffix_variants("minimax/MiniMax-M2.7-highspeed")[1].startswith("minimax/")
```

Safety cases:

```python
@pytest.mark.parametrize(
    "raw",
    [
        "MiniMax-M2.7-pro",
        "mimo-v2.5-pro",
        "qwen3.7-plus",
        "kimi-k2.7-code",
        "hy3-preview",
        "deepseek-v4-flash",
        "gpt-5-mini-turbo",
        "highspeed",
    ],
)
def test_deployment_suffix_variants_do_not_strip_semantic_or_unanchored_names(raw):
    assert generate_deployment_suffix_variants(raw) == (raw,)
```

Resolver cases:

- `MiniMax_M2.7_highspeed` resolves to `minimax/minimax-m2.7`.
- `MiniMax M2.7 highspeed` resolves to `minimax/minimax-m2.7`.
- Ambiguous candidates still return `ambiguous_deployment_suffix_candidates`.

## Phase 2: Align aliases/matches endpoints with detail lookup

### Current issue

`handle_model_info_detail()` uses:

```python
_decoded_id, lookup_id, _provider_suffix = _decode_model_info_lookup_id(request, model_id)
info = await model_info.get_summary(lookup_id)
```

But `handle_model_info_aliases()` and `handle_model_info_matches()` currently use:

```python
decoded_id = unquote(model_id)
... repo.get_aliases_for_model(decoded_id)
... repo.list_match_evidence(decoded_id)
```

This means provider-suffixed IDs can resolve in detail but fail in diagnostics endpoints.

### Required behavior

For a configured provider suffix such as `opencode-go`, all of these should resolve to the same canonical model:

```text
GET /api/model-info/minimax-m3
GET /api/model-info/minimax-m3%2Fopencode-go
GET /api/model-info/minimax-m3/aliases
GET /api/model-info/minimax-m3%2Fopencode-go/aliases
GET /api/model-info/minimax-m3/matches
GET /api/model-info/minimax-m3%2Fopencode-go/matches
```

### Implementation

Use `_decode_model_info_lookup_id()` in aliases and matches handlers too.

For aliases:

```python
decoded_id, lookup_id, provider_suffix = _decode_model_info_lookup_id(request, model_id)
flat_aliases = await model_info.repo.get_aliases_for_model(lookup_id)
source_rows = await model_info.repo.list_alias_rows_for_model(lookup_id)
return JSONResponse(
    content={
        "model_id": lookup_id,
        "requested_model_id": decoded_id,
        "provider_suffix": provider_suffix,
        "aliases": flat_aliases,
        "aliases_by_source": source_rows,
    }
)
```

For matches:

```python
decoded_id, lookup_id, provider_suffix = _decode_model_info_lookup_id(request, model_id)
evidence = await model_info.repo.list_match_evidence(lookup_id, source=None)
return JSONResponse(
    content={
        "model_id": lookup_id,
        "requested_model_id": decoded_id,
        "provider_suffix": provider_suffix,
        "object": "list",
        "data": compact_evidence,
    }
)
```

Preserve legacy `model_id` if tests expect it? Prefer canonical `model_id` plus `requested_model_id` for clarity.

### Tests

Add/extend `tests/unit/test_model_info_route_registration.py` or `test_model_info_match_evidence_api.py`.

1. `test_aliases_endpoint_resolves_provider_suffixed_id_to_canonical`
   - Configure `providers={"opencode-go": ...}` on app state.
   - Mock repo expects `get_aliases_for_model("minimax-m3")`, not `"minimax-m3/opencode-go"`.
   - Request `/api/model-info/minimax-m3%2Fopencode-go/aliases`.
   - Assert response `model_id == "minimax-m3"`, `requested_model_id == "minimax-m3/opencode-go"`, `provider_suffix == "opencode-go"`.

2. `test_matches_endpoint_resolves_provider_suffixed_id_to_canonical`
   - Same pattern for `/matches`.

3. `test_aliases_and_matches_unsuffixed_ids_remain_unchanged`.

## Phase 3: Strengthen diagnostics for deployment suffix matches

### Current state

The deployment suffix tier records:

```json
{
  "stripped_variant": "MiniMax-M2.7",
  "base_variant": "MiniMax-M2.7",
  "normalized_key": "minimaxm27",
  "matched_source_model_id": "minimax/minimax-m2.7",
  "candidate_count": 1
}
```

### Hardening

Add the stripped suffix token and original raw candidate to diagnostics when possible:

```json
{
  "raw_candidate": "MiniMax-M2.7-highspeed",
  "stripped_suffix": "highspeed",
  "base_variant": "MiniMax-M2.7"
}
```

Fix the current debug breadcrumb in `_tier_deployment_suffix_normalized_exact()` that sets `raw_original` to `local_raw_candidates[0]`, which may not be the candidate that produced the stripped variant.

Suggested internal structure:

```python
@dataclass(frozen=True)
class DeploymentVariant:
    raw_candidate: str
    stripped_variant: str
    stripped_suffix: str
    normalized_key: str
```

Or minimally track a dict keyed by stripped variant.

### Tests

Assert match evidence diagnostics include:

- `match_method = deployment_suffix_normalized_exact`
- `raw_candidate`
- `stripped_suffix = highspeed`
- `base_variant`
- `matched_source_model_id`

## Phase 4: Live verification commands

After implementation, run:

```bash
BASE="http://127.0.0.1:11300"
DB="usage.sqlite3"

curl -sS -X POST "$BASE/api/model-info/refresh?model_id=MiniMax-M2.7-highspeed&force=1" \
  | python3 -m json.tool

curl -sS "$BASE/api/model-info/MiniMax-M2.7-highspeed" | python3 -m json.tool
curl -sS "$BASE/api/model-info/MiniMax-M2.7-highspeed/matches" | python3 -m json.tool
curl -sS "$BASE/api/model-info/MiniMax-M2.7-highspeed/aliases" | python3 -m json.tool

sqlite3 "$DB" <<'SQL'
.headers on
.mode column

SELECT model_id, status, sparse,
       json_extract(provenance_json, '$.sources') AS sources,
       json_extract(detail_json, '$.external_ids.openrouter') AS openrouter_id,
       substr(summary, 1, 140) AS summary
FROM model_info_canonical
WHERE lower(model_id) LIKE '%highspeed%'
ORDER BY model_id;

SELECT model_id, provider_id, source, alias, confidence, active,
       match_method, discovered_by, diagnostics_json
FROM model_info_aliases
WHERE lower(model_id) LIKE '%highspeed%'
ORDER BY model_id, source, alias;

SELECT model_id, provider_id, source, alias, match_method, confidence,
       diagnostics_json, last_seen_at
FROM model_info_match_evidence
WHERE lower(model_id) LIKE '%highspeed%'
ORDER BY last_seen_at DESC;
SQL
```

Expected:

- `MiniMax-M2.1-highspeed`, `MiniMax-M2.5-highspeed`, and `MiniMax-M2.7-highspeed` move to `partial` after refresh cycles.
- `external_ids.openrouter` points to the base MiniMax source ID.
- match evidence uses `deployment_suffix_normalized_exact`.
- diagnostics include the stripped suffix.

For endpoint parity:

```bash
curl -sS "$BASE/api/model-info/minimax-m3%2Fopencode-go/matches" | python3 -m json.tool
curl -sS "$BASE/api/model-info/minimax-m3%2Fopencode-go/aliases" | python3 -m json.tool
```

Expected:

- response canonical `model_id` is `minimax-m3`.
- response `requested_model_id` is `minimax-m3/opencode-go`.
- rows are not empty merely because the request ID was suffixed.

Source diagnostics:

```bash
curl -sS "$BASE/api/model-info/sources" | python3 -m json.tool
```

Expected:

- `artificial_analysis` appears even if disabled/missing key.
- disabled/missing-key reason is explicit.

Background first-run state:

```bash
curl -sS "$BASE/api/stats/runtime" | python3 -m json.tool
```

Expected:

- `model_info_refresh` and `checkpoint` no longer show opaque never-ran state after startup.
- tasks waiting for delayed first run report `first_run_state=never_run_not_due` or `never_run_startup_deferred` with `next_run_at`.

## Phase 5: Test commands

Run focused tests:

```bash
uv run pytest tests/unit/test_model_info_deployment_suffix.py \
              tests/unit/test_model_info_matching_safety.py \
              tests/unit/test_model_info_tiered_matching.py \
              tests/unit/test_model_info_match_evidence_api.py \
              tests/unit/test_model_info_route_registration.py \
              tests/unit/test_model_info_source_diagnostics.py \
              tests/unit/test_background_first_run.py
```

Run broader model-info subset:

```bash
scripts/test_model_info_identity.sh
uv run pytest tests/unit/test_model_info*.py
```

## Acceptance criteria

1. Deployment suffix stripping works for hyphen, underscore, dot, colon, and space separators.
2. Namespace-prefixed source IDs preserve the namespace while stripping only the model segment.
3. Semantic variants are not stripped.
4. Deployment suffix match evidence includes raw candidate, stripped suffix, base variant, normalized key, and matched source ID.
5. `/aliases` and `/matches` use the same provider-suffix canonicalization as detail lookup.
6. Live highspeed rows resolve to base MiniMax OpenRouter IDs.
7. Artificial Analysis source diagnostics remain visible and explicit.
8. Background task first-run state remains clear after this patch.

## Suggested commit message

```text
Harden model-info suffix matching and diagnostics endpoints
```

## Notes for implementer

Keep this patch narrow. The preceding implementation already added the major behavior. This hardening pass is about making the suffix tier robust across provider naming separators and ensuring all diagnostic endpoints resolve the same canonical ID as the detail endpoint.
