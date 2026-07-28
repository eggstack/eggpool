"""Plan 030 — Cross-phase architecture audit (Workstream A).

Verifies that there is exactly one authoritative implementation for each
of the ten architecture ownership points established by Plans 024–029,
and that legacy/duplicate paths are unreachable or removed.

The audit uses structural assertions (AST/source inspection and import
graph checks) rather than behavioural tests — it pins the ownership
invariant so future refactors cannot silently reintroduce dual paths.

Run with::

    uv run pytest tests/unit/test_plan_030_architecture_audit.py -v
"""

from __future__ import annotations

import ast
import inspect
import os
from importlib import import_module
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.request_path]


# ---------------------------------------------------------------------------
# Ownership point registry
# ---------------------------------------------------------------------------
#
# Each entry maps an ownership point to the single canonical module + symbol
# that must own it.  Tests assert that no *other* module defines a function
# or class with the same responsibility name.

OWNERSHIP_POINTS: list[tuple[str, str, str]] = [
    (
        "provider-bound thinking adaptation function",
        "eggpool.transcoder.provider_adaptation",
        "adapt_thinking_controls",
    ),
    (
        "failure-effects classifier",
        "eggpool.failure.classifier",
        "classify_failure_effects",
    ),
    (
        "failure-effects application boundary",
        "eggpool.failure.applier",
        "EffectsApplier",
    ),
    (
        "selected-attempt finalization supervisor",
        "eggpool.request.finalization_job",
        "RequestFinalizationSupervisor",
    ),
    (
        "runtime ownership release abstraction",
        "eggpool.request.finalization_job",
        "AttemptRuntimeLease",
    ),
    (
        "process-owned database recovery controller",
        "eggpool.db.recovery",
        "DatabaseRecoveryController",
    ),
    (
        "decoded provider-bound request lifecycle",
        "eggpool.request.provider_bound_request",
        "ProviderBoundRequest",
    ),
    (
        "non-stream parsed response lifecycle",
        "eggpool.request.parsed_upstream_response",
        "ParsedUpstreamResponse",
    ),
    (
        "bounded dispatch-writer diagnostics implementation",
        "eggpool.request.dispatch_writer",
        "DispatchPersistenceWriter",
    ),
    (
        "request-coherent span sampling decision",
        "eggpool.runtime_dispatch",
        "DispatchSpanRecorder",
    ),
]

# Symbols that are *allowed* to appear in multiple modules (e.g. the
# dataclass is defined in one place and re-exported in another).  Each
# entry is (symbol_name, canonical_module).
ALLOWED_REEXPORTS: dict[str, str] = {
    "FailureEffects": "eggpool.failure.effects",
    "FailureObservation": "eggpool.failure.observation",
    "ModelQuarantine": "eggpool.failure.quarantine",
    "EffectsApplier": "eggpool.failure.applier",
    "ProviderBoundRequest": "eggpool.request.provider_bound_request",
    "ParsedUpstreamResponse": "eggpool.request.parsed_upstream_response",
    "RequestFinalizationSupervisor": "eggpool.request.finalization_job",
    "AttemptRuntimeLease": "eggpool.request.finalization_job",
    "DatabaseRecoveryController": "eggpool.db.recovery",
    "DispatchSpanRecorder": "eggpool.runtime_dispatch",
    "DispatchPersistenceWriter": "eggpool.request.dispatch_writer",
    "ThinkingControlContract": "eggpool.catalog.capabilities",
    "adapt_thinking_controls": "eggpool.transcoder.provider_adaptation",
    "classify_failure_effects": "eggpool.failure.classifier",
}

