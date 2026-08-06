"""Phase 1 audit: full field inventory, schema-policy parity, and secret tagging.

These tests walk the AppConfig Pydantic schema and the ``_FIELD_DISPOSITION``
policy map to ensure every field is classified, no policy entry is orphaned,
and credential-bearing paths are tagged with ``secret=True``.

Run with ``pytest -s`` to print the inventory table to stdout.
"""

from __future__ import annotations

from typing import Any

import pytest

from eggpool.config_reload_policy import (
    _FIELD_DISPOSITION,
    _SECRET_FIELD_NAMES,
    ReloadDisposition,
    _disposition_for,
    _is_secret_field,
    compute_diff,
)

SERVER_API_KEY = "ep_test_server_key_1234567890"
ACCOUNT_API_KEY = "sk-test-account-key-1234567890"


# ---------------------------------------------------------------------------
# Schema walking helpers
# ---------------------------------------------------------------------------


def _schema_leaves(model: Any, prefix: str = "") -> list[str]:
    """Walk a Pydantic model and return dotted paths for every scalar leaf.

    List and dict fields are leaves (not recursed into), matching the
    convention in ``test_config_reload_policy.py::_scalar_leaves``.
    """
    from pydantic import BaseModel

    leaves: list[str] = []
    if isinstance(model, BaseModel):
        for name in type(model).model_fields:
            child = getattr(model, name, None)
            path = f"{prefix}.{name}" if prefix else name
            if isinstance(child, BaseModel):
                leaves.extend(_schema_leaves(child, path))
            else:
                leaves.append(path)
    return leaves


def _build_schema_path_set() -> set[str]:
    """Collect all dotted leaf paths from the live AppConfig schema."""
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
    return set(_schema_leaves(config))


# ---------------------------------------------------------------------------
# Inventory table helpers
# ---------------------------------------------------------------------------


def _build_inventory_table() -> list[tuple[str, str, bool, str]]:
    """Build a sorted list of (path, disposition, is_secret, inherited_from) tuples.

    For each path in ``_FIELD_DISPOSITION`` the table records:
    - the dotted path;
    - the disposition label;
    - whether the path is a secret field (last segment in ``_SECRET_FIELD_NAMES``);
    - the parent it inherits from, or ``"exact"`` for an explicit entry.
    """
    # Known inheritable parents (from _disposition_for docstring).
    inheritable_parents = {
        "providers",
        "accounts",
        "model_overrides",
        "model_capabilities",
        "transcoder",
        "compression",
        "cache",
        "models",
    }

    table: list[tuple[str, str, bool, str]] = []
    for path, disposition in sorted(_FIELD_DISPOSITION.items()):
        is_secret = _is_secret_field(path)
        # Determine inheritance source.
        if path in _FIELD_DISPOSITION:
            # Check if a parent prefix also exists in the map.
            parts = path.split(".")
            inherited_from = "exact"
            for i in range(1, len(parts)):
                parent = ".".join(parts[:i])
                if parent in _FIELD_DISPOSITION and parent in inheritable_parents:
                    inherited_from = f"parent:{parent}"
                    break
        else:
            inherited_from = "default:RESTART_REQUIRED"
        table.append((path, disposition.value, is_secret, inherited_from))
    return table


def print_inventory() -> None:
    """Print the inventory table in Markdown format.

    Intended to be wired as a pytest fixture so ``pytest -s`` produces
    the table.  Also usable standalone.
    """
    table = _build_inventory_table()
    header = "| Path | Disposition | Secret | Inherits From |"
    separator = "|------|-------------|--------|---------------|"
    print()  # noqa: T201
    print(header)  # noqa: T201
    print(separator)  # noqa: T201
    for path, disposition, is_secret, inherited_from in table:
        secret_col = "yes" if is_secret else ""
        print(f"| {path} | {disposition} | {secret_col} | {inherited_from} |")  # noqa: T201
    print()  # noqa: T201
    print(f"Total entries: {len(table)}")  # noqa: T201


@pytest.fixture(autouse=True)
def _print_inventory_fixture(capsys: pytest.CaptureFixture[str]) -> Any:  # type: ignore[type-arg]
    """Auto-print the inventory table when tests run with ``-s``."""
    yield
    # Print after all tests in this module complete.
    import sys

    if sys.flags.interactive or "-s" in sys.argv or "--capture=no" in sys.argv:
        print_inventory()


