# Test Invocation and Model-Info Patch Plan

## Context

The current GitHub CI is clean, but a local targeted pytest invocation failed when run from the wrong checkout/root:

```text
rootdir: /Users/davidbowman/projects/gorouter
ModuleNotFoundError: No module named 'eggpool'
```

The failing test files are Eggpool tests and import `eggpool.*`, but pytest was invoked from `/Users/davidbowman/projects/gorouter`. This should not happen for normal CI, but it is a recurring handoff/operator problem: test commands in plans and docs need to be robust, repo-relative, and explicit enough that running them from a sibling project does not produce misleading import failures.

There are also two model-info patch items still worth closing:

1. `ModelInfoService._known_provider_namespaces()` currently calls `self._catalog.known_provider_ids()`. Verify that `ModelCatalogCache.known_provider_ids()` exists on the concrete cache class. If not, add it. Without it, tiered OpenRouter matching can fall back to legacy exact-alias matching and silently lose the fresh-DB normalization fix.
2. `register_model_info_routes()` registers the greedy `/api/model-info/{model_id:path}` route before `/api/model-info/{model_id:path}/aliases`. The new `/matches` route is correctly before detail, but `/aliases` may be shadowed. Reorder route registration so all specific suffix routes are registered before the greedy detail route.

This plan covers both the test-invocation hardening and these remaining model-info patches.

## Goals

1. Make local test invocation instructions repo-relative and resilient.
2. Add a small wrapper script or Make/just target for the model-info identity test subset.
3. Ensure tests can be invoked from the Eggpool repo root without requiring absolute paths.
4. Add explicit guardrails/documentation for running from outside the repo root.
5. Ensure `ModelCatalogCache.known_provider_ids()` exists and is tested.
6. Reorder model-info API routes so `/aliases` is not shadowed by the greedy detail route.
7. Set the pytest-asyncio fixture loop scope explicitly to remove the deprecation warning.

## Non-goals

- Do not modify package import semantics to support arbitrary sibling-project root execution unless the command explicitly points at Eggpool.
- Do not add broad environment-specific absolute paths to docs or tests.
- Do not change production matching behavior beyond fixing the concrete namespace accessor and route ordering.
- Do not make tests depend on live network access.

## Phase 1: Add a repo-relative focused test command

### Option A: Add a script

Add:

```text
scripts/test_model_info_identity.sh
```

Script behavior:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if command -v uv >/dev/null 2>&1; then
  exec uv run pytest \
    tests/unit/test_model_info_fresh_db_service.py \
    tests/unit/test_model_info_match_evidence_api.py \
    tests/unit/test_model_info_matching_safety.py \
    tests/unit/test_model_info_migration_0049.py \
    tests/unit/test_model_info_tiered_matching.py \
    tests/unit/test_model_info_openrouter_contract.py
else
  PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    exec python -m pytest \
      tests/unit/test_model_info_fresh_db_service.py \
      tests/unit/test_model_info_match_evidence_api.py \
      tests/unit/test_model_info_matching_safety.py \
      tests/unit/test_model_info_migration_0049.py \
      tests/unit/test_model_info_tiered_matching.py \
      tests/unit/test_model_info_openrouter_contract.py
fi
```

Notes:

- The script computes `REPO_ROOT` relative to itself, not from `$PWD`.
- It changes into the repo root before running pytest.
- It prefers `uv run pytest`, matching the project environment.
- It has a fallback that sets `PYTHONPATH=$REPO_ROOT/src` for non-uv environments.
- It does not include host-specific absolute paths.

Invocation should work from any cwd:

```bash
/path/to/eggpool/scripts/test_model_info_identity.sh
```

From the repo root:

```bash
scripts/test_model_info_identity.sh
```

### Option B: Add a Make target if Makefile exists

If the repo already has a Makefile, add:

```make
test-model-info-identity:
	uv run pytest \
		tests/unit/test_model_info_fresh_db_service.py \
		tests/unit/test_model_info_match_evidence_api.py \
		tests/unit/test_model_info_matching_safety.py \
		tests/unit/test_model_info_migration_0049.py \
		tests/unit/test_model_info_tiered_matching.py \
		tests/unit/test_model_info_openrouter_contract.py
