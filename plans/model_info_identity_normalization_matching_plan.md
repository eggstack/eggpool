# Model-Info Identity Normalization and Tiered Matching Plan

## Context

The latest live behavior shows the core failure more clearly: with a fresh database, available models are displayed as `sparse` even though provider catalog discovery and dashboard joining now appear to be wired. This strongly suggests the remaining failure is not the dashboard render path, but the identity-resolution path between local provider model names and third-party metadata source model IDs.

The current model-info system is intentionally exact-alias oriented. That is safe, but too brittle for real inference-provider catalogs. Aggregator providers such as `opencode-go` expose model names that often differ from OpenRouter, Artificial Analysis, Hugging Face, or provider-native metadata source identifiers by casing, punctuation, whitespace, vendor prefixes, duplicated vendor names, and display-name decoration.

Example observed problem:

```text
Local provider row:
  provider_id = opencode-go
  model_id    = minimax-m3

Provider-catalog auto alias today:
  opencode-go/minimax-m3

OpenRouter source model ID:
  minimax/minimax-m3

OpenRouter display name:
  MiniMax: MiniMax M3
```

With the current exact-only rules, this fresh-DB row stays `sparse` unless the operator manually configures:

```text
minimax-m3 -> minimax/minimax-m3
```

The correct next line of work is to add a conservative, auditable identity-normalization and tiered matching system. It must improve automatic matching for third-party inference providers without introducing unsafe fuzzy joins.

## Current chain and likely failure point

The current chain is:

1. Catalog refresh discovers routable provider/model rows.
2. `ModelInfoService.load_cache()` seeds configured aliases and persists provider-catalog observations.
3. `reconcile_catalog_snapshot()` creates canonical model-info rows from provider-native detail and any persisted external observations.
4. On a fresh DB, external observations do not exist yet, so rows are provider-only and usually `sparse`.
5. `refresh_due_models()` or manual `refresh_model_info()` fetches OpenRouter/AA/HF metadata and attempts identity resolution.
6. Current identity resolution succeeds for configured exact aliases and a few exact source-ID cases.
7. Most aggregator-provider rows do not match because provider IDs such as `opencode-go` are not third-party source namespaces.

The problematic current assumption is provider-catalog alias generation like:

```python
f"{provider_id}/{model_id}"
```

That is useful when `provider_id` is also the metadata source vendor namespace, such as `openai/gpt-...` or `anthropic/claude-...`, but it fails for aggregator providers. It also does not account for casing, separators, duplicated vendor names, display names, and source-specific naming conventions.

## Goals

1. Add deterministic normalization for model IDs, source IDs, and display names.
2. Add a tiered resolver that tries exact aliases first, then normalized exact/regex/guarded similarity matching.
3. Preserve safety: ambiguous matches must not auto-bind.
4. Persist non-exact matches as alias rows with method/confidence/provenance so future refreshes are stable and auditable.
5. Add diagnostics that explain why each sparse model did or did not match external metadata.
6. Add fixture-based tests built from real catalog shapes and optional live tests behind an environment flag.
7. Improve periodic logging so “attempted 33, matched 0” cannot remain silent.

## Non-goals

- Do not introduce arbitrary fuzzy matching with no guardrails.
- Do not make OpenRouter or any metadata source authoritative for routability.
- Do not change dispatch/routing selection logic.
- Do not treat OpenRouter advisory pricing as cost-accounting truth.
- Do not require live network access in normal CI.
- Do not force operators to maintain hand-written aliases for every model.

## Design principles

### 1. Exact remains highest authority

Configured exact aliases and exact source IDs continue to win. If the operator has configured an alias, the resolver should use it before any normalization or similarity scoring.

### 2. Matching must be explainable

Every non-exact decision should record:

- `match_method`
- `confidence`
- `candidate_source_model_id`
- `candidate_display_name`
- normalized local key
- normalized candidate key
- score, if applicable
- rejected candidate count
- ambiguity reason, if applicable

### 3. Similarity is a last resort

