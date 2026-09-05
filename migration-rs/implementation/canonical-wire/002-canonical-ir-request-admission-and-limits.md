# W002 — Canonical IR, Request Admission, Limits, and M5 Fact Bridge

Status: planned; blocked on W001 closure

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md#w002--canonical-ir-request-admission-limits-and-m5-fact-bridge`

Primary class: capability/invariant

Hard dependency: W001 accepted closure.

## 1. Objective

Establish the Rust canonical semantic boundary used by every later codec. Port bounded request admission, core canonical request/response/event types, deterministic request/media/document/token estimates, and pure adapters to the already-closed M5 routing and model-router affinity DTOs.

W002 must not know how to submit an upstream request or choose an account/wire profile.

## 2. Python oracle

Primary behavior sources:

- `request.parsed_payload`;
- `request.body`;
- `request.limits`;
- `wire.ir`;
- request-facing canonical helpers used by current codecs;
- D006/D007/D009 M5 DTO contracts.

Use W001 fixture observations as the acceptance oracle rather than copying implementation details mechanically.

## 3. Admission boundary

Implement a typed bounded admission function that accepts raw request bytes plus client surface/context facts and returns either a canonical admitted request or a typed local rejection.

Required behavior:

- enforce raw body byte limit before expensive parsing where current server semantics require it;
- parse JSON exactly once in the normal path;
- reject malformed JSON and unsupported top-level shapes deterministically;
- preserve distinction between missing fields and explicit null/false/zero where downstream semantics use that distinction;
- extract/validate the client model identifier without provider dispatch;
- retain the original raw byte length as an accounting fact, but do not copy raw bytes into long-lived routing/diagnostic state;
- avoid recursive/adversarial structures causing unbounded stack/memory behavior; enforce the existing practical content/collection limits represented by the Python contract.

Do not add a general JSON-schema validator dependency.

## 4. Canonical request IR

Define Rust types sufficient for all supported surfaces. The exact names may differ, but the semantic model must cover:

- model/source surface and streaming intent;
- ordered messages/turns with role/source distinctions;
- text content;
- image/media/document references or inline payload descriptors;
- system/developer instructions without collapsing distinctions that matter to a target codec;
- tools/functions and their JSON-schema-like parameter definitions;
- tool calls/results and stable IDs;
- tool-choice/parallel-tool intent;
- reasoning/thinking intent, explicit disable, effort/budget/toggle, and historical reasoning content where supported;
- structured response/JSON-schema intent;
- generation controls that are portable or need explicit provider adaptation;
- cache-control/body metadata that carries client semantics;
- provider/client extension values only where EggPool intentionally preserves them.

Unknown fields must follow W001's frozen preserve/drop/reject policy. Do not store arbitrary input objects merely to avoid making a decision.

## 5. Canonical response and event foundation

Define the base finite-response and streaming-event types needed by W004-W008:

- response identity/model metadata;
- text/content parts;
- tool calls/results;
- reasoning content/metadata;
- finish/stop cause;
- canonical usage;
- canonical provider error evidence;
- stream start/delta/content-block/tool/reasoning/usage/finish/error/terminal event categories as frozen by W001.

W002 need not implement provider-family decoders yet; it freezes the typed target they will populate.

## 6. Source intent and loss safety

The IR must preserve enough source intent to answer later whether a conversion is exact, warned, or rejected. Required examples include:

- omitted reasoning control vs explicit reasoning disable;
- system vs developer role where the client surface distinguishes them;
- tool-call IDs and tool-result linkage;
- raw-vs-URL media source form when target capabilities differ;
- explicit response-format/schema constraints;
- requested streaming/non-streaming behavior.

Do not preemptively collapse these into generic strings/maps if doing so would make W006 unable to diagnose loss.

## 7. Deterministic limits and estimates

Port pure request-sizing behavior from `request.limits`, including the current compatibility contract for:

- raw body bytes;
- message/text contribution;
- media/image/document count and decoded/declared byte bounds;
- data URI/base64 length calculations without decoding unbounded content;
- document limits;
- context-limit estimate where current Python enforces one locally;
- reservation/projected token estimate used by routing before dispatch.

Use saturating/checked integer arithmetic. Invalid lengths/non-finite numeric controls must fail typed validation rather than wrap.

