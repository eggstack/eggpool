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

if ! codex_version="$("$codex_bin" --version 2>/dev/null)"; then
  echo "FAIL: unable to run $codex_bin --version" >&2
  exit 1
fi
printf 'Codex CLI: %s\n' "$codex_version"

workdir="$(mktemp -d "${TMPDIR:-/tmp}/eggpool-codex-smoke.XXXXXX")"
text_output="$workdir/text-last-message"
tool_output="$workdir/tool-last-message"
marker_suffix="$(od -An -N16 -tx1 /dev/urandom | tr -d '[:space:]')"
marker="eggpool-tool-${marker_suffix}"
printf '%s\n' "$marker" >"$workdir/tool-smoke-marker.txt"
trap 'rm -rf -- "$workdir"' EXIT

codex_args=(
  exec
  --ephemeral
  --skip-git-repo-check
  --sandbox read-only
  --config 'approval_policy="never"'
  --config 'model_provider="eggpool"'
  --config 'model_providers.eggpool.name="EggPool"'
  --config "model_providers.eggpool.base_url=\"$base_url\""
  --config 'model_providers.eggpool.env_key="EGGPOOL_API_KEY"'
  --config 'model_providers.eggpool.wire_api="responses"'
  --config 'model_providers.eggpool.supports_websockets=false'
  --model "$EGGPOOL_CODEX_MODEL"
)

if ! EGGPOOL_API_KEY="$EGGPOOL_CODEX_API_KEY" "$codex_bin" "${codex_args[@]}" \
  --output-last-message "$text_output" "$prompt" >/dev/null; then
  echo "FAIL: Codex text phase failed" >&2
  exit 1
fi

if ! grep -Fq "eggpool-codex-smoke-ok" "$text_output"; then
  echo "FAIL: Codex text phase did not return the expected smoke marker" >&2
  exit 1
fi
echo "PASS: Codex text phase completed through EggPool"

tool_prompt='Use your shell tool to read ./tool-smoke-marker.txt and reply exactly with its contents. Do not guess the value.'
if ! EGGPOOL_API_KEY="$EGGPOOL_CODEX_API_KEY" "$codex_bin" "${codex_args[@]}" \
  --cd "$workdir" --output-last-message "$tool_output" "$tool_prompt" >/dev/null; then
  echo "FAIL: Codex tool-loop phase failed" >&2
  exit 1
fi

if ! grep -Fq "$marker" "$tool_output"; then
  echo "FAIL: Codex tool-loop phase did not return the temporary marker" >&2
  exit 1
fi
echo "PASS: Codex tool-loop phase completed through EggPool"

echo "PASS: Codex Responses HTTP/SSE text and tool-loop smoke completed through EggPool"
