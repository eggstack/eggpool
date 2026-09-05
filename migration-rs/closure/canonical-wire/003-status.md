# W003 Closure — Static Wire-Profile Registry and Codec Contract

Status: closed

Implementation commit: [`f0ab286`](https://github.com/eggstack/eggpool/commit/f0ab286)

Plan: [W003 — static wire-profile registry and codec contract](../../implementation/canonical-wire/003-wire-profile-registry-and-codec-contract.md)

## Outcome

W003 adds the immutable Rust static wire-profile boundary under
`rust/src/wire/`. The registry includes the packaged Python-oracle
`src/eggpool/providers/_wire_profiles.toml` at build time and parses it with
strict Serde/TOML structures. Profile names and codec identifiers are closed
Rust enums, so TOML cannot import code, select arbitrary implementations, or
carry account credentials.

The registry covers all five W001 identities:

| Profile | Request codec | Response codec | Stream codec | Family |
|---|---|---|---|---|
| `openai_chat_completions` | `openai_chat` | `openai_chat` | `openai_chat_sse` | OpenAI Chat |
| `openai_responses` | `openai_responses` | `openai_responses` | `openai_responses_sse` | OpenAI Responses |
| `anthropic_messages` | `anthropic_messages` | `anthropic_messages` | `anthropic_messages_sse` | Anthropic Messages |
| `gemini_interactions` | `gemini_interactions` | `gemini_interactions` | `gemini_interactions_sse` | Gemini Interactions |
| `gemini_generate_content` | `gemini_generate_content` | `gemini_generate_content` | `gemini_generate_content_sse` | Gemini generateContent |

Provider-configured path templates, streaming paths, and priorities are joined
to these definitions as non-secret `ConfiguredWireProfile` values. Profiles
are sorted by priority and surface identity, independent of map insertion
order. Provider and model-wire references are validated against the same
registry during Rust configuration validation; legacy path synthesis remains
unchanged.

## Codec contract

`WireCodec` is a pure interface for later W004-W009 implementations. It
accepts typed canonical IR/profile values and JSON values, and exposes request
decode/encode, finite response decode/encode, and stream-event hooks. It has no
HTTP client, account credential, retry, preference, or persistence dependency.
`CodecReasonCode`, `CodecError`, `CodecOutput`, and `AdaptationNotice` provide
stable typed error/notice metadata. A valid provider error envelope is
represented as `DecodedProviderPayload::Error`, separate from malformed
provider response/event errors.

The M7 boundary remains explicit: no learned preference, rejected-wire state,
alternate retry, failure classification, provider submission, or durable
attempt/finalization state was added.

## Required-case evidence

| Requirement | Evidence | Result |
|---|---|---|
| Every W001 profile parses | Embedded registry inventory test asserts all five profiles and ten codec IDs | Pass |
| Duplicate/unknown/malformed definitions fail | Strict TOML tests cover duplicate IDs, unknown IDs/codecs, missing fields, and extra fields | Pass |
| Unknown provider references fail closed | Rust config tests cover unknown provider surface and unavailable model preference | Pass |
| Deterministic lookup/order | BTreeMap-backed lookup plus priority/surface ordering test | Pass |
| Compatibility query | Native Chat/Responses/Messages paths and canonical adaptation path are typed and tested | Pass |
| Stable codec outcome codes | Serde serialization of typed `loss_rejected` reason is asserted | Pass |
| Secrets and runtime state | Registry/profile/debug values contain only structural metadata; no credential or mutable negotiation state exists | Pass |

## Verification

Passed:

```text
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1  # 125 passed
cargo test --manifest-path rust/Cargo.toml --test wire_profiles -- --test-threads=1  # 9 passed
uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 70 passed, 3 skipped
uv run pytest tests/migration_rs/test_f003_config_cli.py tests/unit/test_wire_profiles.py tests/unit/test_wire_codecs.py tests/unit/test_wire_resolver.py tests/integration/test_wire_negotiation_e2e.py -q --tb=short --maxfail=1  # 47 passed
uv run pytest tests/unit/test_config.py tests/unit/test_config_reload_policy.py tests/unit/test_connect.py -q --tb=short --maxfail=1  # 299 passed
uv run eggpool --config config.example.toml check-config  # passed
uv run eggpool --config config.sbc.example.toml check-config  # passed
git diff --check
```

The Rust crate reuses the existing `serde`, `serde_json`, and `toml`
dependencies; no dependency, database migration, network call, background
task, or CI matrix was added. The existing M4 provider transport remains
profile-agnostic.

## Registry transition and future-plan audit

W003 is removed from the dependency-ready table and added to the completed
table in `migration-rs/registry.md` with this accepted closure record. M6
remains active and W004 is promoted to the sole dependency-ready plan because
its hard dependency, W003, is now closed. W005-W010 remain blocked by the
serial predecessor chain in the canonical-wire README and handoff sequence.
No M7 implementation plan is unblocked; M7 remains behind W010 accepted
closure.
