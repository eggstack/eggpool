# Q011 Closure — Q007 Live-Provider Corrective Closure

Status: accepted; closed 2026-09-10

Plan: [Q011 — Q007 live-provider corrective closure](../../implementation/qualification/011-q007-live-provider-corrective-closure.md)

Corrects: blocked Q007 and blocked Q010 closure

Implementation commit: `daae984`

Machine-readable evidence: [`011-run.json`](011-run.json)

Evidence SHA-256: `440d5af387e7d1c81bdbe9282f232196eab05c4fb1389797acc891f1ca081a3f`

Candidate SHA-256: `d14da6d963efd9d8ddaa6bc3b58d158bd7503c4a52e5aa8293ffaace805eb2c2`

## Outcome

Q011 is accepted. The original OpenCode Go attempt remains recorded as a
blocked provider-edge result in Q007's historical closure; it was not
reclassified as a pass. The supplied OpenCode credential reached the catalog,
but its first Responses cell still did not produce an accepted finite result.
The corrective plan's reviewed fallback was used: two structurally distinct,
maintainer-authorized provider edges completed the frozen seven-request budget
without an external retry loop.

GeneralCompute supplied the OpenAI-compatible Chat surface, including real
Responses and Chat client requests plus streaming. MiniMax International
supplied the Anthropic Messages surface, including native Messages streaming
and a Chat-to-Messages cross-surface request. Both catalogs resolved the
planned model identifiers.

## Live matrix

| Cell | Provider/model | Client → upstream surface | Result |
|---|---|---|---|
| responses-finite | GeneralCompute / `gpt-oss-120b` | Responses → OpenAI Chat | HTTP 200; request ID, usage, durable completion |
| chat-finite | GeneralCompute / `gpt-oss-120b` | Chat → OpenAI Chat | HTTP 200; request ID, usage, durable completion |
| messages-finite | MiniMax / `MiniMax-M2.5` | Messages → Anthropic Messages | HTTP 200; usage, durable completion |
| chat-to-messages-finite | MiniMax / `MiniMax-M2.5` | Chat → Anthropic Messages | HTTP 200; usage, durable completion |
| responses-stream | GeneralCompute / `gpt-oss-120b` | Responses → OpenAI Chat | HTTP 200; incremental events and `response.completed` |
| chat-stream | GeneralCompute / `gpt-oss-120b` | Chat → OpenAI Chat | HTTP 200; incremental events and `[DONE]` |
| messages-stream | MiniMax / `MiniMax-M2.5` | Messages → Anthropic Messages | HTTP 200; incremental events and `message_stop` |

The run resolved both planned models, submitted exactly 7 requests, completed
7 requests and 7 attempts, observed 4 upstream request IDs, and converged to
zero pending requests, zero active reservations, and zero account backoffs.
Normalized usage totals were 419 input tokens and 184 output tokens. The
report retained no raw response body, credential, proxy credential, or client
body; only bounded status, usage, terminal, request-ID, and durable facts were
written.

The exact corrective command was:

```text
uv run python scripts/qualification_live_provider.py \
  --binary rust/target/release/eggpool \
  --enable-live --profile q011-multi \
  --provider-key-env Q011_GENERALCOMPUTE_KEY \
  --secondary-provider-key-env Q011_MINIMAX_KEY \
  --output migration-rs/closure/qualification/011-run.json
```

The two environment variables were populated only in the invoking shell. No
key values were stored in the repository or closure evidence.

## Findings and deterministic regressions

The OpenCode diagnostic identified that its edge requires the documented
`x-opencode-session` request header. A deterministic Rust coordinator
regression now proves that provider-required session headers are forwarded
while `x-eggpool-route-session` remains private. The Responses codec also has
a regression for a valid `status = "incomplete"`, `error = null` response with
usage and no output blocks. These regressions are secret-free and do not add a
new runtime dependency or persistence surface.

The alternate-provider run exposed no high- or medium-severity EggPool live
interoperability finding. The unsupported OpenCode edge result remains a
provider-specific qualification limitation, not a fabricated success or a
waiver of the Q001 live requirement.

## Verification

```text
uv run pytest tests/migration_rs/test_q007_live_provider.py tests/migration_rs/test_q011_live_provider.py -q --tb=short --maxfail=1  # 9 passed
uv run python scripts/qualification_live_provider.py --binary rust/target/release/eggpool --offline-fake --output /tmp/q007-loopback-final.json  # pass; 7/7
uv run python scripts/qualification_runner.py --skip-build  # 18 pass, 0 fail/block/skip
cargo fmt --manifest-path rust/Cargo.toml --all -- --check  # pass
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings  # pass
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1  # 445 passed; 52 suites
uv run pytest tests/migration_rs -q --tb=short --maxfail=1  # 161 passed, 3 skipped
uv run pytest tests/smoke/ -q --tb=short --maxfail=1  # 14 passed
git diff --check  # pass
```

## Dependency-order re-acceptance

Q011 removes the live-provider blocker. Q008, Q009, and Q010 were then
re-accepted in direct dependency order through append-only addenda to their
existing blocked records. Q008's accepted physical Raspberry Pi evidence and
Q009's accepted deterministic stability evidence were unchanged by this
qualification-only corrective implementation. The Q010 addendum accepts M10
and makes M11 eligible for a separate planning review; it does not authorize
M11 cutover or auto-promote an M11 implementation plan.

## Registry transition

Q007's original blocked record remains historical and unchanged. Q011 is
accepted as its corrective closure. Q008, Q009, and Q010 are accepted in
dependency order; M10 is closed. M11 is eligible for a separate planning
review only. Python remains the public install/release/update authority.