These estimates are routing/admission facts, not billing truth.

## 8. Deterministic JSON body encoding

Port the pure `request.body` output semantics required by later codecs:

- stable compact JSON encoding class;
- byte output and uncompressed length;
- compression decision/threshold/minimum-saving policy if this policy is part of the current canonical body preparation contract;
- bounded maximum encoded output.

Do not attach provider auth headers or send the body. If compression is actually owned at the provider transport boundary in current Rust architecture, expose only the pure decision/encoded representation needed by M7 and avoid duplicating transport behavior.

## 9. M5 routing-fact bridge

Add a pure adapter from an admitted canonical request plus caller-supplied static provider/profile feasibility facts to M5 `RoutingRequestFacts`.

It must map, where available:

- canonical/provider-qualified model identity using the existing M5 parser contract;
- client request surface;
- client protocol;
- requested/feasible upstream protocol facts supplied by the caller rather than guessed from JSON;
- projected tokens;
- canonical thinking requirement and capability policy input;
- stable current-time/freshness facts supplied explicitly by caller/test.

It must not:

- query the catalog/database;
- call `select_and_claim`;
- mutate fairness/health/quota;
- choose a provider or wire profile.

## 10. D007 affinity identity bridge

Create the pure canonical input needed for D007 session affinity:

- validate/hash explicit bounded session identity only through the existing M5 helper where applicable;
- derive the canonical conversation-prefix representation from system/developer and first-user text according to the closed D007 contract;
- never retain raw session values in M5 affinity state;
- avoid including images/documents/tool payloads in automatic affinity unless the D007 oracle explicitly does so.

Do not mutate the affinity cache or call a semantic model-router selector.

## 11. Errors

Use a small typed error enum with stable semantic reason codes for at least:

- invalid JSON/top-level request;
- missing/invalid model;
- invalid role/content/tool structure;
- invalid reasoning/structured-output control;
- raw body/media/document/context limit;
- integer/length overflow;
- unsupported canonical content form.

Keep user-facing HTTP status mapping out of W002; M7/inbound handlers will map typed errors to responses according to the public contract.

## 12. Resource/security requirements

- one normal-path JSON parse;
- no unbounded recursion/collection growth under M6-owned input handling;
- no raw request body in `Debug` for canonical types;
- custom/redacted `Debug` where inline media or sensitive client content could otherwise be dumped;
- no API/proxy credentials in any type;
- no spawned tasks or background work;
- no new database/network dependency;
- preserve `unsafe_code = "forbid"`.

## 13. Required tests

Use W001 observations to cover:

1. admission parity for valid/minimal/all major request shapes;
2. malformed JSON/top-level/model failures;
3. omitted/null/zero/false distinctions;
4. system/developer/user/tool role preservation;
5. reasoning and structured-output source intent;
6. media/document descriptor and boundary accounting;
7. saturating/overflow-safe limits;
8. reservation-token estimate parity;
9. deterministic canonical serialization/fixture snapshot;
10. canonical request -> M5 `RoutingRequestFacts` parity;
11. canonical conversation -> D007 affinity identity input parity;
12. debug/serialization audit proving no raw secret sentinel/session identifier leaks;
13. no mutation of M5 fairness/claim/health state during bridge calls.

## 14. Verification

Run:

```text
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <targeted Python request/limits/IR tests> -q --tb=short --maxfail=1
git diff --check
```

No live provider inference.

## 15. Acceptance criteria

W002 closes only if:

- every W001 request fixture maps to the expected canonical result or typed rejection;
- the IR can represent all semantics needed by all four codec families without opaque raw-body escape hatches;
- request/media/document/token limits match the oracle and are overflow-safe;
- M5 routing/affinity bridges are pure and parity-equivalent;
- no codec/provider transport/retry behavior has leaked into admission;
- W003 can depend on stable canonical and error types.

## 16. Stop conditions

Do not close if later codecs would need to recover semantics that W002 discarded, arbitrary unknown JSON is stored wholesale as a workaround, request parsing can allocate without bound, M5 routing is invoked from admission, or W002 begins mapping upstream failures/retries.

## 17. Closure evidence

Create `migration-rs/closure/canonical-wire/002-status.md` with implementation commit, IR/limit fixture coverage, security/resource audit, verification commands, and registry transition promoting W003.