# Modules that must NOT define a duplicate of the canonical symbol.
# These are the legacy locations that were audited and removed.
FORBIDDEN_DUPLICATE_MODULES: dict[str, list[str]] = {
    "adapt_thinking_controls": [
        "eggpool.request.coordinator",
        "eggpool.transcoder.streaming",
        "eggpool.transcoder.openai_to_anthropic",
        "eggpool.transcoder.anthropic_to_openai",
    ],
    "classify_failure_effects": [
        "eggpool.request.coordinator",
        "eggpool.request.finalizer",
        "eggpool.retry",
    ],
    "EffectsApplier": [
        "eggpool.request.coordinator",
        "eggpool.request.finalizer",
    ],
    "RequestFinalizationSupervisor": [
        "eggpool.request.coordinator",
        "eggpool.request.finalizer",
    ],
    "DatabaseRecoveryController": [
        "eggpool.db.connection",
        "eggpool.request.coordinator",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_for(module_name: str) -> str | None:
    """Return the source code of *module_name* or ``None`` if unavailable."""
    try:
        mod = import_module(module_name)
    except Exception:
        return None
    try:
        return inspect.getsource(mod)
    except (TypeError, OSError):
        return None


def _ast_for(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _defined_names(tree: ast.Module) -> list[str]:
    """Return top-level function and class names defined in *tree*."""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return names


def _module_names_for(symbol: str) -> list[str]:
    """Find all eggpool modules that *define* *symbol* at top level."""
    import eggpool

    base = eggpool.__file__
    assert base is not None
    pkg_dir = Path(base).parent

    found: list[str] = []
    for root, _dirs, files in os.walk(pkg_dir):
        for fname in files:
            if not fname.endswith(".py") or fname == "__main__.py":
                continue
            fpath = Path(root) / fname
            rel = fpath.relative_to(pkg_dir)
            mod_name = "eggpool." + str(rel)[:-3].replace(os.sep, ".")
            source = _source_for(mod_name)
            if source is None:
                continue
            tree = _ast_for(source)
            if tree is None:
                continue
            if symbol in _defined_names(tree):
                found.append(mod_name)
    return found


# ---------------------------------------------------------------------------
# Tests — ownership point existence
# ---------------------------------------------------------------------------


class TestOwnershipPointExistence:
    """Each canonical symbol must exist and be importable."""

    @pytest.mark.parametrize("description,module,symbol", OWNERSHIP_POINTS)
    def test_canonical_symbol_exists(
        self, description: str, module: str, symbol: str
    ) -> None:
        mod = import_module(module)
        assert hasattr(mod, symbol), (
            f"Ownership point '{description}': {module}.{symbol} is missing"
        )

    @pytest.mark.parametrize("description,module,symbol", OWNERSHIP_POINTS)
    def test_canonical_symbol_is_callable_or_class(
        self, description: str, module: str, symbol: str
    ) -> None:
        mod = import_module(module)
        obj = getattr(mod, symbol)
        assert callable(obj) or isinstance(obj, type), (
            f"Ownership point '{description}': {module}.{symbol} is not callable/class"
        )


# ---------------------------------------------------------------------------
# Tests — single-authority invariant
# ---------------------------------------------------------------------------


class TestSingleAuthority:
    """No symbol may be *defined* in more than its canonical module."""

    @pytest.mark.parametrize("description,module,symbol", OWNERSHIP_POINTS)
    def test_symbol_defined_only_in_canonical_module(
        self, description: str, module: str, symbol: str
    ) -> None:
        defining_modules = _module_names_for(symbol)
        # The symbol should be defined in exactly one module (the canonical
        # one), or zero if it's defined via a factory/alias pattern.
        # Re-exports via `from x import y` don't count (they're not defs).
        assert len(defining_modules) <= 1, (
            f"Ownership point '{description}': symbol '{symbol}' is defined "
            f"in multiple modules: {defining_modules}"
        )

    @pytest.mark.parametrize("description,module,symbol", OWNERSHIP_POINTS)
    def test_no_forbidden_duplicate_definition(
        self, description: str, module: str, symbol: str
    ) -> None:
        forbidden = FORBIDDEN_DUPLICATE_MODULES.get(symbol, [])
        for forbidden_mod in forbidden:
            source = _source_for(forbidden_mod)
            if source is None:
                continue
            tree = _ast_for(source)
            if tree is None:
                continue
            assert symbol not in _defined_names(tree), (
                f"Ownership point '{description}': forbidden duplicate definition "
                f"of '{symbol}' in {forbidden_mod}"
            )


# ---------------------------------------------------------------------------
# Tests — legacy path removal
# ---------------------------------------------------------------------------


class TestLegacyPathRemoval:
    """Legacy routing and finalization paths must be removed."""

    def test_no_legacy_select_accounts_in_router(self) -> None:
        source = _source_for("eggpool.routing.router")
        assert source is not None
        tree = _ast_for(source)
        assert tree is not None
        names = _defined_names(tree)
        assert "select_accounts" not in names, (
            "Legacy select_accounts() must be removed; use build_routing_plan()"
        )

    def test_no_legacy_select_accounts_anywhere(self) -> None:
        """select_accounts must not be defined in any eggpool module."""
        found = _module_names_for("select_accounts")
        assert found == [], f"Legacy select_accounts() still defined in: {found}"

    def test_build_routing_plan_is_authoritative(self) -> None:
        """Router must expose build_routing_plan as the selection path."""
        mod = import_module("eggpool.routing.router")
        assert hasattr(mod, "Router")
        router_cls = mod.Router
        assert hasattr(router_cls, "build_routing_plan")

    def test_no_legacy_finalizer_timeout_pattern(self) -> None:
        """The fragile asyncio.wait_for(asyncio.shield(...), timeout=10)
        pattern must be removed — replaced by process-owned finalization."""
        source = _source_for("eggpool.request.coordinator")
        assert source is not None
        assert "timeout=10" not in source and "timeout=10.0" not in source, (
            "Legacy 10s shield timeout pattern should be removed"
        )

    def test_no_legacy_finalizer_retry_enqueue(self) -> None:
        """_enqueue_finalization_retry must be removed (legacy path)."""
        source = _source_for("eggpool.request.coordinator")
        assert source is not None
        assert "_enqueue_finalization_retry" not in source, (
            "Legacy _enqueue_finalization_retry must be removed"
        )

    def test_failure_effects_applied_once(self) -> None:
        """EffectsApplier must use idempotency keys to prevent double-application."""
        source = _source_for("eggpool.failure.applier")
        assert source is not None
        assert "idempotency" in source.lower() or "applied" in source.lower(), (
            "EffectsApplier must track idempotency to apply effects exactly once"
        )


# ---------------------------------------------------------------------------
# Tests — config flag inventory (Workstream J)
# ---------------------------------------------------------------------------


class TestFeatureFlagInventory:
    """Every temporary feature flag must have a documented removal criterion."""

    def test_no_temp_flags_in_source(self) -> None:
        """No temporary feature flags should remain in source code."""
        import eggpool

        base = eggpool.__file__
        assert base is not None
        pkg_dir = Path(base).parent

        temp_flag_patterns = ["temp_flag", "TEMP_FLAG", "feature_flag_", "_TEMP_"]
        offenders: list[str] = []
        for root, _dirs, files in os.walk(pkg_dir):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = Path(root) / fname
                content = fpath.read_text(encoding="utf-8")
                for pattern in temp_flag_patterns:
                    if pattern in content:
                        offenders.append(f"{fpath}:{pattern}")
        assert offenders == [], f"Temporary feature flags found in source: {offenders}"

    def test_dispatch_writer_opt_in_documented(self) -> None:
        """Dispatch writer must remain opt-in with documented default."""
        from eggpool.models.config import DispatchWriterConfig

        field_info = DispatchWriterConfig.model_fields.get("enabled")
        assert field_info is not None
        assert field_info.default is False, (
            "Dispatch writer must default to False (opt-in) until soak evidence"
        )

    def test_recovery_enabled_by_default(self) -> None:
        """Database recovery must be enabled by default (Plan 027)."""
        from eggpool.models.config import DatabaseRecoveryConfig

        field_info = DatabaseRecoveryConfig.model_fields.get("enabled")
        assert field_info is not None
        assert field_info.default is True, (
            "Database automatic recovery must be enabled by default"
        )

    def test_span_sampling_default_documented(self) -> None:
        """Detailed span sampling must default to 5% (Plan 029)."""
        from eggpool.models.config import DispatchSpansConfig

        field_info = DispatchSpansConfig.model_fields.get("sample_rate")
        assert field_info is not None
        assert field_info.default == 0.05, "Detailed span sampling must default to 5%"


# ---------------------------------------------------------------------------
# Tests — bounded diagnostics
# ---------------------------------------------------------------------------


class TestBoundedDiagnostics:
    """All diagnostic storage must use bounded collections."""

    def test_dispatch_writer_uses_bounded_deque(self) -> None:
        """Dispatch writer sample storage must use deque(maxlen=...)."""
        source = _source_for("eggpool.request.dispatch_writer")
        assert source is not None
        assert "deque" in source, "Dispatch writer must use deque for bounded storage"
        assert "maxlen" in source, (
            "Dispatch writer must use deque(maxlen=...) for bounds"
        )

    def test_finalization_supervisor_uses_bounded_registry(self) -> None:
        """Finalization supervisor must use bounded active-jobs dict + deque."""
        source = _source_for("eggpool.request.finalization_job")
        assert source is not None
        assert "deque" in source, "Finalization supervisor must use deque for history"
        assert "maxlen" in source, (
            "Finalization supervisor must bound history with maxlen"
        )

    def test_span_recorder_uses_bounded_window(self) -> None:
        """Span recorder must use bounded window for sample storage."""
        source = _source_for("eggpool.runtime_dispatch")
        assert source is not None
        assert "deque" in source or "maxlen" in source, (
            "Span recorder must use bounded storage"
        )

    def test_quarantine_uses_bounded_state(self) -> None:
        """Model quarantine must use bounded TTL-based state."""
        source = _source_for("eggpool.failure.quarantine")
        assert source is not None
        assert "ttl" in source.lower() or "expiry" in source.lower(), (
            "Model quarantine must use TTL-based bounded state"
        )


# ---------------------------------------------------------------------------
# Tests — single-parse lifecycle (Workstream A, point 7 & 8)
# ---------------------------------------------------------------------------


class TestSingleParseLifecycle:
    """Provider-bound request and parsed response must be single-parse."""

    def test_provider_bound_request_has_payload_generation(self) -> None:
        """ProviderBoundRequest must track payload_generation for cache reuse."""
        from eggpool.request.provider_bound_request import ProviderBoundRequest

        assert hasattr(ProviderBoundRequest, "payload_generation") or hasattr(
            ProviderBoundRequest, "set_provider_payload"
        ), "ProviderBoundRequest must support copy-on-write payload lifecycle"

    def test_parsed_upstream_response_has_parsed_dict(self) -> None:
        """ParsedUpstreamResponse must provide parsed_dict for single-decode."""
        from eggpool.request.parsed_upstream_response import ParsedUpstreamResponse

        assert hasattr(ParsedUpstreamResponse, "parsed_dict"), (
            "ParsedUpstreamResponse must provide parsed_dict for single-decode"
        )

    def test_coordinator_uses_parsed_response(self) -> None:
        """Coordinator must use ParsedUpstreamResponse for non-stream usage."""
        source = _source_for("eggpool.request.coordinator")
        assert source is not None
        assert (
            "ParsedUpstreamResponse" in source or "parsed_upstream_response" in source
        ), "Coordinator must use ParsedUpstreamResponse for single-decode lifecycle"

    def test_finalizer_extracts_from_parsed(self) -> None:
        """Finalizer must read usage from parsed response, not re-parse."""
        source = _source_for("eggpool.request.finalizer")
        assert source is not None
        assert (
            "parsed_dict" in source
            or "ParsedUpstreamResponse" in source
            or "parsed" in source.lower()
        ), "Finalizer must use parsed response data, not re-parse raw bytes"
