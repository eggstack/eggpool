# W001 Closure — Canonical Wire Contract and Deterministic Fixture Freeze

Status: closed

Recommendation: closed; W002 is dependency-ready.

Implementation commit: [`52f1dfac`](https://github.com/eggstack/eggpool/commit/52f1dfac)

Plan: [W001 — canonical wire contract and deterministic fixture freeze](../../implementation/canonical-wire/001-contract-and-fixture-freeze.md)

Contract: [M6 canonical wire contract](../../canonical-wire-contract.md)

## Outcome

W001 freezes the Python-owned M6 semantic boundary before Rust codec work. The
oracle is grounded in the current parsed-payload/body/limits, canonical IR,
closed wire registry and built-in codecs, SSE framing/observation, normalized
usage, transcoder error/loss, and static profile modules. It does not import
wire negotiation state, call the catalog/coordinator, make provider requests,
or require live credentials.

The fixture package contains a profile matrix, SSE byte-fixture inventory, and
committed semantic observation projection. The executable oracle additionally
builds rich request encodings, finite response observations, all-single-byte
SSE split observations, terminal/EOF decisions, usage distinctions, typed
provider-error evidence, malformed request cases, and bounded limit facts at
test time. Synthetic text and media are small; raw headers, secrets, session
identities, timestamps, UUIDs, process IDs, and personal content are absent.

## Requirement-to-evidence matrix

| W001 requirement | Evidence | Result |
|---|---|---|
| Contract artifact and parity rules | `migration-rs/canonical-wire-contract.md` defines surfaces, profile identities, exact/semantic JSON rules, omitted/null/zero behavior, canonical source ownership, typed outcomes, usage vocabulary, terminal evidence, limits, M5 bridge, and M7 exclusions. | Pass |
| Four required wire representatives and built-ins | Matrix and registry test cover OpenAI Chat, OpenAI Responses, Anthropic Messages, Gemini generateContent, plus the accepted Gemini Interactions profile. All five `_wire_profiles.toml` entries are asserted. | Pass |
| Request matrix | Rich synthetic Chat, Responses, and Messages requests exercise ordered roles, content parts, tools/IDs/results, reasoning, schema, media, cache metadata, stream intent, Unicode, presence distinctions, invalid shapes, and body/context/reservation sizing. | Pass |
| Response matrix | Every built-in profile has finite text, reasoning/tool where representable, finish status, usage, encoded response, and typed provider-error observations; malformed/unknown usage shapes are covered. | Pass |
| SSE grammar and split invariance | LF/CRLF fixtures cover event/id/comments/ignored fields/multiline data/blank records, and the oracle feeds every small profile fixture one byte at a time. Canonical event sequences are equal across split and line-ending variants. | Pass |
| Terminal evidence | Chat `[DONE]`, Responses completed/incomplete/failed vocabulary, Anthropic `message_stop`, and Gemini completion/incomplete status are documented and observed. EOF without terminal evidence is explicitly false; oversized unterminated carry is discarded. | Pass |
| Usage semantics | OpenAI and Anthropic cache reads/writes, explicit zero, missing fields, missing usage, and unknown shape retain `None` versus `0` and cache status. | Pass |
| Stable loss/error taxonomy | Fixture-level exact/native, warning, approved-loss, rejected-loss, malformed, limit, unsupported-profile, and incomplete-terminal codes are frozen alongside the existing `LOSS_WARNING_KINDS`. | Pass |
| Resource/security posture | 10 MiB request, 64 KiB SSE, 128,000 reservation, and affinity bounds are recorded. Output scans reject API/proxy/session sentinels; no raw personal fixture body is emitted. | Pass |
| M7 boundary | Contract and tests exclude learned preference, rejected candidates, alternate retry, provider submission, downstream handoff, timeout/cancellation policy, failure effects, and durable finalization. | Pass |
| Existing migration observations | The full `tests/migration_rs` suite remains green; no F002/M4/M5 fixture was changed. | Pass |

## Generated fixture inventory

| Artifact | Coverage |
|---|---|
| `w001-fixture-matrix.json` | 3 public client surfaces, 5 upstream profiles, request/response/SSE case families, 12 stable outcome codes, resource limits, M7 boundary, and secret markers. |
| `w001-sse-fixture-inventory.json` | 5 profile streams, 15 total native records, LF/CRLF and all-single-byte split requirements, grammar probes, terminal vocabulary, and EOF cases. |
| `w001-python-observations.json` | 5 profile inventory entries, 3 request observations, 5 finite response observations, 5 stream observations, 6 usage observations, and 6 limit facts. |
| `canonical_wire_fixtures.py` | Deterministic runtime generation of the bounded richer request/response/event observations from production modules. |
| `test_w001_canonical_wire.py` | 15 focused assertions, including a profile-parameterized terminal test. |

## Verification commands actually run

```text
uv run pytest tests/migration_rs/test_w001_canonical_wire.py -q --tb=short --maxfail=1  # 15 passed
uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 52 passed, 21 skipped
uv run pytest tests/unit/test_wire_profiles.py tests/unit/test_wire_resolver.py tests/unit/test_transcoder/test_errors_parse.py tests/unit/test_sse_observer.py tests/unit/test_normalized_usage.py -q --tb=short --maxfail=1  # 98 passed
uv run ruff format --check src/ tests/ scripts/  # 723 files already formatted
uv run ruff check src/ tests/ scripts/  # passed
uv run pyright src/ scripts/  # 0 errors, 0 warnings, 0 informations
uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
git diff --check  # passed
```

The migration suite was rerun serially after one parallel run encountered a
fixture listener startup timeout; the serial run passed. No Rust files or
dependencies were changed, so Rust formatting/clippy/tests were not required
by W001's conditional verification step.

## Unresolved ambiguities and accepted differences

No mandatory stop condition remains. The contract records current Python
compatibility behavior rather than silently cleaning it up: OpenAI missing or
zero totals may be reconstructed, Anthropic totals include cache components,
Anthropic stream cache creation mirrors the legacy write counter, and the
current stream adapter can expose both a finish-derived and native terminal
event. Later Rust plans must either preserve these observations or introduce a
new explicitly reviewed contract/corrective plan.

The public canonical client vocabulary has three surfaces while the accepted
upstream registry has five profiles. Gemini generateContent is the required
fourth wire-family representative; Gemini Interactions is an additional
built-in profile. This distinction is intentional and documented in the
contract rather than represented as a nonexistent public endpoint.

## Registry transition and future-plan audit

W001 is removed from the dependency-ready section of `migration-rs/registry.md`
and added to the completed table with implementation commit `52f1dfac` and
this closure record. M6 remains active, now at W002. W002 is the sole plan
moved to **ready for handoff** because its only hard dependency, W001, has
accepted closure evidence. W003-W010 remain blocked by their stated serial
predecessors; no later M6 plan can be safely unblocked by W001 alone. M7
implementation handoff, M8-M12, and runtime lifecycle work remain sequenced
behind their roadmap dependencies.

The exact synchronized transition is recorded in:

- `migration-rs/registry.md`;
- `migration-rs/subsystems/canonical-wire-roadmap.md`;
- `migration-rs/implementation/canonical-wire/README.md`;
- `migration-rs/implementation/canonical-wire/000-handoff-sequence.md`; and
- W001/W002 implementation-plan status lines.

Unresolved mandatory findings: none.
