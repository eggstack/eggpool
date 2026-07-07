#!/usr/bin/env bash
# Run the focused model-info identity test subset.
#
# This script is repo-relative: it computes its own location and changes into
# the Eggpool repo root before invoking pytest, so it works from any cwd and
# avoids the ``ModuleNotFoundError: No module named 'eggpool'`` failure that
# occurs when pytest is invoked from a sibling project root.
#
# Usage:
#   scripts/test_model_info_identity.sh
#   /absolute/path/to/eggpool/scripts/test_model_info_identity.sh
#   EGGPOOL_REPO=/absolute/path/to/eggpool "$EGGPOOL_REPO/scripts/test_model_info_identity.sh"
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

TEST_FILES=(
    tests/unit/test_model_info_fresh_db_service.py
    tests/unit/test_model_info_match_evidence_api.py
    tests/unit/test_model_info_matching_safety.py
    tests/unit/test_model_info_migration_0049.py
    tests/unit/test_model_info_tiered_matching.py
    tests/unit/test_model_info_openrouter_contract.py
)

if command -v uv >/dev/null 2>&1; then
    exec uv run pytest "${TEST_FILES[@]}"
else
    PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        exec python -m pytest "${TEST_FILES[@]}"
fi