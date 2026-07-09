#!/usr/bin/env bash
# Live operator verification script for the model-info OpenRouter enrichment.
#
# Runs the three commands that prove a model (default ``minimax-m3``) is
# being enriched by the OpenRouter source end-to-end:
#
# 1. POST /api/model-info/refresh?model_id=<id>&force=1
#    — forces a refresh and prints ``source_diagnostics``.  This call is
#    state-changing: it fetches external source data, updates the model-info
#      tables (canonical rows, source health, aliases, observations, match
#      evidence).
# 2. GET /api/model-info/<id>
#    — reads the canonical detail (limits, display_name, external_ids,
#      pricing block, observations[]).
# 3. SELECT … FROM model_info_source_health
#    — confirms OpenRouter source health was recorded.
#
# Required environment:
#
#   EGGPOOL_BASE_URL   base URL of the running Eggpool (default
#                      ``http://127.0.0.1:8000``).
#   EGGPOOL_DB         path to ``usage.sqlite3`` (default
#                      ``usage.sqlite3`` in the current directory).
#   EGGPOOL_API_KEY    optional ``[server].api_key`` (or ``x-api-key``)
#                      used to authenticate against the refresh endpoint,
#                      which is always auth-gated regardless of
#                      ``dashboard.public``.  When unset, the script
#                      assumes authentication is disabled.
#
# Usage:
#
#   ./scripts/debug_model_info_openrouter.sh              # uses defaults
#   ./scripts/debug_model_info_openrouter.sh gpt-4o       # override model
#   EGGPOOL_DB=/var/lib/eggpool/usage.sqlite3 ./scripts/debug_model_info_openrouter.sh
#   EGGPOOL_API_KEY=<server.api_key> ./scripts/debug_model_info_openrouter.sh
#
# This script performs a forced refresh (state-changing) for the given
# model id and reads only from the local SQLite file.  The SQLite
# inspection queries are read-only.

set -euo pipefail

BASE="${EGGPOOL_BASE_URL:-http://127.0.0.1:8000}"
MODEL="${1:-minimax-m3}"
DB="${EGGPOOL_DB:-usage.sqlite3}"
API_KEY="${EGGPOOL_API_KEY:-}"

# Percent-encode the model id for URL paths and query strings so values
# containing ``&``, ``?``, ``#``, ``/``, or whitespace do not change the
# request shape.
# SQL single-quote escape: a single embedded ``'`` is rewritten as ``''``
# so the SQLite heredoc treats the value as a string literal rather than
# as SQL syntax.
SAFE_VALUES="$(MODEL="$MODEL" python3 - <<'PY'
import os, urllib.parse
model = os.environ["MODEL"]
encoded = urllib.parse.quote(model, safe="")
sql = model.replace("'", "''")
print(f"{encoded}\n{sql}")
PY
)"
MODEL_ENCODED="$(printf '%s\n' "${SAFE_VALUES}" | sed -n '1p')"
SQL_MODEL="$(printf '%s\n' "${SAFE_VALUES}" | sed -n '2p')"

# Build curl auth args.  The server accepts both ``Authorization: Bearer``
# and ``x-api-key``; we use Bearer for consistency with the documented
# contract.
CURL_AUTH_ARGS=()
if [ -n "${API_KEY}" ]; then
    CURL_AUTH_ARGS=(-H "Authorization: Bearer ${API_KEY}")
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required" >&2
    exit 1
fi
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "sqlite3 is required" >&2
    exit 1
fi

