"""Typed configuration diff and reload policy.

The reload policy classifies every :class:`AppConfig` field as one of:

- :attr:`ReloadDisposition.LIVE` -- the field can be applied without
  restarting the supervisor process.  No field is currently classified
  this way in milestone A; milestone B/C will introduce generation-owned
  services that consume live-reloadable state.  Marking a field ``LIVE``
  before milestone C lands is a regression.
- :attr:`ReloadDisposition.RESTART_REQUIRED` -- the field is consumed by
  constructor-owned state (Granian construction, database connection,
  middleware, ASGI app) and cannot change without a process restart.
- :attr:`ReloadDisposition.IGNORED` -- the field is captured for audit
  only; it does not change runtime behavior.  Reserved for diagnostics.

Adding a new field
------------------

Add the field to ``AppConfig`` first, then add an entry to
:data:`_FIELD_DISPOSITION`.  The default is
:attr:`ReloadDisposition.RESTART_REQUIRED` so any field not explicitly
classified is fail-closed against partial live reloads.  This module is
the single reviewable map of which fields each milestone is expected to
be able to swap live -- when milestone B/C adds a live replacement path,
move the corresponding entry to ``LIVE`` in the same diff that
introduces the swap path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from pathlib import Path

    from eggpool.config_validation import (
        ConfigValidationResult,
        ConfigValidationWarning,
    )
    from eggpool.models.config import AppConfig


class ReloadDisposition(Enum):
    """Reload disposition for a single configuration field."""

    LIVE = "live"
    RESTART_REQUIRED = "restart_required"
    IGNORED = "ignored"


@dataclass(frozen=True)
class ConfigChange:
    """A single field-level change between two :class:`AppConfig` versions.

    ``secret`` is ``True`` whenever the underlying field carries
    credentials or other secret-adjacent state -- in that case ``old_display``
    and ``new_display`` are replaced with ``"<changed>"`` so the value is
    never written to logs or command output.
    """

    path: str
    disposition: ReloadDisposition
    old_display: str
    new_display: str
    section: str
    secret: bool = False


@dataclass(frozen=True)
class ConfigDiff:
    """Structured diff between two :class:`AppConfig` instances."""

    changes: tuple[ConfigChange, ...]

    @property
    def restart_required(self) -> tuple[ConfigChange, ...]:
        """Return only the changes classified as ``RESTART_REQUIRED``."""
        return tuple(
            change
            for change in self.changes
            if change.disposition is ReloadDisposition.RESTART_REQUIRED
        )

    @property
    def live(self) -> tuple[ConfigChange, ...]:
        """Return only the changes classified as ``LIVE``."""
        return tuple(
            change
            for change in self.changes
            if change.disposition is ReloadDisposition.LIVE
        )


# Field-level reload policy.
#
# Each key is a dotted path that reaches a Pydantic field on AppConfig or
# on a nested model; each value is the corresponding ReloadDisposition.
# Order matters only for stability of test output via ``compute_diff``
# below; it does not change classification.
#
# The default for any field not listed here is RESTART_REQUIRED to keep
# the policy fail-closed.
_FIELD_DISPOSITION: Final[dict[str, ReloadDisposition]] = {
    # ---- server (Granian construction + middleware are constructor-owned) ----
    "server.host": ReloadDisposition.RESTART_REQUIRED,
    "server.port": ReloadDisposition.RESTART_REQUIRED,
    "server.api_key": ReloadDisposition.RESTART_REQUIRED,
    "server.api_key_env": ReloadDisposition.RESTART_REQUIRED,
    "server.log_level": ReloadDisposition.RESTART_REQUIRED,
    "server.access_log": ReloadDisposition.RESTART_REQUIRED,
    "server.threads": ReloadDisposition.RESTART_REQUIRED,
    # ---- upstream provider transport (consumed at client-pool build) ----
    "upstream.base_url": ReloadDisposition.RESTART_REQUIRED,
    "upstream.connect_timeout_s": ReloadDisposition.RESTART_REQUIRED,
    "upstream.read_timeout_s": ReloadDisposition.RESTART_REQUIRED,
    "upstream.write_timeout_s": ReloadDisposition.RESTART_REQUIRED,
    "upstream.pool_timeout_s": ReloadDisposition.RESTART_REQUIRED,
    "upstream.max_connections": ReloadDisposition.RESTART_REQUIRED,
    "upstream.max_keepalive": ReloadDisposition.RESTART_REQUIRED,
    "upstream.keepalive_timeout_s": ReloadDisposition.RESTART_REQUIRED,
    # ---- database path / WAL / synchronous / worker thread count ----
    "database.path": ReloadDisposition.RESTART_REQUIRED,
    "database.busy_timeout_ms": ReloadDisposition.RESTART_REQUIRED,
    "database.wal": ReloadDisposition.RESTART_REQUIRED,
    "database.synchronous": ReloadDisposition.RESTART_REQUIRED,
    "database.worker_threads": ReloadDisposition.RESTART_REQUIRED,
    # ---- models catalog refresh cadence ----
    "models.refresh_interval_s": ReloadDisposition.RESTART_REQUIRED,
    "models.expose_mode": ReloadDisposition.RESTART_REQUIRED,
    "models.startup_refresh": ReloadDisposition.RESTART_REQUIRED,
    "models.stale_after_s": ReloadDisposition.RESTART_REQUIRED,
    "models.allow_stale_catalog": ReloadDisposition.RESTART_REQUIRED,
    "models.ping_retain_days": ReloadDisposition.RESTART_REQUIRED,
    "models.collapse_models": ReloadDisposition.RESTART_REQUIRED,
    "models.catalog_withdrawal_policy": ReloadDisposition.RESTART_REQUIRED,
    # ---- routing strategy + scoring knobs (consumed at router construction) ----
    "routing.strategy": ReloadDisposition.RESTART_REQUIRED,
    "routing.near_tie_epsilon": ReloadDisposition.RESTART_REQUIRED,
    "routing.max_retries_before_stream": ReloadDisposition.RESTART_REQUIRED,
    "routing.unknown_request_reservation_microdollars": ReloadDisposition.RESTART_REQUIRED,  # noqa: E501
    "routing.inflight_penalty": ReloadDisposition.RESTART_REQUIRED,
    "routing.health_penalty": ReloadDisposition.RESTART_REQUIRED,
    "routing.randomize_near_ties": ReloadDisposition.RESTART_REQUIRED,
    "routing.quota_exhausted_cooldown_seconds": ReloadDisposition.RESTART_REQUIRED,
    "routing.local_quota_mode": ReloadDisposition.RESTART_REQUIRED,
    "routing.fairness_mode": ReloadDisposition.RESTART_REQUIRED,
    "routing.fairness_epsilon": ReloadDisposition.RESTART_REQUIRED,
    "routing.fairness_scope": ReloadDisposition.RESTART_REQUIRED,
    "routing.trace.mode": ReloadDisposition.RESTART_REQUIRED,
    "routing.trace.sample_rate": ReloadDisposition.RESTART_REQUIRED,
    "routing.trace.include_score_components": ReloadDisposition.RESTART_REQUIRED,
    "routing.trace.skip_above_lock_wait_p95_ms": ReloadDisposition.RESTART_REQUIRED,
    # ---- limits / pricing / dashboard / security ----
    "limits.five_hour_microdollars": ReloadDisposition.RESTART_REQUIRED,
    "limits.weekly_microdollars": ReloadDisposition.RESTART_REQUIRED,
    "limits.monthly_microdollars": ReloadDisposition.RESTART_REQUIRED,
    "pricing.fallback": ReloadDisposition.RESTART_REQUIRED,
    "dashboard.enabled": ReloadDisposition.RESTART_REQUIRED,
    "dashboard.public": ReloadDisposition.RESTART_REQUIRED,
    "dashboard.theme": ReloadDisposition.RESTART_REQUIRED,
    "dashboard.themes_dir": ReloadDisposition.RESTART_REQUIRED,
    "dashboard.retain_request_stats_days": ReloadDisposition.RESTART_REQUIRED,
    "dashboard.retain_event_days": ReloadDisposition.RESTART_REQUIRED,
    "dashboard.store_request_content": ReloadDisposition.RESTART_REQUIRED,
    "dashboard.refresh_interval_s": ReloadDisposition.RESTART_REQUIRED,
    "security.allowed_hosts": ReloadDisposition.RESTART_REQUIRED,
    "security.cors_origins": ReloadDisposition.RESTART_REQUIRED,
    "security.redact_headers": ReloadDisposition.RESTART_REQUIRED,
    "security.persist_redacted_error_detail": ReloadDisposition.RESTART_REQUIRED,
    # ---- metrics / backup / dns / network (process-owned) ----
    "metrics.write_mode": ReloadDisposition.RESTART_REQUIRED,
    "metrics.flush_interval_s": ReloadDisposition.RESTART_REQUIRED,
    "metrics.max_buffered_events": ReloadDisposition.RESTART_REQUIRED,
    "metrics.timeseries_bucket_s": ReloadDisposition.RESTART_REQUIRED,
    "metrics.trace_sample_rate": ReloadDisposition.RESTART_REQUIRED,
    "metrics.aggregate_only": ReloadDisposition.RESTART_REQUIRED,
    "metrics.rollup_retain_days": ReloadDisposition.RESTART_REQUIRED,
    "metrics.cleanup_interval_s": ReloadDisposition.RESTART_REQUIRED,
    "metrics.cleanup_max_rows_per_pass": ReloadDisposition.RESTART_REQUIRED,
    "backup.enabled": ReloadDisposition.RESTART_REQUIRED,
    "backup.interval_s": ReloadDisposition.RESTART_REQUIRED,
    "backup.retain_count": ReloadDisposition.RESTART_REQUIRED,
    "backup.startup_delay_s": ReloadDisposition.RESTART_REQUIRED,
    "backup.directory": ReloadDisposition.RESTART_REQUIRED,
    "backup.include_env": ReloadDisposition.RESTART_REQUIRED,
    "dns_cache.enabled": ReloadDisposition.RESTART_REQUIRED,
    "dns_cache.ttl_seconds": ReloadDisposition.RESTART_REQUIRED,
    "dns_cache.max_entries": ReloadDisposition.RESTART_REQUIRED,
    "dns_cache.negative_ttl_seconds": ReloadDisposition.RESTART_REQUIRED,
    "dns_cache.stale_if_error_seconds": ReloadDisposition.RESTART_REQUIRED,
    "dns_cache.prefer_ipv6": ReloadDisposition.RESTART_REQUIRED,
    "dns_cache.lookup_timeout_seconds": ReloadDisposition.RESTART_REQUIRED,
    "network.connect_timeout_s": ReloadDisposition.RESTART_REQUIRED,
    "network.read_timeout_s": ReloadDisposition.RESTART_REQUIRED,
    "network.max_connections": ReloadDisposition.RESTART_REQUIRED,
    "network.max_keepalive": ReloadDisposition.RESTART_REQUIRED,
    "network.keepalive_expiry_s": ReloadDisposition.RESTART_REQUIRED,
    # ---- proxies / providers / accounts / overrides ----
    "proxies": ReloadDisposition.RESTART_REQUIRED,
    "providers": ReloadDisposition.RESTART_REQUIRED,
    "accounts": ReloadDisposition.RESTART_REQUIRED,
    "model_overrides": ReloadDisposition.RESTART_REQUIRED,
    "model_capabilities": ReloadDisposition.RESTART_REQUIRED,
    # ---- transcoder / compression / cache / model_info ----
    "transcoder": ReloadDisposition.RESTART_REQUIRED,
    "compression": ReloadDisposition.RESTART_REQUIRED,
    "cache": ReloadDisposition.RESTART_REQUIRED,
    "model_info.enabled": ReloadDisposition.RESTART_REQUIRED,
    "model_info.startup_refresh": ReloadDisposition.RESTART_REQUIRED,
    "model_info.refresh_interval_s": ReloadDisposition.RESTART_REQUIRED,
    "model_info.known_ttl_s": ReloadDisposition.RESTART_REQUIRED,
    "model_info.partial_ttl_s": ReloadDisposition.RESTART_REQUIRED,
    "model_info.sparse_new_initial_ttl_s": ReloadDisposition.RESTART_REQUIRED,
    "model_info.sparse_new_later_ttl_s": ReloadDisposition.RESTART_REQUIRED,
    "model_info.sparse_new_accelerated_days": ReloadDisposition.RESTART_REQUIRED,
    "model_info.conflict_ttl_s": ReloadDisposition.RESTART_REQUIRED,
    "model_info.max_models_per_cycle": ReloadDisposition.RESTART_REQUIRED,
    "model_info.include_in_models_endpoint": ReloadDisposition.RESTART_REQUIRED,
    "model_info.store_raw_observations": ReloadDisposition.RESTART_REQUIRED,
    "model_info.sources": ReloadDisposition.RESTART_REQUIRED,
    "model_info.aliases": ReloadDisposition.RESTART_REQUIRED,
    "model_info.overrides": ReloadDisposition.RESTART_REQUIRED,
    "force_segmentation": ReloadDisposition.RESTART_REQUIRED,
}


_SECRET_FIELD_NAMES: Final = frozenset(
    {
        "api_key",
        "proxy_url",
        "proxy_url_env",
        "api_key_env",
    }
)


def _is_secret_field(path: str) -> bool:
    tail = path.rsplit(".", 1)[-1]
    return tail in _SECRET_FIELD_NAMES


def _display(value: object, *, secret: bool) -> str:
    """Render a value for ``ConfigChange.{old,new}_display``.

    Secrets are always rendered as ``"<changed>"``; never the raw value.
    Booleans, ints, and simple scalars render via ``repr`` for stability.
    Strings are rendered inline without quotes so logs stay readable.
    Complex values recurse through :func:`_display_collection` so secret
    fields nested inside dicts / lists / tuples are detected and redacted
    instead of leaking through ``repr``.
    """
    if secret:
        return "<changed>"
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float | type(None)):
        return repr(value)
    if isinstance(value, list | tuple | Mapping):
        return _display_collection(
            cast("list[object] | tuple[object, ...] | Mapping[str, object]", value),
            secret=False,
        )
    return repr(value)


def _path_segments(root: object, dotted_path: str) -> tuple[object, str]:
    """Return ``(parent, leaf_name)`` for ``dotted_path``.

    A bare ``"providers"`` against an :class:`AppConfig` returns ``(config,
    "providers")``.  ``"server.host"`` returns ``(config.server, "host")``.
    """
    parts = dotted_path.split(".")
    cursor: object = root
    for segment in parts[:-1]:
        if isinstance(cursor, Mapping):
            cursor = cast("Mapping[str, object]", cursor)[segment]
        else:
            cursor = getattr(cursor, segment)
    return cursor, parts[-1]


def _disposition_for(path: str) -> ReloadDisposition:
    """Return the policy for ``path``, defaulting to ``RESTART_REQUIRED``."""
    return _FIELD_DISPOSITION.get(path, ReloadDisposition.RESTART_REQUIRED)


def _is_structured_object(value: object) -> bool:
    """Return True when ``value`` looks like a Pydantic model / dataclass.

    Used by :func:`_diff_single_field` to decide whether to drill into
    attributes vs emit a single change for the whole object.
    """
    if value is None:
        return False
    if isinstance(value, bool | int | float | str | bytes):
        return False
    if isinstance(value, list | tuple | dict | set | frozenset):
        return False
    type_name = type(value).__name__
    if type_name in {"Model", "BaseModel"}:
        return True
    module = getattr(type(value), "__module__", "")
    return module.startswith("pydantic") or module.startswith("eggpool")


def _diff_single_field(
    field_name: str, old: object, new: object
) -> tuple[ConfigChange, ...]:
    """Return any :class:`ConfigChange` records implied by a single field."""
    if _is_structured_object(old) and _is_structured_object(new):
        return _diff_nested_object(field_name, old, new)
    if old == new:
        return ()
    secret = _is_secret_field(field_name)
    return (
        ConfigChange(
            path=field_name,
            disposition=_disposition_for(field_name),
            old_display=_display(old, secret=secret),
            new_display=_display(new, secret=secret),
            section=field_name.split(".", 1)[0],
            secret=secret,
        ),
    )


def _diff_nested_object(
    field_name: str,
    old: object,
    new: object,
) -> tuple[ConfigChange, ...]:
    """Diff two structured fields (account / provider / etc.) attribute-wise.

    Each differing attribute becomes its own :class:`ConfigChange`. Secret
    attributes are always rendered as ``<changed>``.
    """
    changes: list[ConfigChange] = []
    old_dict = _to_dict(old)
    new_dict = _to_dict(new)
    keys = sorted(set(old_dict) | set(new_dict))
    for attr_name in keys:
        if attr_name in {"model_config", "model_extra", "model_fields_set"}:
            continue
        old_value: object = old_dict.get(attr_name, _MISSING)
        new_value: object = new_dict.get(attr_name, _MISSING)
        child_path = f"{field_name}.{attr_name}"
        secret = _is_secret_field(attr_name)
        if isinstance(old_value, list) or isinstance(new_value, list):
            if old_value == new_value:
                continue
            changes.append(
                ConfigChange(
                    path=child_path,
                    disposition=_disposition_for(child_path),
                    old_display=_display_collection(
                        cast("object", old_value), secret=secret
                    ),
                    new_display=_display_collection(
                        cast("object", new_value), secret=secret
                    ),
                    section=field_name.split(".", 1)[0],
                    secret=secret,
                )
            )
            continue
        if isinstance(old_value, Mapping) or isinstance(new_value, Mapping):
            if old_value == new_value:
                continue
            changes.append(
                ConfigChange(
                    path=child_path,
                    disposition=_disposition_for(child_path),
                    old_display=_display_collection(
                        cast("object", old_value), secret=secret
                    ),
                    new_display=_display_collection(
                        cast("object", new_value), secret=secret
                    ),
                    section=field_name.split(".", 1)[0],
                    secret=secret,
                )
            )
            continue
        if old_value == new_value:
            continue
        changes.append(
            ConfigChange(
                path=child_path,
                disposition=_disposition_for(child_path),
                old_display=_display(old_value, secret=secret),
                new_display=_display(new_value, secret=secret),
                section=field_name.split(".", 1)[0],
                secret=secret,
            )
        )
    return tuple(changes)


def _to_dict(value: object) -> dict[str, object]:
    """Best-effort conversion of a Pydantic model / dataclass to a dict."""
    if value is _MISSING:
        return {}
    if isinstance(value, Mapping):
        return dict(cast("Mapping[str, object]", value))
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            dumped: object = dump()
            return dict(cast("Iterable[tuple[str, object]]", dumped))
        except Exception:
            return {}
    if hasattr(value, "__dict__"):
        return {
            k: v
            for k, v in value.__dict__.items()
            if not k.startswith("_") and not callable(v)
        }
    return {}


def _display_collection(value: object, *, secret: bool) -> str:
    if isinstance(value, list | tuple):
        rendered = ", ".join(
            _display(item, secret=secret)
            for item in cast("list[object] | tuple[object, ...]", value)
        )
        return f"[{rendered}]"
    if isinstance(value, Mapping):
        items = ", ".join(
            f"{k}={_display(v, secret=_is_secret_field(k) or secret)}"
            for k, v in sorted(
                cast("Mapping[str, object]", value).items(),
            )
        )
        return "{" + items + "}"
    return _display(value, secret=secret)


def _iter_tracked_fields(config: AppConfig) -> list[tuple[str, object]]:
    """Yield ``(dotted_path, value)`` tuples spanning every tracked field."""
    fields: list[tuple[str, object]] = []
    skip_top_level = {"accounts", "providers"}
    for path in _FIELD_DISPOSITION:
        if path in skip_top_level:
            # Account/provider collections are expanded into per-key rows
            # below; the unexpanded views would mask those changes.
            continue
        parent, leaf = _path_segments(config, path)
        try:
            if isinstance(parent, Mapping):
                value: object = cast("Mapping[str, object]", parent)[leaf]
            else:
                value = getattr(parent, leaf)
        except (AttributeError, KeyError):
            continue
        fields.append((path, value))

    fields.extend(_expand_accounts(config))
    fields.extend(_expand_providers(config))
    return fields


def _expand_accounts(config: AppConfig) -> list[tuple[str, object]]:
    rows: list[tuple[str, object]] = []
    for account in config.all_accounts():
        provider_id = _resolve_provider_id(config, account)
        rows.append((f"accounts.{provider_id}/{account.name}", account))
    return rows


def _resolve_provider_id(config: AppConfig, account: object) -> str:
    name = getattr(account, "name", None)
    if not isinstance(name, str):
        return ""
    for provider_id, provider in config.providers.items():
        for candidate in provider.accounts:
            if candidate.name == name:
                return provider_id
    return ""


def _expand_providers(config: AppConfig) -> list[tuple[str, object]]:
    rows: list[tuple[str, object]] = []
    for provider_id in sorted(config.providers):
        rows.append((f"providers.{provider_id}", config.providers[provider_id]))
    return rows


def compute_diff(
    old: AppConfig,
    new: AppConfig,
) -> ConfigDiff:
    """Compute the structured diff between ``old`` and ``new``.

    Output ordering is stable: changes are sorted by ``path`` so tests can
    pin exact messages.  Each change carries an explicit disposition so the
    reload planner can decide whether to proceed or fall back to restart.
    """
    old_map = dict(_iter_tracked_fields(old))
    new_map = dict(_iter_tracked_fields(new))
    paths = sorted(set(old_map) | set(new_map))

    changes: list[ConfigChange] = []
    for path in paths:
        old_value = old_map.get(path, _MISSING)
        new_value = new_map.get(path, _MISSING)
        if old_value is _MISSING or new_value is _MISSING:
            secret = _is_secret_field(path)
            changes.append(
                ConfigChange(
                    path=path,
                    disposition=_disposition_for(path),
                    old_display="<missing>" if old_value is _MISSING else "",
                    new_display="<missing>" if new_value is _MISSING else "",
                    section=path.split(".", 1)[0],
                    secret=secret,
                )
            )
            continue
        changes.extend(_diff_single_field(path, old_value, new_value))
    changes.sort(key=lambda change: change.path)
    return ConfigDiff(changes=tuple(changes))


class _MissingType:
    """Sentinel singleton for missing diff fields."""


_MISSING = _MissingType()


# ----------------------------------------------------------------------
# Reload result types (Workstream A5)
# ----------------------------------------------------------------------


class ReloadStage(Enum):
    """Stages of a reload transaction.

    Values correspond to the milestones in the reload plan; the control
    socket that milestone C introduces speaks this enum directly.
    """

    VALIDATION = "validation"
    DIGEST_CHECK = "digest_check"
    DIFF = "diff"
    PREPARATION = "preparation"
    RECONCILIATION = "reconciliation"
    COMMIT = "commit"
    RETIREMENT = "retirement"


@dataclass(frozen=True)
class ReloadResult:
    """Structured outcome of a reload transaction (or preflight step).

    Construction is intentionally lossy on the ``AppConfig`` itself: the
    result must never leak raw config bodies or credentials.
    """

    ok: bool
    stage: ReloadStage
    generation: int | None
    changed_sections: tuple[str, ...]
    warnings: tuple[ConfigValidationWarning, ...]
    restart_required: tuple[ConfigChange, ...]
    message: str


def diff_from_validation(
    baseline_path: Path | str | None,
    candidate: ConfigValidationResult,
) -> ConfigDiff:
    """Build a :class:`ConfigDiff` against an on-disk baseline.

    ``baseline_path`` is the config currently in effect on the server.
    When ``None`` or unreadable, the diff is reported as missing-baseline
    by returning a diff containing every tracked field as added -- this
    keeps the safety path visible without throwing.
    """
    if baseline_path is None:
        baseline = None
    else:
        try:
            from eggpool.config_validation import validate_config_file

            baseline = validate_config_file(baseline_path)
        except Exception:
            baseline = None
    if baseline is None:
        return ConfigDiff(
            changes=tuple(
                ConfigChange(
                    path=path,
                    disposition=_disposition_for(path),
                    old_display="<missing-baseline>",
                    new_display="<see candidate>",
                    section=path.split(".", 1)[0],
                    secret=_is_secret_field(path),
                )
                for path, _ in _iter_tracked_fields(candidate.config)
            )
        )
    return compute_diff(baseline.config, candidate.config)


__all__ = [
    "ConfigChange",
    "ConfigDiff",
    "ReloadDisposition",
    "ReloadResult",
    "ReloadStage",
    "compute_diff",
    "diff_from_validation",
]
