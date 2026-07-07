#!/usr/bin/env bash
# Live operator verification script for the model-info OpenRouter enrichment.
#
# Runs the three commands that prove a model (default ``minimax-m3``) is
# being enriched by the OpenRouter source end-to-end:
#
# 1. POST /api/model-info/refresh?model_id=<id>&force=1
#    — forces a refresh and prints ``source_diagnostics``.
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
#
# Usage:
#
#   ./scripts/debug_model_info_openrouter.sh              # uses defaults
#   ./scripts/debug_model_info_openrouter.sh gpt-4o       # override model
#   EGGPOOL_DB=/var/lib/eggpool/usage.sqlite3 ./scripts/debug_model_info_openrouter.sh
#
# The script intentionally never edits state — refresh is read-only and
# the SQLite query is read-only.

set -euo pipefail

BASE="${EGGPOOL_BASE_URL:-http://127.0.0.1:8000}"
MODEL="${1:-minimax-m3}"
DB="${EGGPOOL_DB:-usage.sqlite3}"

if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required" >&2
    exit 1
fi
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "sqlite3 is required" >&2
    exit 1
fi

echo "==> Refreshing model-info for ${MODEL} (force=1)"
echo "    POST ${BASE}/api/model-info/refresh?model_id=${MODEL}&force=1"
curl -sS -X POST \
    "${BASE}/api/model-info/refresh?model_id=${MODEL}&force=1" \
    | python3 -m json.tool

echo ""
echo "==> Reading detail for ${MODEL}"
echo "    GET ${BASE}/api/model-info/${MODEL}"
curl -sS "${BASE}/api/model-info/${MODEL}" | python3 -m json.tool

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
echo "    GET ${BASE}/api/model-info/${MODEL}/matches"
curl -sS "${BASE}/api/model-info/${MODEL}/matches" | python3 -m json.tool

echo ""
echo "==> model_info_match_evidence snapshot (file: ${DB})"
if [ -f "${DB}" ]; then
    sqlite3 "${DB}" <<SQL
.headers on
.mode column
SELECT model_id, source, alias, match_method, confidence, provider_id, last_seen_at
FROM model_info_match_evidence
WHERE lower(model_id) = lower('${MODEL}')
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
WHERE lower(model_id) = lower('${MODEL}')
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