Levenshtein/difflib-style matching should rank candidates only after exact, normalized-exact, and curated regex tiers fail. It should require strong score thresholds, score-gap thresholds, and family/version-token compatibility.

### 4. Ambiguity is a safe miss

If two candidates are plausible, do not bind. Surface `ambiguous_candidates` diagnostics and leave the row `sparse`.

### 5. Provider namespaces are not vendor namespaces

Do not assume `provider_id/model_id` maps to a source ID when `provider_id` is an aggregator such as `opencode-go`. Instead, provider IDs should be one input among several, with explicit provider-to-vendor mapping only where configured or known.

## Phase 1: Add normalized identity primitives

Create a new module:

```text
src/eggpool/model_info/normalization.py
```

Core functions:

```python
def normalize_model_key(value: str) -> str:
    ...

def split_source_id(value: str) -> tuple[str | None, str]:
    ...

def normalize_vendor_key(value: str | None) -> str | None:
    ...

def tokenize_model_key(value: str) -> tuple[str, ...]:
    ...

def strip_provider_namespace(value: str, known_providers: set[str]) -> str:
    ...
```

Required normalization behavior:

1. Unicode normalize with NFKC.
2. Use `.casefold()` instead of `.lower()` for all canonical comparison keys.
3. Strip leading/trailing whitespace.
4. Split slash-delimited source IDs into namespace/vendor and model segment.
5. Remove or normalize punctuation/separators:
   - spaces
   - hyphens
   - underscores
   - colons
   - dots
   - slashes
   - repeated separators
6. Remove non-alphanumeric characters from comparison keys.
7. Collapse obvious duplicate vendor tokens where safe:
   - `MiniMax: MiniMax M3` should be comparable to `minimax-m3`.
8. Preserve raw strings separately for display and diagnostics.

Examples to pin in tests:

```text
MiniMax-M3                  -> minimaxm3
minimax-m3                  -> minimaxm3
MiniMax M3                  -> minimaxm3
MiniMax: MiniMax M3         -> minimaxm3 after duplicate-token collapse
minimax/minimax-m3          -> vendor=minimax, normalized_model=minimaxm3
opencode-go/minimax-m3      -> provider=opencode-go, normalized_model=minimaxm3
Claude Sonnet 4.5           -> claudesonnet45
claude-sonnet-4.5           -> claudesonnet45
GPT_5.5-mini                -> gpt55mini
```

Implementation notes:

- Keep this stdlib-only initially.
- Use `unicodedata.normalize("NFKC", value)`.
- Use `re.sub(r"[^0-9a-z]+", "", casefolded)` after token-aware preprocessing.
- Implement duplicate-token collapse conservatively. For display names of the form `Vendor: Vendor Model`, drop only the first repeated vendor token if it exactly matches the next token after normalization.

## Phase 2: Build source candidate indexes

Add a source-agnostic candidate representation:

```python
@dataclass(frozen=True)
class ModelInfoMatchCandidate:
    source: str
    source_model_id: str
    display_name: str | None
    vendor: str | None
    raw_keys: tuple[str, ...]
    normalized_keys: tuple[str, ...]
    family_tokens: tuple[str, ...]
    version_tokens: tuple[str, ...]
    record: SourceModelRecord
```

Create a helper:

```python
def build_candidate_index(
    source: str,
    records: Iterable[SourceModelRecord],
) -> ModelInfoCandidateIndex:
    ...
```

For OpenRouter, index these raw keys:

- `source_model_id`, e.g. `minimax/minimax-m3`
- source ID model segment, e.g. `minimax-m3`
- display name, e.g. `MiniMax: MiniMax M3`
- normalized display-name variants
- any normalized/raw source-specific IDs already in `record.normalized`

For each key, compute normalized forms and maintain reverse mappings:

```python
exact_by_source_id: dict[str, SourceModelRecord]
by_normalized_key: dict[str, list[ModelInfoMatchCandidate]]
by_vendor_and_key: dict[tuple[str, str], list[ModelInfoMatchCandidate]]
```

Do not collapse multiple candidates into one at index-build time. Preserve ambiguity for the resolver.

## Phase 3: Add tiered matching resolver