```

If no Makefile exists or the repo prefers scripts, use the script only.

## Phase 2: Document correct test invocation

Update relevant docs and plans, especially:

- `docs/model-info-openrouter-debug.md`
- any model-info identity/matching plan that includes test commands
- `AGENTS.md` or repo handoff instructions if it already lists common test commands

Use repo-relative commands:

```bash
cd /path/to/eggpool
uv run pytest tests/unit/test_model_info_fresh_db_service.py \
              tests/unit/test_model_info_match_evidence_api.py \
              tests/unit/test_model_info_matching_safety.py \
              tests/unit/test_model_info_migration_0049.py \
              tests/unit/test_model_info_tiered_matching.py \
              tests/unit/test_model_info_openrouter_contract.py
```

Also document the script:

```bash
scripts/test_model_info_identity.sh
```

For external invocation, document the safe form:

```bash
EGGPOOL_REPO=/path/to/eggpool
"$EGGPOOL_REPO/scripts/test_model_info_identity.sh"
```

Avoid instructions that tell users to run Eggpool tests from a sibling project root.

## Phase 3: Add import-root guard if useful

If local confusion persists, consider adding a small `tests/conftest.py` guard. Only do this if it does not conflict with existing test infrastructure.

Possible implementation:

```python
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
```

However, prefer using `uv run pytest` from repo root and a script that sets cwd. Only add this guard if the project already uses src-layout path insertion or if direct `python -m pytest` without install is supported.

Acceptance criterion: the focused model-info identity suite runs from the repo root and via `scripts/test_model_info_identity.sh` without `ModuleNotFoundError`.

## Phase 4: Set pytest-asyncio fixture loop scope explicitly

The local output included:

```text
The event loop scope for asynchronous fixtures will default to the fixture caching scope.
Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to function scope.
```

Patch `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_default_fixture_loop_scope = "function"
```

If `[tool.pytest.ini_options]` already exists, add only the setting. Do not duplicate the section.

Run the focused suite and confirm the warning is gone.

## Phase 5: Add `ModelCatalogCache.known_provider_ids()` if missing

### Problem

`ModelInfoService._known_provider_namespaces()` calls:

```python
provider_ids = self._catalog.known_provider_ids()
```

If the concrete `ModelCatalogCache` lacks this method, tiered OpenRouter resolution raises inside `_resolve_openrouter_with_tiered_matching()`, is caught, and falls back to legacy exact matching. That would quietly disable the key fresh-DB normalization improvement.

### Implementation

Add to `src/eggpool/catalog/cache.py` near provider-entry accessors:

```python
def known_provider_ids(self) -> set[str]:
    """Return provider IDs known from provider-scoped catalog rows."""
    return {str(provider_id) for (_model_id, provider_id) in self._provider_models}
```

If provider IDs can ever be `None`, filter them:

```python
return {str(provider_id) for (_model_id, provider_id) in self._provider_models if provider_id}
```

Given `_provider_models` is keyed as `tuple[str, str]`, the first form should be fine.

### Tests

Add to catalog cache tests:

```python
def test_known_provider_ids_returns_provider_model_namespaces():
    cache = ModelCatalogCache()
    cache._provider_models[("minimax-m3", "opencode-go")] = {}
    cache._provider_models[("gpt-4o", "openai-direct")] = {}
    assert cache.known_provider_ids() == {"opencode-go", "openai-direct"}
```

Add a service regression test that fails if the method is missing:

```python
async def test_service_known_provider_namespaces_uses_catalog_accessor():
    cache = _make_cache("minimax-m3", provider_id="opencode-go")
    service = ModelInfoService(config, db, cache, outbound_client=client)
    assert service._known_provider_namespaces() == {"opencode-go"}