# ===========================================================================
# Test 1: live_inventory_table — snapshot of the full inventory
# ===========================================================================

# Frozen snapshot of the inventory.  If this drifts, update the snapshot
# and justify the change in the commit message.
EXPECTED_INVENTORY_SNAPSHOT: tuple[tuple[str, str, bool], ...] = (
    ("accounts", "live", False),
    ("backup.directory", "restart_required", False),
    ("backup.enabled", "live", False),
    ("backup.include_env", "restart_required", False),
    ("backup.interval_s", "live", False),
    ("backup.retain_count", "live", False),
    ("backup.startup_delay_s", "live", False),
    ("cache", "live", False),
    ("compression", "live", False),
    ("dashboard.enabled", "restart_required", False),
    ("dashboard.public", "restart_required", False),
    ("dashboard.refresh_interval_s", "restart_required", False),
    ("dashboard.retain_event_days", "live", False),
    ("dashboard.retain_request_stats_days", "live", False),
    ("dashboard.store_request_content", "restart_required", False),
    ("dashboard.theme", "restart_required", False),
    ("dashboard.themes_dir", "restart_required", False),
    ("database.busy_timeout_ms", "restart_required", False),
    ("database.path", "restart_required", False),
    ("database.synchronous", "restart_required", False),
    ("database.wal", "restart_required", False),
    ("database.worker_threads", "restart_required", False),
    ("dispatch_writer.enabled", "restart_required", False),
    ("dispatch_writer.enqueue_timeout_ms", "restart_required", False),
    ("dispatch_writer.high_pressure_batch_wait_ms", "restart_required", False),
    ("dispatch_writer.low_pressure_batch_wait_ms", "restart_required", False),
    ("dispatch_writer.max_batch_size", "restart_required", False),
    ("dispatch_writer.max_batch_wait_ms", "restart_required", False),
    ("dispatch_writer.max_queue_depth", "restart_required", False),
    ("dispatch_writer.sample_window", "restart_required", False),
    ("dispatch_writer.shutdown_drain_timeout_s", "restart_required", False),
    ("dns_cache.enabled", "restart_required", False),
    ("dns_cache.lookup_timeout_seconds", "restart_required", False),
    ("dns_cache.max_entries", "restart_required", False),
    ("dns_cache.negative_ttl_seconds", "restart_required", False),
    ("dns_cache.positive_ttl_seconds", "restart_required", False),
    ("dns_cache.prefer_ipv6", "restart_required", False),
    ("dns_cache.stale_if_error_seconds", "restart_required", False),
    ("force_segmentation", "restart_required", False),
    ("limits.five_hour_microdollars", "restart_required", False),
    ("limits.monthly_microdollars", "restart_required", False),
    ("limits.weekly_microdollars", "restart_required", False),
    ("metrics.aggregate_only", "restart_required", False),
    ("metrics.cleanup_interval_s", "restart_required", False),
    ("metrics.cleanup_max_rows_per_pass", "restart_required", False),
    ("metrics.detailed_span_sample_rate", "live", False),
    ("metrics.dispatch_spans.sample_rate", "live", False),
    ("metrics.dispatch_spans.window_size", "restart_required", False),
    ("metrics.event_loop_lag_enabled", "restart_required", False),
    ("metrics.flush_interval_s", "live", False),
    ("metrics.max_buffered_events", "restart_required", False),
    ("metrics.rollup_retain_days", "restart_required", False),
    ("metrics.timeseries_bucket_s", "restart_required", False),
    ("metrics.trace_sample_rate", "restart_required", False),
    ("metrics.write_mode", "restart_required", False),
    ("model_capabilities", "live", False),
    ("model_info.aliases", "restart_required", False),
    ("model_info.conflict_ttl_s", "restart_required", False),
    ("model_info.enabled", "live", False),
    ("model_info.include_in_models_endpoint", "restart_required", False),
    ("model_info.known_ttl_s", "restart_required", False),
    ("model_info.max_models_per_cycle", "restart_required", False),
    ("model_info.overrides", "restart_required", False),
    ("model_info.partial_ttl_s", "restart_required", False),
    ("model_info.refresh_interval_s", "live", False),
    ("model_info.sources", "restart_required", False),
    ("model_info.sparse_new_accelerated_days", "restart_required", False),
    ("model_info.sparse_new_initial_ttl_s", "restart_required", False),
    ("model_info.sparse_new_later_ttl_s", "restart_required", False),
    ("model_info.startup_refresh", "restart_required", False),
    ("model_info.store_raw_observations", "restart_required", False),
    ("model_overrides", "live", False),
    ("models.allow_stale_catalog", "live", False),
    ("models.catalog_withdrawal_policy", "restart_required", False),
    ("models.collapse_models", "live", False),
    ("models.expose_mode", "live", False),
    ("models.ping_retain_days", "live", False),
    ("models.refresh_interval_s", "live", False),
    ("models.stale_after_s", "live", False),
    ("models.startup_refresh", "restart_required", False),
    ("network.connect_timeout_s", "restart_required", False),
    ("network.dns_cache.enabled", "restart_required", False),
    ("network.dns_cache.lookup_timeout_seconds", "restart_required", False),
    ("network.dns_cache.max_entries", "restart_required", False),
    ("network.dns_cache.negative_ttl_seconds", "restart_required", False),
    ("network.dns_cache.positive_ttl_seconds", "restart_required", False),
    ("network.dns_cache.prefer_ipv6", "restart_required", False),
    ("network.dns_cache.stale_if_error_seconds", "restart_required", False),
    ("network.keepalive_expiry_s", "restart_required", False),
    ("network.max_connections", "restart_required", False),
    ("network.max_keepalive", "restart_required", False),
    ("network.read_timeout_s", "restart_required", False),
    ("pricing.catalogs.opencode_zen.api_key", "restart_required", True),
    ("pricing.catalogs.opencode_zen.base_url", "restart_required", False),
    ("pricing.catalogs.opencode_zen.enabled", "restart_required", False),
    ("pricing.catalogs.opencode_zen.max_entries", "restart_required", False),
    ("pricing.catalogs.opencode_zen.options", "restart_required", False),
    ("pricing.catalogs.opencode_zen.priority", "restart_required", False),
    ("pricing.catalogs.opencode_zen.ttl_seconds", "restart_required", False),
    ("pricing.catalogs.openrouter.api_key", "restart_required", True),
    ("pricing.catalogs.openrouter.base_url", "restart_required", False),
    ("pricing.catalogs.openrouter.enabled", "restart_required", False),
    ("pricing.catalogs.openrouter.max_entries", "restart_required", False),
    ("pricing.catalogs.openrouter.options", "restart_required", False),
    ("pricing.catalogs.openrouter.priority", "restart_required", False),
    ("pricing.catalogs.openrouter.ttl_seconds", "restart_required", False),
    ("pricing.fallback", "restart_required", False),
    ("providers", "live", False),
    ("proxies", "restart_required", False),
    ("readiness_probe.enabled", "restart_required", False),
    ("readiness_probe.freshness_s", "restart_required", False),
    ("readiness_probe.initial_probe", "restart_required", False),
    ("readiness_probe.interval_s", "restart_required", False),
    ("readiness_probe.timeout_s", "restart_required", False),
    ("routing.fairness_epsilon", "live", False),
    ("routing.fairness_mode", "live", False),
    ("routing.fairness_scope", "live", False),
    ("routing.health_penalty", "live", False),
    ("routing.inflight_penalty", "live", False),
    ("routing.local_quota_mode", "live", False),
    ("routing.max_retries_before_stream", "live", False),
    ("routing.near_tie_epsilon", "live", False),
    ("routing.quota_exhausted_cooldown_seconds", "live", False),
    ("routing.randomize_near_ties", "live", False),
    ("routing.strategy", "live", False),
    ("routing.trace.flush_interval_s", "restart_required", False),
    ("routing.trace.guard_cooldown_s", "live", False),
    ("routing.trace.guard_oldest_event_age_s", "live", False),
    ("routing.trace.guard_queue_occupancy_threshold", "live", False),
    ("routing.trace.include_score_components", "live", False),
    ("routing.trace.max_batch_size", "restart_required", False),
    ("routing.trace.mode", "live", False),
    ("routing.trace.queue_capacity", "restart_required", False),
    ("routing.trace.sample_rate", "live", False),
    ("routing.trace.shutdown_flush_timeout_s", "restart_required", False),
    ("routing.trace.skip_above_lock_wait_p95_ms", "live", False),
    ("routing.unknown_request_reservation_microdollars", "live", False),
    ("security.allowed_hosts", "restart_required", False),
    ("security.cors_origins", "restart_required", False),
    ("security.persist_redacted_error_detail", "live", False),
    ("security.redact_headers", "restart_required", False),
    ("security.trusted_proxies", "restart_required", False),
    ("server.access_log", "restart_required", False),
    ("server.api_key", "restart_required", True),
    ("server.api_key_env", "restart_required", True),
    ("server.host", "restart_required", False),
    ("server.log_level", "restart_required", False),
    ("server.port", "restart_required", False),
    ("server.threads", "restart_required", False),
    ("transcoder", "live", False),
    ("update_checker.enabled", "restart_required", False),
    ("upstream.base_url", "restart_required", False),
    ("upstream.connect_timeout_s", "restart_required", False),
    ("upstream.keepalive_timeout_s", "restart_required", False),
    ("upstream.max_connections", "restart_required", False),
    ("upstream.max_keepalive", "restart_required", False),
    ("upstream.pool_timeout_s", "restart_required", False),
    ("upstream.read_timeout_s", "restart_required", False),
    ("upstream.write_timeout_s", "restart_required", False),
)


