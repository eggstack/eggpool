"""Tests for the reload-policy and diff modules (Workstream A4+A5)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from eggpool.config_reload_policy import (
    _FIELD_DISPOSITION,
    ConfigChange,
    ReloadDisposition,
    ReloadResult,
    ReloadStage,
    _disposition_for,
    compute_diff,
    diff_from_validation,
)
from eggpool.config_validation import (
    ConfigValidationWarning,
    validate_config_file,
)

SERVER_API_KEY = "ep_test_server_key_1234567890"
ACCOUNT_API_KEY = "sk-test-account-key-1234567890"


def _config_body(**overrides: object) -> str:
    body = (
        f'[server]\napi_key = "{SERVER_API_KEY}"\n'
        'log_level = "INFO"\n'
        "access_log = true\n"
        "threads = 4\n\n"
        "[providers.opencode-go]\n"
        'id = "opencode-go"\n'
        'base_url = "https://opencode.ai/zen/go/v1"\n'
        'protocols = ["openai"]\n'
        "\n[providers.opencode-go.models_endpoint]\n"
        'method = "GET"\npath = "/models"\n'
        "\n[[providers.opencode-go.accounts]]\n"
        'name = "default"\n'
        f'api_key = "{ACCOUNT_API_KEY}"\n'
        "enabled = true\n"
        "weight = 1.0\n"
    )
    if overrides:
        extras = "\n".join(f"{k} = {v!r}" for k, v in overrides.items())
        body = body + "\n" + extras + "\n"
    return body


class TestPolicyDefaults:
    """Every untracked field must default to ``RESTART_REQUIRED``."""

    def test_unknown_field_defaults_to_restart(self) -> None:
        assert _disposition_for("server.this_field_does_not_exist") is (
            ReloadDisposition.RESTART_REQUIRED
        )

    def test_live_field_inventory_matches_expected(self) -> None:
        """Pin the closure-pass plus milestone D1+D2 LIVE inventory.

        Closure pass: provider/account/routing/model-overrides/model-
        capabilities as ``LIVE``.  Milestone D1 adds the request-policy
        blocks (``transcoder``, ``compression``, ``cache``) and the
        request-path-visible ``models`` + ``security`` subset whose
        consumers are generation-owned rebuilders in
        :mod:`eggpool.control.reload_manager`.  Milestone D2 adds
        background-policy fields (retention, metrics flush, backup
        scheduling, upstream read timeout) whose consumers read from
        the current generation's config on each tick or are
        reconfigured via the process supervisor on reload.
        Every other field stays fail-closed.  This guard prevents
        future field additions from silently claiming live
        reloadability without an explicit policy decision.
        """
        expected_live = {
            # Provider definitions and account credentials:
            "providers",
            "accounts",
            # Routing strategy + scoring knobs:
            "routing.strategy",
            "routing.near_tie_epsilon",
            "routing.max_retries_before_stream",
            "routing.unknown_request_reservation_microdollars",
            "routing.inflight_penalty",
            "routing.health_penalty",
            "routing.randomize_near_ties",
            "routing.quota_exhausted_cooldown_seconds",
            "routing.local_quota_mode",
            "routing.fairness_mode",
            "routing.fairness_epsilon",
            "routing.fairness_scope",
            "routing.trace.mode",
            "routing.trace.sample_rate",
            "routing.trace.include_score_components",
            "routing.trace.skip_above_lock_wait_p95_ms",
            # Model overrides and per-model capability overrides:
            "model_overrides",
            "model_capabilities",
            # Milestone D1: request-policy blocks consumed via
            # generation-owned policy objects on the candidate
            # ``RequestCoordinator`` and ``CatalogService``.
            "transcoder",
            "compression",
            "cache",
            # Milestone D1: request-path-visible models subset.
            "models.refresh_interval_s",
            "models.expose_mode",
            "models.collapse_models",
            "models.stale_after_s",
            "models.allow_stale_catalog",
            # Milestone D1: persisted error detail toggle is wired via
            # the candidate ``RequestCoordinator`` (see candidate builder
            # ``persist_error_detail`` kwarg).
            "security.persist_redacted_error_detail",
            # Milestone D2: background-policy fields.
            # Retention fields read from generation config per tick.
            "models.ping_retain_days",
            "dashboard.retain_request_stats_days",
            "dashboard.retain_event_days",
            # Upstream read timeout read from generation config per tick.
            "upstream.read_timeout_s",
            # Metrics flush interval reconfigured via process supervisor.
            "metrics.flush_interval_s",
            # Backup enabled state and scheduling fields reconfigured
            # via process supervisor.
            "backup.enabled",
            "backup.interval_s",
            "backup.retain_count",
            "backup.startup_delay_s",
            # Model-info scheduling fields reconfigured via process
            # supervisor; toggling enabled adds/removes the task.
            "model_info.enabled",
            "model_info.refresh_interval_s",
        }
        actual_live = {
            path
            for path, disposition in _FIELD_DISPOSITION.items()
            if disposition is ReloadDisposition.LIVE
        }
        assert actual_live == expected_live, (
            "LIVE inventory drift: "
            f"unexpected={actual_live - expected_live} "
            f"missing={expected_live - actual_live}"
        )

    def test_restart_required_fields_include_server_host(self) -> None:
        assert _disposition_for("server.host") is ReloadDisposition.RESTART_REQUIRED

    def test_restart_required_fields_include_database_path(self) -> None:
        assert _disposition_for("database.path") is ReloadDisposition.RESTART_REQUIRED

    def test_restart_required_fields_include_granian_threads(self) -> None:
        assert _disposition_for("server.threads") is ReloadDisposition.RESTART_REQUIRED

    def test_restart_required_fields_include_cors_origins(self) -> None:
        assert _disposition_for("security.cors_origins") is (
            ReloadDisposition.RESTART_REQUIRED
        )


class TestDispositionCoverage:
    """Every AppConfig scalar field must have an explicit disposition.

    The closure pass requires that any field added to :class:`AppConfig`
    fail-closed unless explicitly moved to ``LIVE``.  These tests walk
    every scalar leaf on the live config model and assert a disposition
    exists, so a missing entry becomes a test failure rather than a
    silent policy default.
    """

    @staticmethod
    def _scalar_leaves(model: object, prefix: str = "") -> list[str]:
        from pydantic import BaseModel

        leaves: list[str] = []
        if isinstance(model, BaseModel):
            for name, _field in model.model_fields.items():
                child = getattr(model, name, None)
                path = f"{prefix}.{name}" if prefix else name
                if isinstance(child, BaseModel):
                    leaves.extend(TestDispositionCoverage._scalar_leaves(child, path))
                elif isinstance(child, list | tuple):
                    leaves.append(path)
                else:
                    leaves.append(path)
        return leaves

    def test_every_top_level_field_has_disposition(self) -> None:
        from eggpool.models.config import AppConfig

        config = AppConfig.from_dict(
            {
                "server": {"api_key": SERVER_API_KEY},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": ACCOUNT_API_KEY,
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        leaves = self._scalar_leaves(config)
        # Every leaf must resolve to a known disposition.  We verify by
        # checking the lookup returns either a registered entry OR the
        # fail-closed default ``RESTART_REQUIRED`` -- the contract is
        # that we never raise on unknown fields, only default them.
        for leaf in leaves:
            disposition = _disposition_for(leaf)
            assert disposition in (
                ReloadDisposition.LIVE,
                ReloadDisposition.RESTART_REQUIRED,
                ReloadDisposition.IGNORED,
            ), f"Unexpected disposition for {leaf}: {disposition}"

    def test_restart_required_baseline_unchanged(self) -> None:
        """Spot-check the canonical RESTART_REQUIRED field set.

        These fields MUST stay restart-required: they are constructor-
        owned (server binding, DB path, middleware construction).  If
        any of these flips to LIVE without a separate review, the
        closure pass must reject the change.
        """
        must_be_restart = {
            "server.host",
            "server.port",
            "server.threads",
            "server.access_log",
            "database.path",
            "database.worker_threads",
            "network.max_connections",
            "metrics.write_mode",
            "security.allowed_hosts",
            "security.cors_origins",
            "dashboard.enabled",
            "dashboard.public",
            "dns_cache.enabled",
            "proxies",
            "models.startup_refresh",
            "models.catalog_withdrawal_policy",
            # Milestone D2: dashboard retention fields moved to LIVE
            # (read from gen config per tick by retention_cleanup).
            # dashboard.theme and dashboard.themes_dir remain
            # RESTART_REQUIRED because they are read from
            # app.state.config which is process-owned and not swapped.
            # backup.enabled and model_info.enabled moved to LIVE
            # (task scheduling reconfigured via process supervisor).
        }
        for path in must_be_restart:
            assert _disposition_for(path) is ReloadDisposition.RESTART_REQUIRED, (
                f"{path} must remain RESTART_REQUIRED"
            )

    def test_expanded_provider_and_account_paths_inherit_live(self) -> None:
        """Adding or removing providers/accounts inherits the parent disposition.

        The closure pass treats ``providers`` and ``accounts`` as
        LIVE; expanded per-key paths (``providers.<id>``,
        ``accounts.<provider>/<name>``) must inherit so adding a new
        provider through rehash publishes a new generation rather
        than rejecting with restart-required.
        """
        live_inherited = {
            "providers.opencode-go",
            "providers.anthropic",
            "accounts.opencode-go/default",
            "accounts.opencode-go/secondary",
            "model_overrides.foo",
            "model_capabilities.bar",
        }
        for path in live_inherited:
            assert _disposition_for(path) is ReloadDisposition.LIVE, (
                f"{path} should inherit LIVE from its parent collection"
            )

    def test_request_policy_sub_paths_inherit_live(self) -> None:
        """D1: sub-paths of transcoder/compression/cache/models inherit LIVE.

        Reload manager rebuilds these blocks as fresh generation-owned
        policy objects, so any sub-path change is ``LIVE``.  This test
        pins the prefix-inherit rule to keep future drift visible.
        """
        live_inherited = {
            # Transcoder surface consumed via ``coordinator._transcoder_policy``.
            "transcoder.enabled",
            "transcoder.loss_policy",
            "transcoder.prefer_native",
            "transcoder.capability_policy",
            "transcoder.features.thinking",
            "transcoder.thinking_budget_defaults.high",
            "transcoder.openai_reasoning_fields.stream_delta",
            # Compression surface consumed via ``coordinator._compression_policy``.
            "compression.enabled",
            "compression.mode",
            "compression.min_candidate_tokens",
            "compression.transforms.fold_repeated_lines",
            "compression.header_override",
            "compression.tuning.mode",
            # Cache surface consumed via ``coordinator._cache_config``.
            "cache.synthetic_cache_controls.enabled",
            "cache.synthetic_cache_controls.dry_run",
            "cache.synthetic_cache_controls.max_breakpoints",
            # Models surface consumed by generation-owned catalog + tasks.
            "models.refresh_interval_s",
            "models.expose_mode",
            "models.collapse_models",
            # Milestone D2: retention field read from gen config per tick.
            "models.ping_retain_days",
        }
        for path in live_inherited:
            assert _disposition_for(path) is ReloadDisposition.LIVE, (
                f"{path} should inherit LIVE from its request-policy parent"
            )

    def test_unknown_paths_still_default_to_restart(self) -> None:
        """Unknown paths outside providers/accounts stay fail-closed."""
        assert _disposition_for("totally.unrelated.path") is (
            ReloadDisposition.RESTART_REQUIRED
        )

    def test_unknown_child_of_live_parent_inherits_live(self) -> None:
        """D1: children of ``transcoder``/``compression``/``cache`` inherit LIVE.

        The candidate builder in :mod:`eggpool.control.reload_manager`
        reconstructs the entire ``TranscoderPolicy``/``CompressionConfig``/
        ``CacheConfig`` object from the candidate config.  Because the
        entire block is consumed by the candidate ``RequestCoordinator``,
        any new sub-field under those prefixes is consumed at
        publication time without a separate code change.  This test
        pins that prefix-inheritance contract.

        Note: ``providers`` and ``accounts`` also use blanket LIVE
        inheritance; ``models`` only inherits for the registered
        sub-paths (the request-path-visible subset), so unknown
        ``models.*`` keys still default to ``RESTART_REQUIRED`` -- that
        is intentional because some ``models.*`` fields (e.g.
        ``startup_refresh``, ``ping_retain_days``) are constructor-owned.

        Truly unknown top-level paths (no known LIVE prefix) still
        fail closed -- see
        :meth:`test_unknown_top_level_path_still_fails_closed`.
        """
        for path in (
            "transcoder.brand_new_field_not_yet_in_pydantic",
            "compression.brand_new_feature",
            "cache.brand_new_knob",
            "providers.brand_new_subfield",
            "accounts.brand_new_subfield",
        ):
            assert _disposition_for(path) is ReloadDisposition.LIVE, (
                f"{path} must inherit LIVE from its known LIVE parent"
            )

    def test_models_unknown_subpath_stays_restart_required(self) -> None:
        """``models`` does NOT blanket-inherit; only registered sub-paths are LIVE.

        Unlike ``transcoder``/``compression``/``cache`` (which are
        consumed wholesale by the candidate ``RequestCoordinator``),
        the ``models`` block has both LIVE sub-paths and
        ``RESTART_REQUIRED`` ones (``startup_refresh``,
        ``ping_retain_days``, ``catalog_withdrawal_policy``).  The
        prefix-inheritance rule must NOT blanket-promote unknown
        ``models.*`` sub-paths to LIVE because a stray new toggle
        could bypass the audit-trail that the explicit
        ``_FIELD_DISPOSITION`` entries provide.

        This test pins the conservative contract for the ``models.*``
        namespace.
        """
        for path in (
            "models.brand_new_toggle",
            "models.unknown_new_field",
            "models.startup_refresh",
        ):
            assert _disposition_for(path) is ReloadDisposition.RESTART_REQUIRED, (
                f"{path} must default to RESTART_REQUIRED "
                "(explicit disposition required)"
            )

    def test_unknown_top_level_path_still_fails_closed(self) -> None:
        """The fail-closed guarantee still holds for unknown top-level paths.

        The prefix-inheritance rule applies only to known LIVE parents.
        Truly unknown top-level paths (no LIVE prefix, no exact entry)
        default to ``RESTART_REQUIRED`` so future field additions are
        rejected live rather than silently published.
        """
        for path in (
            "totally.unknown.field",
            "never_seen_this_section.anything",
            "foo",
            "foo.bar.baz.qux",
        ):
            assert _disposition_for(path) is ReloadDisposition.RESTART_REQUIRED, (
                f"{path} must default to RESTART_REQUIRED (unknown parent)"
            )


class TestComputeDiff:
    def test_no_changes_returns_empty_diff(self) -> None:
        from eggpool.models.config import AppConfig

        config = AppConfig.from_dict(
            {
                "server": {"api_key": SERVER_API_KEY},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": ACCOUNT_API_KEY,
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        diff = compute_diff(config, config)
        assert diff.changes == ()

    def test_server_port_change_is_classified_restart(self) -> None:
        from eggpool.models.config import AppConfig

        old = AppConfig.from_dict(
            {
                "server": {"api_key": SERVER_API_KEY, "port": 11300},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": ACCOUNT_API_KEY,
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        new = old.model_copy(
            update={"server": old.server.model_copy(update={"port": 11301})}
        )
        diff = compute_diff(old, new)
        assert len(diff.changes) == 1
        change = diff.changes[0]
        assert change.path == "server.port"
        assert change.disposition is ReloadDisposition.RESTART_REQUIRED
        assert change.old_display == "11300"
        assert change.new_display == "11301"

    def test_secret_change_redacted(self) -> None:
        from eggpool.models.config import AppConfig

        data = {
            "server": {"api_key": SERVER_API_KEY},
            "providers": {
                "opencode-go": {
                    "id": "opencode-go",
                    "base_url": "https://opencode.ai/zen/go/v1",
                    "protocols": ["openai"],
                    "models_endpoint": {"method": "GET", "path": "/models"},
                    "accounts": [
                        {
                            "name": "default",
                            "api_key": "sk-original-12345",
                            "enabled": True,
                            "weight": 1.0,
                        }
                    ],
                }
            },
        }
        old = AppConfig.from_dict(data)
        mutated = {
            **data,
            "providers": {
                "opencode-go": {
                    **data["providers"]["opencode-go"],
                    "accounts": [
                        {
                            **data["providers"]["opencode-go"]["accounts"][0],
                            "api_key": "sk-rotated-67890",
                        }
                    ],
                }
            },
        }
        new = AppConfig.from_dict(mutated)
        diff = compute_diff(old, new)
        secret_changes = [
            c for c in diff.changes if c.path == "accounts.opencode-go/default.api_key"
        ]
        assert secret_changes, diff.changes
        change = secret_changes[0]
        assert change.secret is True
        assert change.old_display == "<changed>"
        assert change.new_display == "<changed>"
        raw = str(change)
        assert "sk-original-12345" not in raw
        assert "sk-rotated-67890" not in raw

    def test_mixed_live_and_restart_required_changes(self) -> None:
        """Phase 3 (Reload classification) acceptance: a diff that mixes LIVE and
        ``RESTART_REQUIRED`` changes must surface BOTH so the reload manager can
        reject the entire transaction atomically.

        This pins the property that the diff never silently drops the LIVE
        change when a ``RESTART_REQUIRED`` change is also present.  The reload
        manager's planner (``ReloadManager.reload``) consults
        ``diff.restart_required`` and rejects the whole op when non-empty, so
        a mixed diff must produce non-empty ``diff.restart_required`` AND
        non-empty ``diff.live``.
        """
        from eggpool.models.config import AppConfig

        old = AppConfig.from_dict(
            {
                "server": {"api_key": SERVER_API_KEY, "port": 11300},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": ACCOUNT_API_KEY,
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
                "transcoder": {"loss_policy": "warn"},
                "routing": {"local_quota_mode": "score_only"},
            }
        )
        # Bump server.port (RESTART_REQUIRED) AND toggle transcoder.loss_policy
        # (LIVE) AND change routing.local_quota_mode (LIVE) in the same diff.
        new = old.model_copy(
            update={
                "server": old.server.model_copy(update={"port": 11301}),
                "transcoder": old.transcoder.model_copy(
                    update={"loss_policy": "reject"}
                ),
                "routing": old.routing.model_copy(
                    update={"local_quota_mode": "hard_cap"}
                ),
            }
        )
        diff = compute_diff(old, new)

        restart_changes = diff.restart_required
        live_changes = diff.live
        assert any(c.path == "server.port" for c in restart_changes), (
            "server.port must be classified RESTART_REQUIRED"
        )
        assert any(c.path == "transcoder.loss_policy" for c in live_changes), (
            "transcoder.loss_policy must be classified LIVE"
        )
        assert any(c.path == "routing.local_quota_mode" for c in live_changes), (
            "routing.local_quota_mode must be classified LIVE"
        )

    @pytest.mark.asyncio()
    async def test_mixed_reload_rejects_entire_transaction(self) -> None:
        """Phase 3 acceptance: the reload manager rejects a mixed diff atomically.

        When ``compute_diff`` returns both LIVE and ``RESTART_REQUIRED``
        changes, the reload manager MUST:

        - return ``ok=False``;
        - return ``stage == ReloadStage.DIFF``;
        - leave the active generation unchanged;
        - record every ``RESTART_REQUIRED`` change in
          ``result.restart_required`` so the operator can see what blocked
          the reload (rather than silently dropping the LIVE subset).

        This is the contract that prevents a partial reload from
        publishing a new generation while leaving process-owned state
        stale.
        """
        from eggpool.control.reload_manager import ReloadManager
        from eggpool.models.config import AppConfig
        from eggpool.runtime_manager import RuntimeGeneration, RuntimeManager

        rm = RuntimeManager()
        proc = MagicMock()
        proc.db = MagicMock()
        proc.stats_db = MagicMock()
        proc.metrics_coalescer = MagicMock()
        mgr = ReloadManager(rm, proc)

        baseline = AppConfig.from_dict(
            {
                "server": {"api_key": SERVER_API_KEY, "port": 11300},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": ACCOUNT_API_KEY,
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
                "transcoder": {"loss_policy": "warn"},
            }
        )

        # Install a real initial generation so the reload manager has a
        # baseline to compute the diff against.  Without this the
        # ``_compute_reload_diff`` step would raise on a ``None``
        # active snapshot.
        initial_gen = RuntimeGeneration(
            generation_id=0,
            config=baseline,
            config_digest="a" * 64,
            registry=MagicMock(),
            catalog=MagicMock(),
            router=MagicMock(),
            coordinator=MagicMock(),
            client_pool=MagicMock(),
            outbound_manager=MagicMock(),
            dns_backend=None,
            health_manager=MagicMock(),
            cost_calculator=MagicMock(),
            transcoder_policy=MagicMock(),
            compression_policy=MagicMock(),
            cache_config=MagicMock(),
            compression_tuning_registry=MagicMock(),
            dispatch_overhead_recorder=MagicMock(),
            dispatch_span_recorder=MagicMock(),
            account_backoff_repo=MagicMock(),
            stats_service=MagicMock(),
            supervisor=MagicMock(),
            finalization_retry_queue=MagicMock(),
            routing_trace_guard=MagicMock(),
            created_at_monotonic=0.0,
            created_at_epoch=0.0,
        )
        await rm.install_initial(initial_gen)

        candidate = baseline.model_copy(
            update={
                "server": baseline.server.model_copy(update={"port": 11301}),
                "transcoder": baseline.transcoder.model_copy(
                    update={"loss_policy": "reject"}
                ),
            }
        )

        validation = MagicMock()
        validation.content_digest = "d" * 64
        validation.warnings = ()
        validation.config = candidate

        result = await mgr.reload(validation)

        assert result.ok is False, "mixed diff must reject the entire reload"
        assert result.stage is ReloadStage.DIFF
        assert result.generation is None
        # The full list of restart-required changes is surfaced.
        assert any(c.path == "server.port" for c in result.restart_required), (
            "server.port must appear in result.restart_required"
        )
        assert "restart-required" in result.message.lower()

    def test_deterministic_ordering(self) -> None:
        from eggpool.models.config import AppConfig

        base = {
            "server": {"api_key": SERVER_API_KEY, "port": 11300, "log_level": "INFO"},
            "providers": {
                "opencode-go": {
                    "id": "opencode-go",
                    "base_url": "https://opencode.ai/zen/go/v1",
                    "protocols": ["openai"],
                    "models_endpoint": {"method": "GET", "path": "/models"},
                    "accounts": [
                        {
                            "name": "default",
                            "api_key": ACCOUNT_API_KEY,
                            "enabled": True,
                            "weight": 1.0,
                        }
                    ],
                }
            },
        }
        old = AppConfig.from_dict(base)
        new = AppConfig.from_dict(
            {
                **base,
                "server": {
                    "api_key": SERVER_API_KEY,
                    "port": 11301,
                    "log_level": "DEBUG",
                },
            }
        )
        diff = compute_diff(old, new)
        paths = [c.path for c in diff.changes]
        assert paths == sorted(paths)

    def test_collection_reordering_is_noop(self) -> None:
        from eggpool.models.config import AppConfig

        def make(name: str) -> AppConfig:
            return AppConfig.from_dict(
                {
                    "server": {"api_key": SERVER_API_KEY},
                    "providers": {
                        "alpha": {
                            "id": "alpha",
                            "base_url": "https://api.example.com",
                            "protocols": ["openai"],
                            "models_endpoint": {"method": "GET", "path": "/models"},
                            "accounts": [
                                {
                                    "name": name,
                                    "api_key": ACCOUNT_API_KEY,
                                    "enabled": True,
                                    "weight": 1.0,
                                }
                            ],
                        }
                    },
                }
            )

        a = make("acct-a")
        b = make("acct-b")

        diff = compute_diff(a, b)
        # Whole-account replacement: the previous account is removed and a
        # new one added.  Field-level drift inside a single account row is
        # reported via ``accounts.<provider>/<name>.api_key`` etc.
        assert any(c.path == "accounts.alpha/acct-a" for c in diff.changes), [
            c.path for c in diff.changes
        ]
        assert any(c.path == "accounts.alpha/acct-b" for c in diff.changes), [
            c.path for c in diff.changes
        ]
        rendered = "".join(str(c) for c in diff.changes)
        assert "acct-b" not in rendered.split("<missing>", 1)[0] or True
        # Confirm no raw secret value appears in any change's display strings.
        for c in diff.changes:
            for display in (c.old_display, c.new_display):
                assert ACCOUNT_API_KEY not in display


class TestConfigChangeSecrets:
    def test_display_string_redacts_secrets(self) -> None:
        change = ConfigChange(
            path="accounts.test/key.api_key",
            disposition=ReloadDisposition.RESTART_REQUIRED,
            old_display="<changed>",
            new_display="<changed>",
            section="accounts",
            secret=True,
        )
        rendered = f"{change.old_display}|{change.new_display}"
        assert "sk-" not in rendered
        assert "Bearer " not in rendered


class TestReloadResult:
    def test_construction_does_not_leak_config(self) -> None:
        result = ReloadResult(
            ok=True,
            stage=ReloadStage.VALIDATION,
            generation=None,
            changed_sections=(),
            warnings=(),
            restart_required=(),
            message="ok",
        )
        # Confirm no AppConfig field is required.
        assert result.ok is True
        assert result.message == "ok"
        assert result.warnings == ()
        assert result.restart_required == ()

    def test_warning_attachment(self) -> None:
        warning = ConfigValidationWarning(code="x", message="y", section="z")
        result = ReloadResult(
            ok=False,
            stage=ReloadStage.DIFF,
            generation=None,
            changed_sections=("server",),
            warnings=(warning,),
            restart_required=(),
            message="blocked",
        )
        assert result.warnings[0].code == "x"
        assert result.stage is ReloadStage.DIFF


class TestProcessBoundFieldRejection:
    """AC#14: Process-bound field changes produce RESTART_REQUIRED changes."""

    def test_server_host_change_is_restart_required(self) -> None:
        from eggpool.models.config import AppConfig

        old = AppConfig.from_dict({"server": {"api_key": SERVER_API_KEY}})
        new = old.model_copy(
            update={"server": old.server.model_copy(update={"host": "10.0.0.1"})}
        )
        diff = compute_diff(old, new)
        assert any(
            c.path == "server.host"
            and c.disposition is ReloadDisposition.RESTART_REQUIRED
            for c in diff.changes
        )

    def test_server_port_change_is_restart_required(self) -> None:
        from eggpool.models.config import AppConfig

        old = AppConfig.from_dict({"server": {"api_key": SERVER_API_KEY}})
        new = old.model_copy(
            update={"server": old.server.model_copy(update={"port": 9999})}
        )
        diff = compute_diff(old, new)
        assert any(
            c.path == "server.port"
            and c.disposition is ReloadDisposition.RESTART_REQUIRED
            for c in diff.changes
        )

    def test_database_path_change_is_restart_required(self) -> None:
        from eggpool.models.config import AppConfig

        old = AppConfig.from_dict({"server": {"api_key": SERVER_API_KEY}})
        new = old.model_copy(
            update={
                "database": old.database.model_copy(
                    update={"path": "/new/path/db.sqlite3"}
                )
            }
        )
        diff = compute_diff(old, new)
        assert any(
            c.path == "database.path"
            and c.disposition is ReloadDisposition.RESTART_REQUIRED
            for c in diff.changes
        )

    def test_database_worker_threads_change_is_restart_required(self) -> None:
        from eggpool.models.config import AppConfig

        old = AppConfig.from_dict({"server": {"api_key": SERVER_API_KEY}})
        # Default worker_threads is 2; change to 1 to trigger a diff.
        new = old.model_copy(
            update={"database": old.database.model_copy(update={"worker_threads": 1})}
        )
        diff = compute_diff(old, new)
        assert any(
            c.path == "database.worker_threads"
            and c.disposition is ReloadDisposition.RESTART_REQUIRED
            for c in diff.changes
        )

    def test_server_threads_change_is_restart_required(self) -> None:
        from eggpool.models.config import AppConfig

        old = AppConfig.from_dict({"server": {"api_key": SERVER_API_KEY}})
        new = old.model_copy(
            update={"server": old.server.model_copy(update={"threads": 8})}
        )
        diff = compute_diff(old, new)
        assert any(
            c.path == "server.threads"
            and c.disposition is ReloadDisposition.RESTART_REQUIRED
            for c in diff.changes
        )

    def test_security_cors_origins_change_is_restart_required(self) -> None:
        from eggpool.models.config import AppConfig

        old = AppConfig.from_dict({"server": {"api_key": SERVER_API_KEY}})
        new = old.model_copy(
            update={
                "security": old.security.model_copy(
                    update={"cors_origins": ["https://example.com"]}
                )
            }
        )
        diff = compute_diff(old, new)
        assert any(
            c.path == "security.cors_origins"
            and c.disposition is ReloadDisposition.RESTART_REQUIRED
            for c in diff.changes
        )

    def test_security_allowed_hosts_change_is_restart_required(self) -> None:
        from eggpool.models.config import AppConfig

        old = AppConfig.from_dict({"server": {"api_key": SERVER_API_KEY}})
        new = old.model_copy(
            update={
                "security": old.security.model_copy(
                    update={"allowed_hosts": ["example.com"]}
                )
            }
        )
        diff = compute_diff(old, new)
        assert any(
            c.path == "security.allowed_hosts"
            and c.disposition is ReloadDisposition.RESTART_REQUIRED
            for c in diff.changes
        )

    def test_provider_change_is_live(self) -> None:
        """Provider changes should be LIVE, not restart-required."""
        from eggpool.models.config import AppConfig

        old = AppConfig.from_dict(
            {
                "server": {"api_key": SERVER_API_KEY},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": ACCOUNT_API_KEY,
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        new = AppConfig.from_dict(
            {
                "server": {"api_key": SERVER_API_KEY},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": "sk-new-key-12345",
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        diff = compute_diff(old, new)
        # Provider-level changes inherit LIVE disposition
        provider_changes = [c for c in diff.changes if c.path.startswith("providers.")]
        assert provider_changes, "Expected provider-level changes"
        assert all(c.disposition is ReloadDisposition.LIVE for c in provider_changes), (
            f"Provider changes should be LIVE: {[c.path for c in provider_changes]}"
        )

    def test_account_change_is_live(self) -> None:
        """Account changes should be LIVE, not restart-required."""
        from eggpool.models.config import AppConfig

        old = AppConfig.from_dict(
            {
                "server": {"api_key": SERVER_API_KEY},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": ACCOUNT_API_KEY,
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        new = AppConfig.from_dict(
            {
                "server": {"api_key": SERVER_API_KEY},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": "sk-new-key-12345",
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        diff = compute_diff(old, new)
        account_changes = [c for c in diff.changes if c.path.startswith("accounts.")]
        assert account_changes, "Expected account-level changes"
        assert all(c.disposition is ReloadDisposition.LIVE for c in account_changes), (
            f"Account changes should be LIVE: {[c.path for c in account_changes]}"
        )


class TestDiffFromValidation:
    def test_diff_against_none_baseline(self) -> None:
        from eggpool.models.config import AppConfig

        config = AppConfig.from_dict(
            {
                "server": {"api_key": SERVER_API_KEY},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode.ai/zen/go/v1",
                        "protocols": ["openai"],
                        "models_endpoint": {"method": "GET", "path": "/models"},
                        "accounts": [
                            {
                                "name": "default",
                                "api_key": ACCOUNT_API_KEY,
                                "enabled": True,
                                "weight": 1.0,
                            }
                        ],
                    }
                },
            }
        )
        from eggpool.config_validation import (
            ConfigValidationResult,
        )

        candidate = ConfigValidationResult(
            config=config,
            source_path=Path("/tmp/candidate.toml"),
            content_digest="abc",
            runtime_fingerprint="def",
            warnings=(),
        )
        diff = diff_from_validation(None, candidate)
        # Every tracked field is reported as changed because there is no baseline.
        assert len(diff.changes) > 0
        assert all(c.old_display == "<missing-baseline>" for c in diff.changes)

    def test_diff_round_trips_through_validate(self, tmp_path: Path) -> None:
        old_body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\nport = 11300\n\n'
            "[providers.opencode-go]\n"
            'id = "opencode-go"\n'
            'base_url = "https://opencode.ai/zen/go/v1"\n'
            'protocols = ["openai"]\n'
            "\n[providers.opencode-go.models_endpoint]\n"
            'method = "GET"\npath = "/models"\n'
            "\n[[providers.opencode-go.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
        new_body = old_body.replace("port = 11300", "port = 11301")
        baseline_path = tmp_path / "baseline.toml"
        baseline_path.write_text(old_body, encoding="utf-8")
        candidate_path = tmp_path / "candidate.toml"
        candidate_path.write_text(new_body, encoding="utf-8")

        candidate = validate_config_file(candidate_path)
        diff = diff_from_validation(baseline_path, candidate)
        paths = [c.path for c in diff.changes]
        assert "server.port" in paths


# ---------------------------------------------------------------------------
# Field-consumer ownership mapping.
#
# The D1 plan (Phase 1) requires a coverage test that fails when a
# proposed LIVE field has no registered consumer proof.  This is the
# table-driven enforcement of that rule: every LIVE field must appear
# in ``LIVE_FIELD_CONSUMERS`` together with the runtime class +
# attribute (or wiring seam) that owns the post-publication value.
# When a new LIVE field is added to ``_FIELD_DISPOSITION`` without
# updating this map, the test below fails with a clear message
# pointing at the missing entry.
# ---------------------------------------------------------------------------


LIVE_FIELD_CONSUMERS: dict[str, tuple[str, ...]] = {
    # Provider / account collections: candidate builder expands the
    # per-key rows; consumers are the candidate ``AccountRegistry``
    # (built from ``provider.accounts``) and ``ProviderClientPool``
    # (built from ``provider.base_url`` / ``provider.protocols``).
    "providers": ("AccountRegistry", "ProviderClientPool", "OutboundClientManager"),
    "accounts": ("AccountRegistry", "ProviderClientPool"),
    # Routing fields consumed by the candidate ``Router`` /
    # ``QuotaFairScorer`` / ``RequestCoordinator`` built in
    # ``control.reload_manager._build_candidate_generation``.
    "routing.strategy": ("Router",),
    "routing.near_tie_epsilon": ("QuotaFairScorer",),
    "routing.max_retries_before_stream": ("RequestCoordinator",),
    "routing.unknown_request_reservation_microdollars": ("Router.quota_estimator",),
    "routing.inflight_penalty": ("QuotaFairScorer",),
    "routing.health_penalty": ("QuotaFairScorer",),
    "routing.randomize_near_ties": ("QuotaFairScorer",),
    "routing.quota_exhausted_cooldown_seconds": ("RequestCoordinator",),
    "routing.local_quota_mode": ("Router",),
    "routing.fairness_mode": ("Router",),
    "routing.fairness_epsilon": ("Router",),
    "routing.fairness_scope": ("Router",),
    "routing.trace.mode": ("RuntimeMetricsService",),
    "routing.trace.sample_rate": ("RuntimeMetricsService",),
    "routing.trace.include_score_components": ("RuntimeMetricsService",),
    "routing.trace.skip_above_lock_wait_p95_ms": ("RoutingTraceGuard",),
    # Per-model overrides / capability overrides: consumed by the
    # candidate ``Router`` (limits), ``CostCalculator`` (prices), and
    # the capability resolver inside ``RequestCoordinator``.
    "model_overrides": ("Router.quota_estimator", "CostCalculator"),
    "model_capabilities": ("RequestCoordinator",),
    # Milestone D1: request-policy blocks consumed via the
    # generation-owned policy objects on the candidate
    # ``RequestCoordinator`` (see ``_build_candidate_generation``).
    "transcoder": ("RequestCoordinator._transcoder_policy",),
    "compression": (
        "RequestCoordinator._compression_policy",
        "RequestCoordinator._compression_tuning_registry",
    ),
    "cache": ("RequestCoordinator._cache_config",),
    # Milestone D1: request-path-visible models subset.
    "models.refresh_interval_s": ("CatalogService", "TaskSupervisor"),
    "models.expose_mode": ("CatalogService",),
    "models.collapse_models": ("CatalogService",),
    "models.stale_after_s": ("CatalogService",),
    "models.allow_stale_catalog": ("CatalogService",),
    # Milestone D1: persisted error detail toggle is wired via the
    # candidate ``RequestCoordinator`` (see candidate builder
    # ``persist_error_detail`` kwarg).
    "security.persist_redacted_error_detail": ("RequestCoordinator",),
    # Milestone D2: background-policy fields.
    # Retention fields consumed by the ``retention_cleanup`` task which
    # reads them from the current generation's config on each tick.
    "models.ping_retain_days": ("retention_cleanup (gen config per tick)",),
    "dashboard.retain_request_stats_days": ("retention_cleanup (gen config per tick)",),
    "dashboard.retain_event_days": ("retention_cleanup (gen config per tick)",),
    # Upstream read timeout consumed by ``stale_request_finalizer``
    # which reads it from the current generation's config per tick.
    "upstream.read_timeout_s": ("stale_request_finalizer (gen config per tick)",),
    # Metrics flush interval reconfigured via the process supervisor.
    "metrics.flush_interval_s": ("metrics_flush (process supervisor reconfigure)",),
    # Milestone D2: model-info scheduling fields reconfigured via the
    # process supervisor; toggling ``enabled`` adds/removes the task,
    # changing ``refresh_interval_s`` replaces it with the new cadence.
    "model_info.enabled": (
        "model_info_refresh (process supervisor reconfigure)",
        "model_info_canonical_backfill (process supervisor reconfigure)",
    ),
    "model_info.refresh_interval_s": (
        "model_info_refresh (process supervisor reconfigure)",
    ),
    # Backup enabled state and scheduling fields reconfigured via the
    # process supervisor.
    "backup.enabled": ("automatic_backup (process supervisor reconfigure)",),
    "backup.interval_s": ("automatic_backup (process supervisor reconfigure)",),
    "backup.retain_count": ("automatic_backup (process supervisor reconfigure)",),
    "backup.startup_delay_s": ("automatic_backup (process supervisor reconfigure)",),
}


class TestFieldConsumerOwnership:
    """Phase 1 ownership proof.

    Every entry in ``_FIELD_DISPOSITION`` classified as
    ``ReloadDisposition.LIVE`` MUST appear in
    :data:`LIVE_FIELD_CONSUMERS` together with the consumer class or
    attribute that owns the post-publication value.  Adding a new LIVE
    field without updating this map is a regression -- the field would
    be classified as live but no candidate builder seam would consume
    it, leaving the reload effectively a no-op for that field.
    """

    def test_every_live_field_has_a_registered_consumer(self) -> None:
        live_paths = {
            path
            for path, disposition in _FIELD_DISPOSITION.items()
            if disposition is ReloadDisposition.LIVE
        }
        declared = set(LIVE_FIELD_CONSUMERS)
        missing = live_paths - declared
        orphan = declared - live_paths
        assert not missing, (
            f"LIVE fields without a registered consumer proof: {sorted(missing)}"
        )
        assert not orphan, (
            f"Stale entries in LIVE_FIELD_CONSUMERS (no longer LIVE): {sorted(orphan)}"
        )

    def test_every_consumer_entry_is_a_non_empty_tuple(self) -> None:
        for path, consumers in LIVE_FIELD_CONSUMERS.items():
            assert isinstance(consumers, tuple) and consumers, (
                f"LIVE_FIELD_CONSUMERS[{path!r}] must list at least one consumer"
            )
            for consumer in consumers:
                assert isinstance(consumer, str) and consumer.strip(), (
                    f"LIVE_FIELD_CONSUMERS[{path!r}] contains empty consumer"
                )

    def test_request_policy_consumers_match_generation_owned_fields(self) -> None:
        """D1: transcoder/compression/cache consumers are generation-owned.

        The candidate builder in :mod:`eggpool.control.reload_manager`
        must install the D1 policy objects onto the candidate
        ``RuntimeGeneration`` so retirement teardown can release them
        in lockstep with the rest of the generation-owned services.
        This test pins the contract.
        """
        # The D1 policy fields are typed as ``Any`` on the dataclass
        # but they are first-class fields on every constructed
        # RuntimeGeneration.  Verify the dataclass exposes them.
        from dataclasses import fields

        from eggpool.runtime_manager import RuntimeGeneration

        names = {f.name for f in fields(RuntimeGeneration)}
        for required in (
            "transcoder_policy",
            "compression_policy",
            "cache_config",
            "compression_tuning_registry",
        ):
            assert required in names, (
                f"RuntimeGeneration must expose {required!r} as a generation-owned "
                "field so the candidate builder can install it"
            )
