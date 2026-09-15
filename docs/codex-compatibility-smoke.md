# Codex compatibility smoke

The deterministic Rust target `codex_responses_compat` is the required
regression harness for the Responses contract. A real Codex CLI check is
available separately because it needs a running EggPool server, a configured
provider account, and a test-only credential.

Start EggPool with an explicit provider model or alias, then run:

```bash
export EGGPOOL_CODEX_API_KEY='test-only-key'
export EGGPOOL_CODEX_MODEL='eggpool-model-or-alias'
scripts/smoke_codex_compat.sh
```

The script defaults to `http://127.0.0.1:11300/v1`. Override it with
`EGGPOOL_CODEX_BASE_URL` when the server uses another listener. It configures
the current Codex CLI with `wire_api = "responses"` and
`supports_websockets = false`, sends one bounded streamed request, and checks
for a fixed response marker. It uses CLI overrides rather than changing the
operator's Codex config, and it never prints the API key or response body on a
successful run. Exit status 77 means the opt-in credential/model variables
were not supplied.

For a stronger release check, set `EGGPOOL_CODEX_PROMPT` to a short request
that causes the selected model to use one ordinary function tool, then repeat
with a prompt that exercises the provider's custom/freeform tool behavior when
that model supports it. The deterministic harness remains authoritative for
wrapper shape, tool identity, reasoning replay, terminal events, and malformed
wrapper rejection; a live smoke is supplementary provider evidence.