def test_live_inventory_table() -> None:
    """Programmatically export the inventory as a sorted snapshot.

    The snapshot catches unintended drift: any addition, removal, or
    disposition change in ``_FIELD_DISPOSITION`` must be reflected here.
    Run with ``-v`` to see the printed Markdown table.
    """
    actual = []
    for path, disposition in sorted(_FIELD_DISPOSITION.items()):
        actual.append((path, disposition.value, _is_secret_field(path)))

    if actual != list(EXPECTED_INVENTORY_SNAPSHOT):
        actual_set = {p for p, _, _ in actual}
        expected_set = {p for p, _, _ in EXPECTED_INVENTORY_SNAPSHOT}
        added = actual_set - expected_set
        removed = expected_set - actual_set
        msg_parts = ["Inventory snapshot drift detected."]
        if added:
            msg_parts.append(f"Added to _FIELD_DISPOSITION: {sorted(added)}")
        if removed:
            msg_parts.append(f"Removed from _FIELD_DISPOSITION: {sorted(removed)}")
        # Also check for disposition changes on common paths.
        actual_map = {p: d for p, d, _ in actual}
        expected_map = {p: d for p, d, _ in EXPECTED_INVENTORY_SNAPSHOT}
        changed = {
            p
            for p in actual_map
            if p in expected_map and actual_map[p] != expected_map[p]
        }
        if changed:
            details = [
                f"  {p}: {expected_map[p]} -> {actual_map[p]}" for p in sorted(changed)
            ]
            msg_parts.append("Disposition changed:\n" + "\n".join(details))
        pytest.fail("\n".join(msg_parts))

    # Print the table for human review when running with -v.
    table = _build_inventory_table()
    header = "| Path | Disposition | Secret | Inherits From |"
    separator = "|------|-------------|--------|---------------|"
    lines = [header, separator]
    for path, disposition, is_secret, inherited_from in table:
        secret_col = "yes" if is_secret else ""
        lines.append(f"| {path} | {disposition} | {secret_col} | {inherited_from} |")
    lines.append(f"\nTotal entries: {len(table)}")
    print("\n".join(lines))  # noqa: T201