Add a new module:

```text
src/eggpool/model_info/matching.py
```

Core API:

```python
@dataclass(frozen=True)
class MatchDecision:
    record: SourceModelRecord | None
    matched: bool
    match_method: str
    confidence: float
    diagnostics: dict[str, object]
    alias_to_persist: str | None = None

async def resolve_source_record_tiered(
    *,
    source: str,
    model_id: str,
    provider_id: str | None,
    display_name: str | None,
    repo: ModelInfoRepository,
    candidate_index: ModelInfoCandidateIndex,
    config: ModelInfoConfig,
) -> MatchDecision:
    ...
```

Tier order:

### Tier 0: configured exact alias

Current behavior. Use `model_info_aliases` rows with `source=<source>` first.

- confidence: existing alias confidence, capped at `1.0`
- method: `configured_exact_alias`
- ambiguity behavior: if multiple indexed aliases survive, no match with `ambiguous_configured_aliases`

### Tier 1: exact source ID

Try exact source ID matches:

- local `model_id`
- any provider-catalog exact aliases already persisted
- known source ID field in provider metadata, if present later

method: `exact_source_id`

### Tier 2: normalized exact key

Compare normalized local candidates to normalized source candidate keys.

Local raw candidates should include:

- local `model_id`
- local display name from provider catalog, if present
- source/provider aliases from provider-catalog observation
- stripped suffix/prefix variants where provider suffix is known

If exactly one candidate matches normalized key, accept.

method: `normalized_exact`
confidence: `0.75` to `0.9`, depending on vendor agreement and key source

Safety:

- If multiple candidates match the same normalized key, try vendor/family tie-breaks.
- If still ambiguous, do not match.

### Tier 3: curated regex rules

Add a small source-specific regex rule registry, probably config-driven with built-in defaults:

```python
@dataclass(frozen=True)
class SourceMatchRule:
    source: str
    pattern: str
    target_pattern: str | None
    vendor: str | None
    family: str | None
    confidence: float
```

Examples:

```text
(?i)^minimax[-_\s:]*m3$ -> vendor=minimax, normalized target minimaxm3
(?i)^claude[-_\s]*(sonnet|opus)[-_\s]*(\d+(?:\.\d+)?)$ -> vendor=anthropic
(?i)^gemini[-_\s]*(\d+(?:\.\d+)?)[-_\s]*(pro|flash|flash-lite)?$ -> vendor=google
```

Do not overbuild this first pass. Start with rules needed for live observed providers and a few widely used model families.

method: `regex_rule`
confidence: `0.70` to `0.85`

### Tier 4: guarded edit-distance ranking

Use stdlib first:

```python
from difflib import SequenceMatcher
```

A true Levenshtein implementation can be added internally later if needed. Do not add dependency unless the repo already allows one.

Candidate filters before scoring:

- same inferred vendor if vendor is known
- same major version token if present
- do not cross critical variant tokens:
  - `mini`
  - `pro`
  - `flash`
  - `lite`
  - `instruct`
  - `chat`
  - `reasoning`
  - `thinking`
  - `preview`
  - date/version suffixes

Acceptance thresholds:

```text
best_score >= 0.92
best_score - second_best_score >= 0.05
same family token when available
same major numeric version when available
```

If these fail, no match.

method: `similarity_guarded`
confidence: score-adjusted, max `0.70`

## Phase 4: Persist discovered aliases with provenance

The existing `model_info_aliases` table may not have fields for `match_method`, `diagnostics`, or `discovered_by`. Inspect migrations before implementing.

Preferred migration:

```sql
ALTER TABLE model_info_aliases ADD COLUMN match_method TEXT;
ALTER TABLE model_info_aliases ADD COLUMN discovered_by TEXT;
ALTER TABLE model_info_aliases ADD COLUMN diagnostics_json TEXT;
```

If migration churn is undesirable, add a companion table:

