# Live Wire-Surface Verification

EggPool's provider-wire acceptance suite is deliberately opt-in. It uses a
temporary SQLite database and configuration, calls EggPool's public ASGI
endpoints, and records only sanitized outbound observations (provider,
account alias, model, path/surface, status, auth scheme, semantic field names,
streaming, attempt ordinal, and selection source). It never records keys,
header values, raw bodies, or provider response bodies.

## OpenCode Go

The current official OpenCode Go endpoint table lists these representative
surfaces:

| Model ID | Upstream surface |
| --- | --- |
| `muse-spark-1.2-contributor` | OpenAI Responses (`/responses`) |
| `gpt-5.6-luna` | OpenAI Responses (`/responses`) |
| `minimax-m3` | Anthropic Messages (`/messages`) |
| `mimo-v2.5` | OpenAI Chat Completions (`/chat/completions`) |

Re-check the [official Go endpoint table](https://dev.opencode.ai/docs/go/)
before a release verification because the model list and endpoint assignments
can change.

Set the test-only credential variable in the shell that runs pytest:

```bash
export EGGPOOL_E2E_OPENCODE_GO_API_KEY='sk-your-opencode-go-key'
uv run pytest tests/live/test_opencode_go_wire_live.py \
  -m live_opencode_go -v
```

The suite covers non-streaming path selection and learned steady state,
Responses/Chat/Messages streaming terminal evidence, a public
Messages-to-Responses cross-surface request, surface-native Muse and MiMo
reasoning shapes, and invalid-key isolation. The invalid-key fixture uses
ordinary account weights to make the bad account the first candidate; it does
not add a production-only routing hook. It uses bounded, low-token requests;
it is not a load test or billing benchmark. Without the environment variable,
pytest skips it cleanly. It is excluded from the default suite, smoke tests,
and CI.

The optional second account variable, `EGGPOOL_E2E_OPENCODE_GO_API_KEY_2`, is
reserved for future multi-valid-account checks. Gemini live checks use
`EGGPOOL_E2E_GEMINI_API_KEY` when a direct Gemini live matrix is enabled; the
deterministic Gemini codec and path tests do not require credentials.

For release closure, record the exact live test outcomes. A clean skip caused
by a missing credential is not live verification evidence.

## Deterministic migration acceptance

The mandatory stale-profile check uses an in-process fake upstream:

```bash
uv run pytest tests/integration/test_wire_negotiation_e2e.py -q
```

It first accepts Responses, then changes to a safe Responses rejection with
Chat acceptance. The same account succeeds after an in-process alternate-wire
retry, the Chat preference is learned, and the next request uses Chat without
a restart or database reset. The test also verifies that the outbound hook
sees the actual paths and attempt ordinals without exposing credentials.
The same integration module covers an unhinted known-model migration, strong
model-absence control, and Messages↔Responses/Chat cross-surface adaptation.
