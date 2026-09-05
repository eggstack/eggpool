# W002 Closure — Canonical IR, Request Admission, Limits, and M5 Fact Bridge

Status: closed

Implementation commit: [`2096727b`](https://github.com/eggstack/eggpool/commit/2096727b)

Plan: [W002 — canonical IR, request admission, limits, and M5 fact bridge](../../implementation/canonical-wire/002-canonical-ir-request-admission-and-limits.md)

## Outcome

W002 establishes the pure Rust request boundary under `rust/src/request/` and
the source-owned canonical semantic types under `rust/src/wire/ir.rs`. Normal
admission checks the configured raw-body ceiling before parsing, parses JSON
once, validates bounded structure and supported content, records only body
length plus deterministic estimates, and returns no retained raw request body.

The canonical IR preserves client surface, system/developer/user/assistant/tool
roles, ordered text/media/document/audio/reasoning/tool blocks, tool IDs and
results, tools and choice, structured output, generation controls, cache/body
metadata, and omitted/null/false/zero presence. Reasoning effort, fixed
budgets, adaptive/toggle intent, and explicit disable remain distinct until a
later selected-profile codec chooses a target representation.

Canonical response, usage, provider-error, and finite stream-event types are
defined without provider submission, retry, or lifecycle ownership. Compact
JSON body encoding is deterministic and bounded by an explicit caller limit.
Base64 size checks use conservative arithmetic before any decode allocation;
invalid base64 remains a typed admission failure.

Pure adapters produce M5 `RoutingRequestFacts` from caller-supplied static
facts and D007 affinity input from a validated explicit session or bounded
conversation prefix. They do not access catalog/database state, select or
claim accounts, mutate health/quota/fairness, or invoke a selector.

## Fixture and requirement coverage

`rust/tests/canonical_request.rs` covers:

- minimal, rich Chat, Responses, and Messages request admission;
- malformed JSON, non-object top level, model, role, collection, and body-limit
  rejection;
- omitted/null/false/zero presence distinctions;
- role, tool, tool-result, image/data-URI, reasoning, structured-control, and
  Unicode preservation;
- explicit disable versus fixed-budget reasoning;
- compact body encoding and independent canonical source encoding;
- bounded token estimates, overflow-safe base64 sizing, strict base64 shape;
- redacted canonical/affinity debug output;
- pure M5 routing and D007 affinity bridging.

The W001 Python oracle remains unchanged. Its focused canonical test passes with
15 tests, and the targeted Python request/limits/IR suite passes with 88 tests.
The Rust migration candidate has 116 passing tests across all targets,
including 10 W002-specific tests.

## Security and resource audit

- Raw body bytes are used only for the bounded admission/estimate call; the
  admitted value retains `raw_body_bytes`, not the original buffer.
- JSON nesting is capped at 64 levels; messages, content blocks, tools, and
  metadata have explicit collection bounds; estimates use saturating arithmetic.
- Debug implementations expose structural counts/lengths, never message text,
  inline media, provider-error text, or automatic-affinity conversation text.
  No credential or session value is part of the new types.
- No async tasks, database/network calls, provider auth, retry behavior, or
  unsafe code were added. No new runtime dependency was introduced; the
  existing `serde_json` dependency now enables insertion-order preservation for
  compact exposed JSON.

## Verification

Passed:

```text
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test canonical_request -- --test-threads=1  # 10 passed
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1            # 116 passed
uv run pytest tests/migration_rs/test_w001_canonical_wire.py -q --tb=short --maxfail=1    # 15 passed
uv run pytest tests/unit/test_wire_ir.py tests/unit/test_wire_codecs.py tests/unit/test_limits.py tests/unit/test_parsed_payload.py tests/integration/test_request_limits.py -q --tb=short --maxfail=1  # 88 passed
git diff --check
```

The full `tests/migration_rs` diagnostic was also attempted. It consistently
reached the pre-existing `tests/migration_rs/test_harness.py::test_stub_http_drains_without_persisting_request_body`
test and exceeded the local 30-second command window without reporting a
failure. The W001 canonical oracle and all W002-targeted Python tests passed;
no migration-harness failure is attributed to W002.

## Registry transition and future-plan audit

W002 is removed from the dependency-ready table and added to the completed
table in `migration-rs/registry.md` with this accepted closure record.
W003 is promoted to the sole dependency-ready M6 plan because its hard
dependency, W002, is now closed. W004-W010 remain blocked by the serial
predecessors stated in the canonical-wire README and handoff sequence. No M7
implementation plan is unblocked: M7 remains behind W010 accepted closure.