```

## Phase 6: Reorder model-info API routes

### Problem

FastAPI path route order matters when `{model_id:path}` is greedy. Current route order registers:

```text
/api/model-info/{model_id:path}/matches
/api/model-info/{model_id:path}
/api/model-info/{model_id:path}/aliases
```

The detail route may shadow `/aliases` because `{model_id:path}` can capture `foo/aliases` as the model ID.

### Implementation

Register all specific subroutes before the greedy detail route:

```python
app.add_api_route(
    path="/api/model-info/{model_id:path}/matches",
    endpoint=handle_model_info_matches,
    methods=["GET"],
    dependencies=dependencies,
)
app.add_api_route(
    path="/api/model-info/{model_id:path}/aliases",
    endpoint=handle_model_info_aliases,
    methods=["GET"],
    dependencies=dependencies,
)
app.add_api_route(
    path="/api/model-info/{model_id:path}",
    endpoint=handle_model_info_detail,
    methods=["GET"],
    dependencies=dependencies,
)
```

### Tests

Add a route registration test with a small FastAPI app:

```python
async def test_aliases_route_not_shadowed_by_detail_route():
    app = FastAPI()
    register_model_info_routes(app, require_auth=False)
    # attach mock model_info to app.state
    # GET /api/model-info/minimax-m3/aliases should hit alias handler
    # not detail handler with model_id='minimax-m3/aliases'
```

Alternatively, assert route order directly:

```python
paths = [route.path for route in app.routes]
assert paths.index("/api/model-info/{model_id:path}/aliases") < paths.index("/api/model-info/{model_id:path}")
assert paths.index("/api/model-info/{model_id:path}/matches") < paths.index("/api/model-info/{model_id:path}")
```

Prefer a functional request test if the test suite already uses `TestClient`.

## Phase 7: Verify local and CI behavior

Run from the Eggpool repo root:

```bash
scripts/test_model_info_identity.sh
```

Also run directly:

```bash
uv run pytest tests/unit/test_model_info_fresh_db_service.py \
              tests/unit/test_model_info_match_evidence_api.py \
              tests/unit/test_model_info_matching_safety.py \
              tests/unit/test_model_info_migration_0049.py \
              tests/unit/test_model_info_tiered_matching.py \
              tests/unit/test_model_info_openrouter_contract.py
```

Run route/catalog-specific tests:

```bash
uv run pytest tests/unit/test_catalog.py tests/unit/test_model_info_match_evidence_api.py
```

Then run broader model-info subset:

```bash
uv run pytest tests/unit/test_model_info*.py
```

Expected:

- no `ModuleNotFoundError: No module named 'eggpool'` when using repo-root commands or the script;
- no pytest-asyncio default-loop-scope warning;
- `known_provider_ids()` tests pass;
- `/aliases` route is not shadowed;
- CI remains clean.

## Acceptance criteria

This pass is complete when:

1. A repo-relative script or equivalent target exists for the focused model-info identity suite.
2. Docs/plans instruct users to run tests from the Eggpool repo root or through the repo-relative script.
3. `pyproject.toml` explicitly sets `asyncio_default_fixture_loop_scope = "function"`.
4. `ModelCatalogCache.known_provider_ids()` exists and has direct test coverage.
5. `ModelInfoService._known_provider_namespaces()` no longer risks `AttributeError` on the concrete catalog cache.
6. `/api/model-info/{model_id:path}/aliases` is registered before the greedy detail route and is covered by a route test.
7. The focused model-info suite passes locally and in CI.

## Suggested commit message

```text
Harden model-info test invocation and namespace patch
```

## Notes for implementer

The local pytest failure shown from `/Users/davidbowman/projects/gorouter` is not an Eggpool test failure by itself; it is a working-directory/package-root issue. The fix is to make the handoff commands and scripts unambiguous and repo-relative, not to add host-specific paths or make Eggpool importable from arbitrary sibling project roots by default.
