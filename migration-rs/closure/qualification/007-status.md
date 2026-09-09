# Q007 Closure — Bounded Live-Provider Interoperability Smoke

Status: blocked; closure attempted 2026-09-09

Plan: [Q007 — bounded live-provider interoperability smoke](../../implementation/qualification/007-live-provider-interoperability-smoke.md)

Implementation commit: `a0b2e75`

Machine-readable live evidence: [`007-run.json`](007-run.json)

Machine-readable deterministic loopback evidence: [`007-loopback.json`](007-loopback.json)

Evidence SHA-256:

- live attempt: `9002f18dbb4a37e8db128e29e5dcbfefe7fb310b23f79dc89e4198de08249343`
- loopback: `7acb26f6cb791b07b60c666e1509e1ae18cca51fbc59f2f372eae6c6b6303486`

Candidate SHA-256: `14c0494c371ce0f245ba7fd83621751fafa1938736697171e81237527dd9baf0`

## Outcome

The Q007 harness and deterministic Rust corrections are implemented, but Q007
cannot be accepted. The authorized live run resolved all three planned model
identifiers from OpenCode Go and used the fixed seven-request, three-stream
matrix. The first Responses request reached the direct provider path but the
provider edge returned HTTP 403; EggPool consequently produced no upstream
request ID or successful finite envelope. The live run stopped immediately and
did not spend the remaining request budget.

The deterministic loopback run passed all seven cells: OpenAI Responses,
OpenAI Chat Completions, Anthropic Messages, Messages-to-Responses
cross-surface adaptation, and one streaming cell for each wire family. It
verified request IDs, normalized usage, durable completion, no pending
requests, no active reservations, and secret-free semantic observations.

## Matrix and safety

| Area | Result |
|---|---|
| Live provider/model catalog | pass; all three planned identifiers resolved |
| Live OpenAI Responses finite | blocked; provider edge HTTP 403 to Rust direct attempt |
| Live remaining finite/stream/cross-surface cells | not run after first live failure |
| Offline loopback finite/stream/cross-surface matrix | pass; 7/7 cells |
| Request budget | 7 planned; 1 live request submitted; no external retry loop |
| Proxy path | not applicable; Q001 has no mandatory live proxy cell |
| Credentials | read from environment variable `OPENCODE_GO_KEY_1`; no value written |
| Redaction | pass; no credentials, proxy credentials, or raw response bodies persisted |

The live command was:

```text
rtk uv run python scripts/qualification_live_provider.py \
  --binary rust/target/debug/eggpool \
  --enable-live \
  --provider-key-env OPENCODE_GO_KEY_1 \
  --env-file .env \
  --output migration-rs/closure/qualification/007-run.json
```

The deterministic command was:

```text
rtk uv run python scripts/qualification_live_provider.py \
  --binary rust/target/debug/eggpool \
  --offline-fake \
  --output migration-rs/closure/qualification/007-loopback.json
```

## Defects and regressions

Deterministic integration exposed and corrected three migration defects before
the live attempt: configured model-wire preferences were not applied to the
Rust generation, raw API-key auth incorrectly received a bearer prefix, and
cross-surface routing supplied no transcodable protocol facts. The Rust
Responses codec also now accepts the provider's valid `error: null` field, with
a focused regression test. The live HTTP 403 remains unresolved and blocks
acceptance; it is not reclassified as a successful provider interaction.

Focused checks passed: Q007 offline contract tests (6 passed), Rust
`coordinator_boundaries` (5 passed), Rust all-target compile, Rust formatting,
Ruff formatting/checks for the new harness/tests, and the full seven-cell
loopback run.

## Registry transition

Q007 is formally recorded as blocked, not accepted. Q008 is not promoted:
Q008-Q010 remain queued behind their direct predecessors, and M11 remains
blocked on accepted Q010 plus its separate planning review. A future Q007
corrective pass must provide a real live-provider success matrix or an
explicitly reviewed provider-transport decision before Q008 can unblock.