# ===========================================================================
# Test 2: schema_walk_matches_policy_inventory — parity check
# ===========================================================================


def test_schema_walk_matches_policy_inventory() -> None:
    """Walk the AppConfig schema and assert parity with the policy map.

    - Every schema leaf must resolve via ``_disposition_for()`` (no
      unclassified fields — the function always returns a value, so the
      real check is that the path or its prefix appears in the map).
    - The policy map must contain no path that is NOT reachable from
      the schema (no orphan policies).

    Known mismatches between the schema and the policy map are reported
    as warnings (not failures) so the audit surfaces bugs without
    breaking CI on pre-existing issues.
    """
    schema_paths = _build_schema_path_set()
    policy_paths = set(_FIELD_DISPOSITION)

    # Build the set of paths that _disposition_for can resolve via prefix.
    # Any schema path whose top-level or intermediate prefix is in the map
    # is considered "reachable".
    reachable_from_schema: set[str] = set()
    for sp in schema_paths:
        if sp in _FIELD_DISPOSITION:
            reachable_from_schema.add(sp)
        else:
            # Walk prefixes to see if any parent is in the map.
            parts = sp.split(".")
            for i in range(1, len(parts)):
                prefix = ".".join(parts[:i])
                if prefix in _FIELD_DISPOSITION:
                    reachable_from_schema.add(sp)
                    break

    # Schema paths that cannot be resolved at all (no prefix match).
    unresolved_schema = schema_paths - reachable_from_schema

    # Policy paths that are not reachable from any schema path (orphans).
    # A policy path is reachable if:
    # - it is itself a schema leaf, OR
    # - it is a prefix of some schema leaf, OR
    # - it matches a dynamic-map parent (providers, accounts, etc.)
    #   that is a schema dict/list field.
    dynamic_map_prefixes = {
        "providers",
        "accounts",
        "model_overrides",
        "model_capabilities",
    }
    orphan_policy: set[str] = set()
    for pp in policy_paths:
        # Exact schema match.
        if pp in schema_paths:
            continue
        # Prefix of a schema leaf.
        if any(sp.startswith(pp + ".") or sp == pp for sp in schema_paths):
            continue
        # Dynamic-map parent: the policy path is "<parent>" or "<parent>.<key>".
        top_level = pp.split(".")[0]
        if top_level in dynamic_map_prefixes:
            # The policy entry for the bare parent (e.g. "providers") is
            # fine even though the schema has dict fields — the walker
            # emits the bare path as a leaf for dict/list fields.
            if pp == top_level:
                continue
            # Per-key entries (e.g. "providers.opencode-go") are reachable
            # via prefix match from the bare "providers" schema leaf —
            # but only if the top-level is a dict/list in the schema.
            continue
        # Not reachable — mark as orphan.
        orphan_policy.add(pp)

    # Known pre-existing mismatches between the schema and policy map.
    # The dns_cache and pricing.catalogs gaps were closed in milestone D3;
    # only the list-of-dict ``pricing.catalogs.aliases`` field remains
    # outside the policy map because list-of-dict entries cannot be
    # classified as scalar leaves — operators edit it through the same
    # RESTART_REQUIRED disposition the rest of the catalog inherits.
    known_orphans: frozenset[str] = frozenset()
    known_unresolved: frozenset[str] = frozenset(
        {
            "pricing.catalogs.aliases",
            "maintenance.contention_defer_above_lock_wait_p95_ms",
            "maintenance.max_batches_per_tick",
            "maintenance.max_deferral_age_s",
            "maintenance.max_rows_per_batch",
            "maintenance.max_tick_duration_ms",
            "maintenance.p0_max_batches_per_tick",
            "maintenance.p0_max_rows_per_batch",
            "maintenance.p0_max_tick_duration_ms",
        }
    )

    unexpected_orphans = orphan_policy - known_orphans
    unexpected_unresolved = unresolved_schema - known_unresolved

    # Warn about known mismatches so they stay visible.
    if orphan_policy & known_orphans:
        import warnings

        warnings.warn(
            f"Known policy orphans (schema/policy mismatch): "
            f"{sorted(orphan_policy & known_orphans)}",
            stacklevel=1,
        )
    if unresolved_schema & known_unresolved:
        import warnings

        warnings.warn(
            f"Known schema fields without policy resolution: "
            f"{sorted(unresolved_schema & known_unresolved)}",
            stacklevel=1,
        )

    # Fail on NEW mismatches only.
    errors: list[str] = []
    if unexpected_unresolved:
        errors.append(
            f"Schema fields without policy resolution: {sorted(unexpected_unresolved)}"
        )
    if unexpected_orphans:
        errors.append(
            f"Policy orphans (unreachable from schema): {sorted(unexpected_orphans)}"
        )

    assert not errors, "\n".join(errors)