echo "==> Refreshing model-info for ${MODEL} (force=1)"
echo "    POST ${BASE}/api/model-info/refresh?model_id=${MODEL_ENCODED}&force=1"
post_refresh() {
    if [ ${#CURL_AUTH_ARGS[@]} -gt 0 ]; then
        curl -sS -o /tmp/eggpool_debug_refresh_body.$$ -w '%{http_code}' \
            -X POST \
            "${CURL_AUTH_ARGS[@]}" \
            "${BASE}/api/model-info/refresh?model_id=${MODEL_ENCODED}&force=1"
    else
        curl -sS -o /tmp/eggpool_debug_refresh_body.$$ -w '%{http_code}' \
            -X POST \
            "${BASE}/api/model-info/refresh?model_id=${MODEL_ENCODED}&force=1"
    fi
}
REFRESH_BODY="/tmp/eggpool_debug_refresh_body.$$"
refresh_status="$(post_refresh || echo "000")"
if [ "${refresh_status}" = "401" ] && [ -z "${API_KEY}" ]; then
    echo "    (401 Invalid or missing API key — set EGGPOOL_API_KEY or [server].api_key and retry)" >&2
    if [ -f "${REFRESH_BODY}" ]; then
        cat "${REFRESH_BODY}" | python3 -m json.tool || true
    fi
    rm -f "${REFRESH_BODY}"
    exit 1
fi
if [ -f "${REFRESH_BODY}" ]; then
    cat "${REFRESH_BODY}" | python3 -m json.tool || true
fi
rm -f "${REFRESH_BODY}"

echo ""
echo "==> Reading detail for ${MODEL}"
echo "    GET ${BASE}/api/model-info/${MODEL_ENCODED}"
curl -sS "${BASE}/api/model-info/${MODEL_ENCODED}" | python3 -m json.tool

echo ""
echo "==> model_info_source_health snapshot (file: ${DB})"
if [ ! -f "${DB}" ]; then
    echo "    (skip: ${DB} not found)"
    exit 0
fi
sqlite3 "${DB}" <<'SQL'
.headers on
.mode column
SELECT source, enabled, last_success_at, last_error_at, failure_count, last_payload_count
FROM model_info_source_health
ORDER BY source;
SQL

echo ""
echo "==> Match evidence for ${MODEL}"
echo "    GET ${BASE}/api/model-info/${MODEL_ENCODED}/matches"
curl -sS "${BASE}/api/model-info/${MODEL_ENCODED}/matches" | python3 -m json.tool

echo ""
echo "==> model_info_match_evidence snapshot (file: ${DB})"
if [ -f "${DB}" ]; then
    sqlite3 "${DB}" <<SQL
.headers on
.mode column
SELECT model_id, source, alias, match_method, confidence, provider_id, last_seen_at
FROM model_info_match_evidence
WHERE lower(model_id) = lower('${SQL_MODEL}')
ORDER BY created_at DESC
LIMIT 10;
SQL
else
    echo "    (skip: ${DB} not found)"
fi

echo ""
echo "==> model_info_aliases with match_method (file: ${DB})"
if [ -f "${DB}" ]; then
    sqlite3 "${DB}" <<SQL
.headers on
.mode column
SELECT model_id, source, alias, match_method, discovered_by
FROM model_info_aliases
WHERE lower(model_id) = lower('${SQL_MODEL}')
ORDER BY source, alias;
SQL
else
    echo "    (skip: ${DB} not found)"
fi

echo ""
echo "==> Expected outcomes"
echo "    source_diagnostics.openrouter.miss_reason = matched"
echo "    source_diagnostics.openrouter.matched_source_model_id = minimax/minimax-m3"
echo "    source_diagnostics.openrouter.match_method = normalized_exact or regex_rule"
echo "    detail.display_name populated when provider lacks one"
echo "    detail.external_ids.openrouter = minimax/minimax-m3"
echo "    detail.pricing.openrouter present with advisory pricing"
echo "    detail.limits.external_context = 1048576"
echo "    detail.limits.external_output = 512000"
echo "    detail.match_evidence[] contains rows with match_method"
echo "    observations[] contains an OpenRouter row with"
echo "      source_model_id = minimax/minimax-m3 (NOT the local id)"
echo "    model_info_source_health.openrouter.last_payload_count > 0"
