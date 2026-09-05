# W012 Closure — Cross-Surface Differential Requalification and M6 Re-Closure

Status: closed

Implementation commit: [`1e0bb712`](https://github.com/eggstack/eggpool/commit/1e0bb712e9e45cc529d872dc73682a3742b6583b)

Plan: [W012 — cross-surface differential requalification and M6 re-closure](../../implementation/canonical-wire/012-cross-surface-differential-requalification-and-m6-reclosure.md)

Baseline: `fb36054278817de63b5c516c82202184c9200be7` (planning baseline)

Contract and oracle: [M6 canonical wire contract](../../canonical-wire-contract.md),
the committed W001 observations, the committed W011 UTF-8 observations, the
Python production wire IR/codecs/SSE observer, and the Rust `WireRuntime`.

## Outcome

W012 repairs the under-asserted W010 differential qualification and re-closes
aggregate M6. The committed Python oracle now records complete bounded request,
finite-response, and stream/client observations. Rust compares all 15 client
surface/profile pairings in each matrix, while preserving the M6 boundary:
selected-profile transformation only, with no provider dispatch, retry,
negotiation, durable attempt, or finalization behavior.

The stronger matrix exposed and the implementation corrected bounded parity
gaps in tool-result identity, Responses ordering/format handling, multimodal
data URLs, Gemini reasoning/tool identifiers, Anthropic response/stream
grammar, and usage/cache counters. Explicit zero output-token presence and
typed malformed/provider-error/premature-EOF outcomes are also covered.

## Requirement-to-evidence matrix

| Requirement | Evidence | Result |
|---|---|---|
| Full Python-derived request matrix | `rust/tests/wire_qualification.rs::w012_compares_all_fifteen_request_transformations_to_python`; `w012-cross-surface-observations.json` | Pass |
| Full Python-derived finite response matrix | `rust/tests/wire_qualification.rs::w012_compares_all_fifteen_finite_transformations_to_python` | Pass |
| Full Python-derived stream/client matrix | `rust/tests/wire_qualification.rs::w012_compares_all_fifteen_stream_transformations_and_fragmentation` | Pass |
| Strict/warn loss policy and ordered notices remain explicit | W012 `loss_cases` snapshot plus existing W006 Rust adaptation and Python transcoder contract coverage | Pass |
| W011 invalid/truncated UTF-8 EOF regressions remain qualified | `w012_keeps_w011_invalid_and_truncated_utf8_regressions_in_the_qualification_set`; committed W011 fixture | Pass |
| Exact/native and semantic comparison rules are enforced | Native selected-surface bodies compare exact bytes; cross-surface JSON compares recursively with unordered object keys and ordered arrays; stream events/frames compare ordered projections | Pass |
| Presence and typed negative paths cannot become false success | `w012_covers_presence_and_typed_negative_paths`; explicit zero/null fixture and malformed/provider-error/premature-EOF cases | Pass |
| Bounded resource, security, and M6/M7 ownership posture | Static review of changed wire/admission paths and fixture/test boundaries | Pass |

### Request matrix

All cells passed against the Python-derived canonical request and target
encoding. Native cells additionally passed exact raw-byte comparison.

| Client \ Upstream profile | OpenAI Chat | OpenAI Responses | Anthropic Messages | Gemini Interactions | Gemini generateContent |
|---|---:|---:|---:|---:|---:|
| Chat Completions | Pass | Pass | Pass | Pass | Pass |
| Responses | Pass | Pass | Pass | Pass | Pass |
| Messages | Pass | Pass | Pass | Pass | Pass |

### Finite response matrix

All cells passed canonical response comparison and client-body comparison.
Native cells additionally passed exact raw-byte passthrough.

| Upstream profile \ Client | Chat Completions | Responses | Messages |
|---|---:|---:|---:|
| OpenAI Chat | Pass | Pass | Pass |
| OpenAI Responses | Pass | Pass | Pass |
| Anthropic Messages | Pass | Pass | Pass |
| Gemini Interactions | Pass | Pass | Pass |
| Gemini generateContent | Pass | Pass | Pass |

### Stream/client matrix

All cells passed ordered canonical event comparison, client frame grammar,
terminal evidence, usage completion, parser-error count, and one-byte
fragmentation. Native cells use the selected native grammar; cross-surface
cells use the Python-derived client frame projections.

| Upstream profile \ Client | Chat Completions | Responses | Messages |
|---|---:|---:|---:|
| OpenAI Chat | Pass | Pass | Pass |
| OpenAI Responses | Pass | Pass | Pass |
| Anthropic Messages | Pass | Pass | Pass |
| Gemini Interactions | Pass | Pass | Pass |
| Gemini generateContent | Pass | Pass | Pass |

## Differential artifact and regression evidence

`tests/migration_rs/w012_canonical_wire.py` owns the live Python-side snapshot
generation. It exercises the three public client surfaces and five built-in
profiles using synthetic, bounded payloads, and records canonical semantics,
encoded bodies, encoded stream frames, terminal state, presence cases,
negative cases, and warn/reject loss-policy cases. The snapshot is repeatable
and is checked byte-for-byte as canonical sorted JSON by
`tests/migration_rs/test_w012_canonical_wire.py`.

The Rust qualification test consumes the committed snapshot rather than a
coarse metadata projection. Native request and response paths require exact
bytes. Cross-surface request bodies use semantic JSON comparison; finite
client bodies are decoded back through the client codec and compared to the
Python canonical response; stream event and frame projections preserve array
order and compare all mandatory fields. This makes object-key ordering the
only normalized JSON incidental.

W011's committed invalid/truncated UTF-8 fixture remains directly referenced
by W012. The W011 invalid-data-line and truncated-data-line cases, including
the incomplete EOF prefixes, remain in the Rust qualification set and cannot
produce terminal success.

## Resource, dependency, and security review

- No Cargo dependency, lockfile, database schema, provider endpoint, or
  runtime lifecycle contract changed.
- The oracle and Rust qualification use synthetic markers and bounded hex/JSON
  fixture fields. No credentials, authorization headers, sessions, or raw
  secret-bearing diagnostics are committed.
- Request bodies, media, JSON, and SSE carry state remain within the existing
  bounded wire/admission paths. The matrix adds no complete-stream buffer,
  per-event task, network dereference, or provider call.
- No provider HTTP submission, auth injection, account claim/health mutation,
  dynamic wire learning, retry, downstream handoff, timeout/cancellation
  policy, or durable finalization was added. Those remain M7 responsibilities.
- The existing W008 supported difference for oversized-SSE discard-count
  details remains unchanged: Rust fails closed with typed framing evidence and
  does not retain discarded bytes. It does not create false terminal success.

## Verification commands and results

Passed:

```text
rtk cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1  # 12 passed
rtk uv run pytest tests/migration_rs/test_w012_canonical_wire.py -q --tb=short  # 2 passed
rtk uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 74 passed, 3 skipped
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1  # 181 passed (20 suites)
rtk uv run ruff format --check src/ tests/ scripts/  # 726 files already formatted
rtk uv run ruff check src/ tests/ scripts/  # all checks passed
rtk uv run pyright src/ scripts/  # 0 errors, 0 warnings, 0 informations
rtk uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
rtk git diff --check  # passed
```

No live provider, paid inference, credential, database, or external network
call was used. The verification is intentionally scoped to M6's deterministic
non-dispatch boundary.

## Unresolved findings and supported differences

No unresolved high- or medium-severity M6 correctness or security finding
remains. The bounded parity mismatches found by the stronger W012 matrix are
corrected in implementation commit `1e0bb712` and covered by the committed
oracle and Rust qualification suite.

The W008 oversized-SSE discard-count detail remains the sole documented
supported difference. It is fail-closed, bounded, and unchanged by W012; no
new ADR is required. No other supported cross-surface difference remains.

## Registry transition and future-plan audit

W012 moves from the dependency-ready table to the completed table in
`migration-rs/registry.md`. Its plan header, canonical-wire implementation
index, handoff sequence, subsystem roadmap, and long-term roadmap now record
accepted closure. Aggregate M6 is closed after the W011/W012 corrective pass;
historical W010 remains append-only evidence.

M7 is eligible for its own planning review now that M6's hard prerequisite is
closed. There is no M7 implementation plan to promote, and this closure does
not authorize implementation. M8-M12 remain sequenced behind M7 planning and
their own dependency reviews. No later plan is automatically unblocked.

Recommendation: accept W012 closure; begin the independent M7 planning review.