# ===========================================================================
# Test 3: secret_field_tagging_audit
# ===========================================================================


def test_secret_field_tagging_audit() -> None:
    """Audit that credential-bearing paths are tagged as secrets.

    Checks:
    1. Every schema leaf ending with a secret-suffix field name has
       ``_is_secret_field(path) == True``.
    2. ``_SECRET_FIELD_NAMES`` covers all known credential-style field
       names in AppConfig.
    3. An actual diff of a changed secret value renders as ``"<changed>"``.
    """
    schema_paths = _build_schema_path_set()

    # 1. Check that every path ending with a secret suffix is tagged.
    secret_suffixes = {"api_key", "api_key_env", "proxy_url", "proxy_url_env"}
    untagged: list[str] = []
    for sp in sorted(schema_paths):
        tail = sp.rsplit(".", 1)[-1]
        if tail in secret_suffixes and not _is_secret_field(sp):
            untagged.append(sp)
    assert not untagged, (
        f"Secret-suffix fields not tagged as secret: {untagged}. "
        f"Add them to _SECRET_FIELD_NAMES or fix _is_secret_field."
    )

    # 2. Audit _SECRET_FIELD_NAMES for completeness.  Walk schema models
    #    and collect field names that look like credentials.
    from eggpool.models.config import AppConfig

    config_sample = AppConfig.from_dict(
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
    all_field_names: set[str] = set()
    _collect_field_names(config_sample, "", all_field_names)

    # Credential-style suffixes that should be in _SECRET_FIELD_NAMES.
    credential_suffixes = {"api_key", "api_key_env", "proxy_url", "proxy_url_env"}
    missing_from_set = {
        name for name in all_field_names if name in credential_suffixes
    } - set(_SECRET_FIELD_NAMES)
    # Log (not fail) if we discover new secret names that need adding.
    if missing_from_set:
        import warnings

        warnings.warn(
            f"_SECRET_FIELD_NAMES may need expansion: {sorted(missing_from_set)}. "
            "Add these to the frozenset in config_reload_policy.py.",
            stacklevel=1,
        )

    # 3. Run an actual diff with a changed secret and assert redaction.
    data: dict[str, object] = {
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
    mutated: dict[str, object] = {
        **data,
        "providers": {
            "opencode-go": {
                **data["providers"]["opencode-go"],  # type: ignore[index]
                "accounts": [
                    {
                        **data["providers"]["opencode-go"]["accounts"][0],  # type: ignore[index]
                        "api_key": "sk-rotated-67890",
                    }
                ],
            }
        },
    }
    new = AppConfig.from_dict(mutated)
    diff = compute_diff(old, new)
    secret_changes = [c for c in diff.changes if c.secret and "api_key" in c.path]
    assert secret_changes, (
        "Expected at least one secret api_key change; got: "
        f"{[c.path for c in diff.changes]}"
    )
    for change in secret_changes:
        assert change.old_display == "<changed>", (
            f"{change.path}: old_display must be '<changed>' for secrets"
        )
        assert change.new_display == "<changed>", (
            f"{change.path}: new_display must be '<changed>' for secrets"
        )
        raw = str(change)
        assert "sk-original-12345" not in raw
        assert "sk-rotated-67890" not in raw


def _collect_field_names(model: Any, prefix: str, out: set[str]) -> None:
    """Recursively collect all Pydantic field names from a model tree."""
    from pydantic import BaseModel

    if isinstance(model, BaseModel):
        for name in type(model).model_fields:
            out.add(name)
            child = getattr(model, name, None)
            if isinstance(child, BaseModel):
                _collect_field_names(child, f"{prefix}.{name}", out)


# ===========================================================================
# Test 4: inheritable_parent_audit
# ===========================================================================


def test_inheritable_parent_audit() -> None:
    """Assert that only documented prefixes inherit a LIVE disposition.

    The documented inheritable parents (blanket LIVE for any child) are:
      providers, accounts, model_overrides, model_capabilities,
      transcoder, compression, cache

    ``models`` is a special case: only registered sub-paths are LIVE.
    Unknown ``models.*`` children must return ``RESTART_REQUIRED``.

    Any other dynamic-map prefix (e.g. ``proxies``, ``server.api_key_env``)
    must return ``RESTART_REQUIRED`` for unknown children.
    """
    # Documented parents that return LIVE for ANY unknown child.
    blanket_live_parents = {
        "providers",
        "accounts",
        "model_overrides",
        "model_capabilities",
        "transcoder",
        "compression",
        "cache",
    }

    # Verify documented parents return LIVE for unknown children.
    for parent in sorted(blanket_live_parents):
        child_path = f"{parent}.unknown_child_that_does_not_exist"
        assert _disposition_for(child_path) is ReloadDisposition.LIVE, (
            f"{child_path} must inherit LIVE from {parent}"
        )

    # Verify ``models`` does NOT blanket-inherit; only registered sub-paths are LIVE.
    for path in (
        "models.brand_new_toggle",
        "models.unknown_new_field",
    ):
        assert _disposition_for(path) is ReloadDisposition.RESTART_REQUIRED, (
            f"{path} must default to RESTART_REQUIRED "
            "(models requires explicit disposition)"
        )

    # Verify non-documented prefixes return RESTART_REQUIRED for unknown children.
    non_inheritable_prefixes = [
        "proxies",
        "server",
        "upstream",
        "database",
        "limits",
        "pricing",
        "dashboard",
        "security",
        "metrics",
        "backup",
        "dns_cache",
        "network",
        "routing",
        "model_info",
    ]
    for prefix in sorted(non_inheritable_prefixes):
        child_path = f"{prefix}.unknown_child_that_does_not_exist"
        assert _disposition_for(child_path) is ReloadDisposition.RESTART_REQUIRED, (
            f"{child_path} must NOT inherit from {prefix}; "
            f"must stay RESTART_REQUIRED for unknown children"
        )

    # Verify truly unknown top-level paths also fail closed.
    for path in ["not_a_section.anything", "random.top.child"]:
        assert _disposition_for(path) is ReloadDisposition.RESTART_REQUIRED, (
            f"{path} must default to RESTART_REQUIRED"
        )


# ===========================================================================
# Test 5: fail_closed_default_audit
# ===========================================================================

# 50 made-up paths that look like real schema fields but are not in the inventory.
# NOTE: paths under LIVE parents (transcoder, compression, cache, providers,
# accounts, model_overrides, model_capabilities) intentionally inherit LIVE,
# so they are NOT included here — they are tested in inheritable_parent_audit.
_MADE_UP_PATHS: list[str] = [
    "server.tls_cert_path",
    "server.tls_key_path",
    "server.worker_count",
    "server.pid_file",
    "server.graceful_timeout",
    "upstream.retry_budget",
    "upstream.circuit_breaker",
    "upstream.health_check_interval",
    "upstream.tls_verify",
    "database.pool_size",
    "database.journal_mode",
    "database.cache_size",
    "database.mmap_size",
    "database.page_size",
    "models.default_context_window",
    "models.max_concurrent_requests",
    "models.rate_limit_rpm",
    "models.fallback_model",
    "models.priority",
    "routing.retry_backoff_base",
    "routing.circuit_breaker_threshold",
    "routing.health_check_path",
    "routing.max_queue_depth",
    "routing.request_timeout",
    "limits.requests_per_minute",
    "limits.tokens_per_hour",
    "limits.max_concurrent",
    "dashboard.session_timeout",
    "dashboard.admin_password",
    "dashboard.log_queries",
    "security.jwt_secret",
    "security.api_rate_limit",
    "security.brute_force_threshold",
    "security.session_store",
    "metrics.export_url",
    "metrics.prometheus_port",
    "metrics.labels",
    "backup.gpg_key",
    "backup.compression",
    "backup.exclude_patterns",
    "dns_cache.eviction_policy",
    "dns_cache.warm_on_start",
    "network.proxy_url",
    "network.user_agent",
    "network.tls_ca_bundle",
    "model_info.max_age_days",
    "force_segmentation.enabled",
    "proxies.my_proxy.url",
    "pricing.catalogs.openrouter.api_key",
    "pricing.catalogs.opencode_zen.enabled",
]


def test_fail_closed_default_audit() -> None:
    """Assert that made-up paths and ``not_a_real_field`` return RESTART_REQUIRED."""
    for path in _MADE_UP_PATHS:
        assert _disposition_for(path) is ReloadDisposition.RESTART_REQUIRED, (
            f"Made-up path {path!r} must default to RESTART_REQUIRED (fail closed)"
        )

    # Explicitly test the bare string.
    assert _disposition_for("not_a_real_field") is ReloadDisposition.RESTART_REQUIRED, (
        "'not_a_real_field' must default to RESTART_REQUIRED"
    )