```sql
CREATE TABLE IF NOT EXISTS model_info_alias_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    provider_id TEXT,
    source TEXT NOT NULL,
    alias TEXT NOT NULL,
    match_method TEXT NOT NULL,
    confidence REAL NOT NULL,
    diagnostics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Whenever a non-exact match is accepted:

1. Persist normal alias row using existing `upsert_alias()`.
2. Persist evidence row or updated alias metadata.
3. Mark `source_diagnostics.<source>.match_method` and `confidence`.

Do not persist ambiguous or rejected candidates as active aliases. Store rejected samples only in diagnostics/logs.

## Phase 5: Integrate into OpenRouter resolver first

Refactor `resolve_openrouter_record()` into either:

- a wrapper over `resolve_source_record_tiered(source="openrouter", ...)`, or
- a source-specific tiered resolver that shares normalization/matching primitives.

Keep current exact-alias behavior as Tier 0.

In `refresh_due_models()`:

- Build OpenRouter candidate index once after `fetch_all()`.
- For each due model, call tiered resolver with provider/display context from provider catalog detail.
- If matched, persist observation and discovered alias if method is non-exact.
- Collect aggregate diagnostics:
  - attempted
  - matched
  - matched_by_method
  - missed_by_reason
  - ambiguous_count
  - candidate_count

In manual `refresh_model_info()`:

- Include full per-model diagnostics:

```json
{
  "source_diagnostics": {
    "openrouter": {
      "fetched": true,
      "catalog_count": 329,
      "match_method": "normalized_exact",
      "confidence": 0.84,
      "matched_source_model_id": "minimax/minimax-m3",
      "local_normalized_keys": ["minimaxm3"],
      "candidate_normalized_key": "minimaxm3",
      "candidates_considered": 4,
      "rejected_candidates": [
        {"source_model_id": "...", "reason": "version_mismatch"}
      ]
    }
  }
}
```

## Phase 6: Improve logging and operator diagnostics

### Periodic refresh logging

Change the app periodic wrapper for model-info refresh so it logs meaningful no-match cycles, not only writes:

Current behavior logs only if `refreshed > 0`.

New behavior:

- log `INFO` when any external source is fetched and attempts were made;
- log `WARNING` if source payload count > 0, attempted > 0, matched == 0 for N consecutive cycles;
- include matched-by-method summary.

Example:

```text
Model info periodic refresh: total=33 refreshed=0 skipped=33 openrouter_attempted=33 openrouter_matched=0 openrouter_missed=33 matched_by_method={} missed_by_reason={normalized_no_match: 33}
```

### Diagnostic endpoint/report

Add a read-only endpoint or extend existing model-info source diagnostics:

```text
GET /api/model-info/diagnostics?limit=100&status=sparse
```

Return compact data for sparse rows:

- model_id
- providers
- status/sparse/next_refresh_at
- aliases by source
- source health summary
- last refresh diagnostics, if persisted
- suggested next action:
  - `configure_alias`
  - `ambiguous_candidates`
  - `source_fetch_failed`
  - `not_due`
  - `matched_but_not_merged`

If adding a new endpoint is too much for the first pass, add a CLI/debug script that reads SQLite and calls manual refresh for a sample model.

## Phase 7: Tests

### 7.1 Normalization unit tests

Add `tests/unit/test_model_info_normalization.py`.

Pin cases:

```text
MiniMax-M3                  == minimaxm3
minimax-m3                  == minimaxm3
MiniMax M3                  == minimaxm3
MiniMax: MiniMax M3         == minimaxm3
minimax/minimax-m3          splits vendor=minimax, normalized=minimaxm3
opencode-go/minimax-m3      strips known provider namespace to minimaxm3
Claude Sonnet 4.5           == claudesonnet45
claude-sonnet-4.5           == claudesonnet45
GPT_5.5-mini                == gpt55mini
```

Also test negative cases:

```text
gpt55 != gpt55mini
v4 != v4pro
deepseekv4 != deepseekv4pro
claudesonnet4 != claudesonnet45
```

### 7.2 Candidate index tests

Add `tests/unit/test_model_info_candidate_index.py`.

Use OpenRouter-like fixture entries and assert:

- `source_model_id` indexed exactly;
- display name indexed;
- normalized keys map to candidates;
- multiple candidates sharing a normalized key remain multiple until resolver tie-breaks.

### 7.3 Tiered resolver tests

Add `tests/unit/test_model_info_tiered_matching.py`.

Required cases:

1. Configured alias wins over normalized match.
2. Exact source ID matches.
3. Normalized exact matches `MiniMax-M3` to `minimax/minimax-m3`.
4. `MiniMax: MiniMax M3` duplicate vendor display name matches `minimax-m3`.
5. Provider namespace `opencode-go/minimax-m3` is not treated as OpenRouter vendor namespace, but the stripped model key can match.
6. Regex family rule matches only when variant/version tokens are safe.
7. Similarity match accepted only with high score and score gap.
8. Similarity match rejected when `mini/pro/flash/lite` variant tokens differ.
9. Multiple plausible candidates produce no match with `ambiguous_candidates`.
10. Accepted non-exact match persists alias evidence.

### 7.4 Fresh DB integration tests

Add a test that reproduces the live failure:

```text
fresh DB
catalog row: provider_id=opencode-go, model_id=minimax-m3
OpenRouter fixture: source_model_id=minimax/minimax-m3, name=MiniMax: MiniMax M3
no configured alias
run refresh_due_models() or refresh_model_info(force=True)
assert row becomes partial
assert OpenRouter observation exists
assert alias/evidence persisted with match_method=normalized_exact or regex_rule
assert dashboard summary would show partial, not sparse
```

Also include a control test:

```text
fresh DB
local model_id=deepseek-v4
OpenRouter contains deepseek-v4 and deepseek-v4-pro
resolver must not bind to pro if local is non-pro
```

### 7.5 Outbound OpenRouter contract tests

Add focused tests for `OpenRouterModelInfoSource` that verify the request itself:

- URL is exactly `{base_url.rstrip('/')}/models`.
- `User-Agent` header is set.
- `Authorization: Bearer <key>` is set when `resolved_api_key` exists.
- no Authorization header when no key is configured.
- non-2xx response raises `ModelInfoSourceFetchError`.
- invalid JSON raises `ModelInfoSourceFetchError`.
- payload count is reflected in source-health through service tests.

Current tests use mock HTTP responses but do not assert the exact request contract strongly enough. Add a recording fake client that stores `url` and `headers` per call.

### 7.6 Fixture and optional live tests

Do not require live network in normal CI. Add checked-in fixtures:

```text
tests/fixtures/model_info/openrouter_models_sample.json
tests/fixtures/model_info/provider_catalog_sample_opencode_go.json
```

Add an optional live test file gated by env var:

```python
pytestmark = pytest.mark.skipif(
    os.getenv("EGGPOOL_LIVE_MODEL_INFO_TESTS") != "1",
    reason="live model-info tests disabled",
)
```

Live tests should verify:

- OpenRouter catalog fetch succeeds.
- Known model IDs still exist or produce a clear failure requiring fixture update.
- The tiered resolver can resolve a small allowlist of current models.

Keep the allowlist short and stable:

```text
minimax-m3 -> minimax/minimax-m3
claude-sonnet-* -> anthropic/... if present in fixtures/live allowlist
gemini-* -> google/... if present
```

## Phase 8: Config surface

Add optional config for conservative matching:

```toml
[model_info.matching]
enabled = true
normalized_exact = true
regex_rules = true
similarity = false
similarity_threshold = 0.92
similarity_min_gap = 0.05
persist_discovered_aliases = true
max_candidates_per_model = 20

