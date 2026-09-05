# W001 — Canonical Wire Contract and Deterministic Fixture Freeze

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md#w001--contract-and-deterministic-fixture-freeze`

Primary class: invariant/infrastructure

Hard dependencies: F001-F006, M4 T001-T006, and M5 D001-D009 are closed.

## 1. Objective

Freeze M6's behavioral contract before adding Rust request/codec/SSE behavior. Build deterministic Python observations for the supported request, response, transformation, streaming, usage, warning/loss, and error semantics so later Rust plans have an oracle stronger than handwritten expectations.

This plan should primarily add fixtures, Python observation adapters, migration tests, and a canonical M6 contract document. Do not begin a broad Rust codec implementation here.

## 2. Python oracle ownership

The observation harness must be grounded in current production modules, especially:

- `src/eggpool/request/parsed_payload.py`;
- `src/eggpool/request/body.py`;
- `src/eggpool/request/limits.py`;
- `src/eggpool/wire/types.py`;
- `src/eggpool/wire/ir.py`;
- `src/eggpool/wire/registry.py`;
- `src/eggpool/wire/codecs/base.py` and built-in codecs;
- `src/eggpool/transcoder/` compatibility behavior where it remains externally observable;
- `src/eggpool/proxy/sse.py`;
- `src/eggpool/proxy/normalized_usage.py`;
- `src/eggpool/providers/_wire_profiles.toml`.

Do not use `wire.resolver` runtime learning/negotiation as an M6 oracle except to document the explicit M7 boundary.

## 3. Canonical contract artifact

Create `migration-rs/canonical-wire-contract.md` containing at minimum:

- supported public client surfaces;
- supported upstream wire profile identities/families;
- exact-vs-semantic parity rules for JSON ordering, floating representation, omitted/null fields, warning ordering, and error text;
- canonical IR ownership and source-intent requirements;
- typed loss/warning/rejection classes;
- request/media/document/SSE resource ceilings that are externally meaningful;
- usage/cache counter vocabulary;
- terminal stream evidence vocabulary;
- M5 DTO bridge ownership;
- M7 boundary for negotiation/retry/submission/finalization.

If Python currently has ambiguous or inconsistent behavior, record the ambiguity as a fixture/decision rather than silently choosing a cleaner Rust behavior.

## 4. Supported surface/profile matrix

Freeze at least:

- OpenAI Chat Completions;
- OpenAI Responses;
- Anthropic Messages;
- Gemini generateContent;
- every built-in static profile entry in `_wire_profiles.toml` that is currently accepted by production configuration.

Observation output should identify client surface, selected upstream profile, provider kind/profile metadata when relevant, transformation result class, and warning/loss/error class without including credentials.

## 5. Request fixture matrix

Include representative and boundary cases for:

- minimal model-only/message requests;
- system/developer/user/assistant/tool roles;
- omitted vs null vs false vs zero values where semantics differ;
- Unicode and escaped content;
- multiple messages and content-part arrays;
- tools, tool choice, parallel tool calls, tool result IDs;
- reasoning effort/toggle/budget/history fields;
- structured-output/response-format schemas;
- image/document/media inputs in every supported representation;
- cache-control/provider extension fields that EggPool preserves or intentionally rewrites;
- streaming flag and relevant request metadata;
- malformed JSON, wrong top-level type, missing/invalid model, unsupported content shapes;
- body, media, document, and context-limit edges.

Do not snapshot raw sensitive headers or arbitrary user bodies. Fixture content must be synthetic.

## 6. Response fixture matrix

Cover finite success and finite error payloads for all supported wire families:

- simple text;
- multiple content parts;
- tool calls/results where a client-facing surface can represent them;
- reasoning content/metadata;
- finish/stop reasons;
- structured output;
- usage/cache counters;
- provider error objects and malformed success/error shapes;
- missing usage and unknown usage shapes.

Freeze the semantic canonical observation plus client re-encoded result where Python exposes one.

## 7. Streaming/SSE fixture matrix

Build byte fixtures and a deterministic chunk-split driver. Cover:

- LF and CRLF records;
- `event:`, `id:`, comments, ignored fields, and multiline `data:`;
- blank-line record termination;
- `[DONE]` where applicable;
- empty/comment-only records;
- UTF-8 data split across input chunks if the Python contract supports byte-level buffering;
- provider-specific stream event types;
- text deltas, tool deltas, reasoning deltas, usage-bearing events, finish events;
- explicit provider error event;
- malformed JSON event data;
- clean terminal event followed by EOF;
- EOF before terminal evidence;
- oversized unterminated SSE carry buffer.

For each canonical byte fixture, exercise representative split points and a generated all-single-byte split for small fixtures. The expected semantic event sequence must be independent of chunk boundaries.

## 8. Usage observations

Freeze normalized usage semantics for:

- input/output/total tokens;
- cache read/input/cached tokens;
- cache creation/write tokens where present;
- provider-specific nested usage structures;
- explicit zero vs missing fields;
- unknown-format status;
- streaming merged/final usage;
- missing final usage event.

Do not normalize away distinctions that affect accounting in Python.

## 9. Loss/warning/error taxonomy

Create stable fixture-level reason codes for observable transformation outcomes. At minimum distinguish:

- exact/native conversion;
- compatible conversion with warning;
- policy-approved semantic loss;
- unsupported/loss-rejected conversion;
- malformed client request;
- malformed provider response/event;
- request/media/document/context limit;
- unsupported wire/profile;
- incomplete stream terminal evidence.

Error prose may be semantic-normalized if the existing foundation contract permits it, but error class/reason and affected field are not normalizable differences.

## 10. M7 boundary observations

Document but do not implement these as M6 behavior:

- DB-backed/learned wire preference;
- rejected-wire candidate state;
- alternate-wire retry;
- provider HTTP submission;
- downstream response-start ownership;
- timeout/cancellation outcome policy;
- retry classification/failure effects;
- durable attempt/finalization.

The fixture harness should be callable without any of those systems.

## 11. Harness implementation

Prefer extending the existing `tests/migration_rs` runner rather than introducing a second migration harness. Add compact JSON observation files under a new `migration-rs/fixtures/canonical-wire/` directory. Keep large binary/media fixtures tiny and synthetic; generate repeated/oversized data at test time.

Use deterministic ordering for maps/lists in observations. Do not store environment-specific paths, timestamps, UUIDs, random seeds, credentials, or process IDs unless normalized by the existing oracle framework.

## 12. Required tests

At minimum:

1. Python observation generation is deterministic across repeated runs.
2. Every built-in static wire profile appears in the profile inventory.
3. Every supported client surface has finite request/response fixtures.
4. Every supported surface has streaming/terminal evidence fixtures where streaming exists.
5. Chunk-split observations are invariant.
6. Usage zero/missing/cache distinctions are preserved.
7. Loss/error fixtures use stable semantic codes.
8. Fixture outputs contain no configured API/proxy credentials or synthetic secret sentinel.
9. Existing F002/M4/M5 migration observations remain unchanged/green.

## 13. Verification

Run and record:

```text
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <targeted request/wire/transcoder/sse/usage unit tests> -q --tb=short --maxfail=1
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
git diff --check
```

If Rust fixture-reader scaffolding is touched, also run `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, and the relevant Rust tests.

No live provider call is required.

## 14. Acceptance criteria

W001 closes only if:

- the M6 contract and fixture matrix cover all four supported surfaces and built-in profile identities;
- later Rust codec plans can compare semantic outputs without importing coordinator/network behavior;
- request/response/SSE/usage/loss/error boundaries are explicit;
- chunking and resource-limit cases are deterministic;
- dynamic wire negotiation is clearly assigned to M7;
- no secrets/raw personal content enter observations;
- the registry can safely promote W002 as the sole dependency-ready plan.

## 15. Stop conditions

Do not close if a major supported wire surface lacks oracle coverage, SSE completion is represented only by raw bytes without semantic terminal evidence, fixture normalization could hide model/tool/reasoning/media loss, or the harness requires live provider inference to determine correctness.

## 16. Closure evidence

Create `migration-rs/closure/canonical-wire/001-status.md` naming the implementation commit, generated fixture inventory/counts, verification commands actually run, unresolved ambiguities, and the exact registry transition that promotes W002.
