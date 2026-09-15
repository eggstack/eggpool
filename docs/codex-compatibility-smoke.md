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
`EGGPOOL_CODEX_BASE_URL` when the server uses another listener. It reports the
selected Codex CLI version, configures HTTP/SSE with `wire_api = "responses"`
and `supports_websockets = false`, and runs two phases:

1. a basic streamed text request that must return a fixed marker;
2. a second request from a temporary read-only working directory that requires
   Codex to use its shell tool to read `tool-smoke-marker.txt`, whose random
   marker is not present in the prompt.

Both phases use an explicit model, `--sandbox read-only`, and
`approval_policy="never"`. CLI overrides leave the operator's Codex config
unchanged. Temporary files are cleaned up, and the script does not print API
keys or response bodies. Exit status 77 means the opt-in credential/model
variables were not supplied.

The deterministic Rust harness remains authoritative for wrapper shape, tool
identity, interleaved argument accumulation, reasoning replay, terminal events,
and malformed-wrapper rejection. A successful live smoke qualifies the tested
Codex CLI version and selected upstream path only; it does not imply support
for every future Codex version, provider, or native server tool. Translated
provider live qualification must be recorded separately when it is run.