[[model_info.matching.provider_namespace_aliases]]
provider_id = "opencode-go"
strip_provider_namespace = true

[[model_info.matching.source_vendor_aliases]]
source = "openrouter"
local_vendor = "minimax"
source_vendor = "minimax"
```

Defaults:

- `enabled = true`
- `normalized_exact = true`
- `regex_rules = true` for safe built-ins
- `similarity = false` initially, or enabled only for manual diagnostics
- `persist_discovered_aliases = true` for normalized/regex matches, false for similarity until proven safe

If config churn is undesirable, implement internal defaults first and add config only for similarity toggles and thresholds.

## Phase 9: Dashboard/API presentation

Once matching works, dashboard should naturally move models from `sparse` to `partial`. Still, add small detail fields where useful:

- `match_method`
- `match_confidence`
- `external_ids.openrouter`
- source diagnostics in detail view only

Do not clutter the `/models` table. The table can stay status/pill based.

## Manual verification

After implementation, run on a fresh DB:

```bash
BASE="${EGGPOOL_BASE_URL:-http://127.0.0.1:8000}"

curl -sS "$BASE/api/model-info" | python3 -m json.tool | head -120

curl -sS -X POST "$BASE/api/model-info/refresh?model_id=minimax-m3&force=1" \
  | python3 -m json.tool

