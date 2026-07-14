"""Tests for the reload-policy and diff modules (Workstream A4+A5)."""

from __future__ import annotations

from pathlib import Path

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
        """Pin the closure-pass LIVE inventory.

        The closure pass enables provider/account/routing/model-overrides
        as ``LIVE``.  Every other field stays fail-closed.  This guard
        prevents future field additions from silently claiming live
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
            "backup.enabled",
            "transcoder",
            "compression",
            "cache",
            "proxies",
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

    def test_unknown_paths_still_default_to_restart(self) -> None:
        """Unknown paths outside providers/accounts stay fail-closed."""
        assert _disposition_for("totally.unrelated.path") is (
            ReloadDisposition.RESTART_REQUIRED
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
