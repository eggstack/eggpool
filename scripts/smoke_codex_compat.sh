#!/usr/bin/env bash
set -euo pipefail

# Opt-in smoke check for a running EggPool instance and a real Codex CLI.
# Credentials are supplied through the environment and are never printed.

if [[ -z "${EGGPOOL_CODEX_API_KEY:-}" || -z "${EGGPOOL_CODEX_MODEL:-}" ]]; then
  echo "SKIP: set EGGPOOL_CODEX_API_KEY and EGGPOOL_CODEX_MODEL for the live Codex smoke" >&2
  exit 77
fi

codex_bin="${CODEX_BIN:-codex}"
base_url="${EGGPOOL_CODEX_BASE_URL:-http://127.0.0.1:11300/v1}"
prompt="${EGGPOOL_CODEX_PROMPT:-Reply with exactly: eggpool-codex-smoke-ok}"
output_file="$(mktemp "${TMPDIR:-/tmp}/eggpool-codex-smoke.XXXXXX")"
trap 'rm -f "$output_file"' EXIT

EGGPOOL_API_KEY="$EGGPOOL_CODEX_API_KEY" "$codex_bin" exec \
  --ephemeral \
  --skip-git-repo-check \
  --sandbox read-only \
  --config 'approval_policy="never"' \
  --config 'model_provider="eggpool"' \
  --config 'model_providers.eggpool.name="EggPool"' \
  --config "model_providers.eggpool.base_url=\"$base_url\"" \
  --config 'model_providers.eggpool.env_key="EGGPOOL_API_KEY"' \
  --config 'model_providers.eggpool.wire_api="responses"' \
  --config 'model_providers.eggpool.supports_websockets=false' \
  --model "$EGGPOOL_CODEX_MODEL" \
  "$prompt" >"$output_file"

if ! grep -Fq "eggpool-codex-smoke-ok" "$output_file"; then
  echo "FAIL: Codex completed without the expected smoke marker" >&2
  sed -n '1,80p' "$output_file" >&2
  exit 1
fi

echo "PASS: Codex Responses HTTP/SSE smoke completed through EggPool"