curl -sS "$BASE/api/model-info/minimax-m3" | python3 -m json.tool
```

Expected for `minimax-m3` without hand-configured alias:

```text
sources includes provider_catalog and openrouter
status is partial or better
sparse is false
external_ids.openrouter = minimax/minimax-m3
source_diagnostics.openrouter.match_method is normalized_exact or regex_rule
```

SQLite checks:

```bash
sqlite3 usage.sqlite3 <<'SQL'
.headers on
.mode column

SELECT model_id, status, sparse,
       json_extract(provenance_json, '$.sources') AS sources,
       json_extract(detail_json, '$.external_ids.openrouter') AS openrouter_id,
       json_extract(detail_json, '$.display_name') AS display_name
FROM model_info_canonical
ORDER BY model_id
LIMIT 50;

SELECT model_id, provider_id, source, alias, confidence, active
FROM model_info_aliases
ORDER BY model_id, source, alias
LIMIT 200;

SELECT source, model_id, source_model_id, provider_id, confidence, observed_at
FROM model_info_observations
ORDER BY observed_at DESC
LIMIT 100;

SELECT source, enabled, last_success_at, last_error_at, failure_count, last_payload_count
FROM model_info_source_health
ORDER BY source;
SQL
```

Dashboard check:

```bash
curl -sS "$BASE/models" \
  | grep -Eo 'pill-(fresh|partial|sparse|stale|conflict|unmatched|unknown)' \
  | sort | uniq -c
```

Expected: at least known externally matched rows should be `pill-partial` or better rather than all `pill-sparse`.

## Acceptance criteria

This line of work is complete when:

1. A fresh DB with `provider_id=opencode-go`, `model_id=minimax-m3`, and OpenRouter fixture `minimax/minimax-m3` enriches without hand-configured alias.
2. Matching is tiered and auditable: exact alias, exact source ID, normalized exact, regex rule, and guarded similarity all have distinct diagnostics.
3. Ambiguous model-family variants do not auto-bind.
4. Non-exact accepted matches persist alias evidence or alias metadata with method/confidence.
5. Periodic refresh logs no-match cycles with aggregate attempted/matched/missed counts and reasons.
6. OpenRouter outbound request contract is tested for URL, headers, auth, error handling, and parsing.
7. Fixture tests based on real source/catalog shapes pass in normal CI.
8. Optional live tests can validate current OpenRouter data when `EGGPOOL_LIVE_MODEL_INFO_TESTS=1`.
9. Dashboard `/models` no longer shows every available model as `sparse` when external metadata is available and resolvable.

## Suggested commit sequence

1. `Add model-info normalization primitives`
2. `Add model-info candidate indexes`
3. `Add tiered OpenRouter identity matching`
4. `Persist discovered model-info alias evidence`
5. `Add model-info matching diagnostics and logging`
6. `Add fixture and outbound contract tests for model-info sources`
7. `Document model-info matching behavior`

## Suggested final commit message

```text
Add tiered model-info identity matching for provider naming drift
```

## Notes for implementer

Keep the first implementation conservative. Normalized exact plus a few regex rules should fix the current `minimax-m3` failure without needing fuzzy scoring. Add similarity matching behind a disabled-by-default or diagnostics-only flag if risk is high. The main objective is to stop treating aggregator provider IDs as source vendor namespaces and to make every match or miss explainable.
