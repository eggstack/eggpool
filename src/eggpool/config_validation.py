"""Reusable configuration validation for ``check-config`` and ``rehash``.

This module is the single implementation of the validation contract used by
both the ``check-config`` CLI command and the ``rehash`` preflight.  It is
deliberately Click-free and never raises ``SystemExit`` so server-side code
(milestone C's control-plane handler) can call it from any context.

Safety contract
---------------

The shape of this module is constrained by the Milestone A safety contract:

- local validation runs before any control-plane call;
- a validation failure reports a typed error and performs no state mutation;
- secrets and credentials are never echoed in ``display`` strings,
  ``message`` fields, exceptions, or logs;
- the SHA-256 :attr:`ConfigValidationResult.content_digest` is computed over
  the exact bytes the operator placed on disk so a subsequent control-plane
  call can guard against time-of-check/time-of-use drift.

Fields not explicitly classified as live-reloadable default to
restart-required; the policy lives in
:mod:`eggpool.config_reload_policy` to keep concerns separated.
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import urlsplit

from eggpool.auth import require_auth_at_startup
from eggpool.config_utils import get_section, load_raw_config
from eggpool.errors import AggregatorError, ConfigError
from eggpool.providers.contract import compose_provider_url

if TYPE_CHECKING:
    from eggpool.models.config import AppConfig


_VALIDATION_ERROR_PREFIX: Final = "configuration validation failed"


class ConfigValidationError(AggregatorError):
    """Base class for typed ``validate_config_file`` failures."""


class ConfigFileAccessError(ConfigValidationError):
    """The config file could not be read from disk."""


class ConfigParseError(ConfigValidationError):
    """The config file is missing or not valid TOML."""


class ConfigSchemaError(ConfigValidationError):
    """The TOML decoded successfully but failed Pydantic validation."""


class ConfigStartupAuthError(ConfigValidationError):
    """``require_auth_at_startup`` rejected the resolved server API key."""


class ConfigAccountCredentialError(ConfigValidationError):
    """``AppConfig.validate_account_credentials`` rejected one or more accounts."""


class ConfigInternalError(ConfigValidationError):
    """An unexpected internal failure occurred during validation."""


@dataclass(frozen=True)
class ConfigValidationWarning:
    """A non-blocking advisory surfaced by ``validate_config_file``.

    Warnings are never promoted to errors automatically; future "strict mode"
    work may opt in to treating them as fatal.
    """

    code: str
    message: str
    section: str | None = None

    def to_display(self) -> str:
        """Return a single operator-facing line."""
        prefix = f"[{self.section}] " if self.section else ""
        return f"{prefix}{self.message}"


@dataclass(frozen=True)
class ConfigValidationResult:
    """The fully validated, immutable outcome of a single validation pass.

    ``config`` is the parsed :class:`AppConfig` and ``content_digest`` is the
    SHA-256 of the exact bytes that produced it.  ``runtime_fingerprint`` is
    the deterministic, secret-safe digest derived from a redacted canonical
    projection; it is intended for diagnostics and semantic no-op detection
    rather than integrity protection.  Fields that intentionally do **not**
    enter the fingerprint are documented on
    :data:`_RUNTIME_FINGERPRINT_OMITTED` below.
    """

    config: AppConfig
    source_path: Path
    content_digest: str
    runtime_fingerprint: str
    warnings: tuple[ConfigValidationWarning, ...] = field(default_factory=tuple)


# Fields intentionally excluded from the runtime fingerprint.
#
# The fingerprint is a deterministic, secret-safe digest for diagnostics and
# no-op detection. Anything that ends up here MUST NOT influence routing,
# quota, auth, or any other behaviorally relevant calculation -- if it does,
# omission here would cause incorrect "no changes" answers.
#
#  - ``api_key``: raw credential value lives inline or in an env var.
#  - ``api_key_env`` / ``proxy_url_env`` / ``model_info.api_key``: env var
#    names that could in principle be revealed via future server-side
#    tooling; treated as secret-adjacent.
#  - ``pricing_catalogs.*.api_key`` / external catalog credentials.
#  - ``accounts[*].api_key`` / ``accounts[*].proxy_url`` values.
_RUNTIME_FINGERPRINT_OMITTED: Final = frozenset(
    {
        "api_key",
        "api_key_env",
        "proxy_url_env",
        "proxy_url",
    }
)


@dataclass(frozen=True)
class _FingerprintSection:
    """Ordered, secret-safe representation of one TOML section."""

    name: str
    entries: tuple[tuple[str, object], ...]


def _canonical_section(name: str, value: object) -> _FingerprintSection | None:
    """Project ``value`` into a redacted, deterministically-ordered view.

    Returns ``None`` when ``value`` is empty so the fingerprint can omit
    zero-impact defaults from the digest without losing them entirely.
    """
    if isinstance(value, Mapping):
        items: list[tuple[str, object]] = []
        value_map: Mapping[str, object] = cast("Mapping[str, object]", value)
        keys: list[str] = [str(k) for k in sorted(value_map.keys())]
        for key in keys:
            if key in _RUNTIME_FINGERPRINT_OMITTED:
                items.append((key, "<redacted>"))
                continue
            projected: object | None = _canonical_value(value_map[key])
            if projected is None:
                continue
            items.append((key, projected))
        if not items:
            return None
        return _FingerprintSection(name=name, entries=tuple(items))
    if isinstance(value, list):
        rendered: list[object] = []
        for item in cast("list[object]", value):
            projected: object | None = _canonical_value(item)
            if projected is None:
                continue
            rendered.append(projected)
        if not rendered:
            return None
        return _FingerprintSection(name=name, entries=(("list", rendered),))
    projected = _canonical_value(value)
    if projected is None:
        return None
    return _FingerprintSection(name=name, entries=((name, projected),))


def _canonical_value(value: object) -> object | None:
    """Best-effort canonical rendering of one ``AppConfig`` field value.

    Order is not semantically meaningful for account/provider/header/header-like
    lists, so those are sorted by an immutable key (``name`` for accounts,
    ``(id, name)`` for headers, etc.) before being projected.  Returns
    ``None`` when the value carries no information past empty-defaults.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        items: dict[str, object] = {}
        keys: list[str] = [
            str(k) for k in sorted(cast("Mapping[str, object]", value).keys())
        ]
        for key in keys:
            if key in _RUNTIME_FINGERPRINT_OMITTED:
                items[key] = "<redacted>"
                continue
            projected: object | None = _canonical_value(
                cast("Mapping[str, object]", value)[key]
            )
            if projected is None:
                continue
            items[key] = projected
        return items or None
    if isinstance(value, list):
        rendered: list[object] = []
        for item in cast("list[object]", value):
            projected: object | None = _canonical_value(item)
            if projected is None:
                continue
            rendered.append(projected)
        return rendered or None
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value
    return repr(value)


def _ordered_accounts(config: AppConfig) -> list[object]:
    """Account list, ordered by ``(provider_id, name)`` for fingerprinting."""
    rows: list[tuple[str, str, dict[str, object]]] = []
    for provider_id in sorted(config.providers):
        provider = config.providers[provider_id]
        for account in sorted(provider.accounts, key=lambda acct: acct.name):
            projection: dict[str, object] = {
                "name": account.name,
                "provider_id": provider_id,
                "enabled": account.enabled,
                "weight": account.weight,
                "five_hour_offset_microdollars": account.five_hour_offset_microdollars,
                "weekly_offset_microdollars": account.weekly_offset_microdollars,
                "monthly_offset_microdollars": account.monthly_offset_microdollars,
                "proxy": account.proxy,
                "proxy_url_env": "<redacted>" if account.proxy_url_env else None,
                "proxy_url": "<redacted>" if account.proxy_url else None,
            }
            raw_key = account.api_key
            if raw_key:
                projection["api_key"] = "<redacted>"
            elif account.api_key_env:
                projection["api_key_env"] = account.api_key_env
            rows.append((provider_id, account.name, projection))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in rows]


def _ordered_providers(config: AppConfig) -> list[object]:
    """Providers ordered by id; headers and static models also sorted."""
    rendered: list[object] = []
    for provider_id in sorted(config.providers):
        provider = config.providers[provider_id]
        headers = sorted(
            (
                {
                    "name": h.name,
                    "value": "<redacted>" if _looks_like_secret(h.name) else h.value,
                }
                for h in provider.headers
            ),
            key=lambda row: row["name"] or "",
        )
        static_models = sorted(
            (
                {
                    "id": sm.id,
                    "protocol": sm.protocol,
                    "max_context_tokens": sm.max_context_tokens,
                    "supports_tools": sm.supports_tools,
                    "supports_vision": sm.supports_vision,
                }
                for sm in provider.static_models
            ),
            key=lambda row: row["id"] or "",
        )
        rendered.append(
            {
                "id": provider_id,
                "base_url": provider.base_url,
                "routing_priority": provider.routing_priority,
                "protocols": sorted(provider.protocols),
                "openai_path": provider.openai_path,
                "anthropic_path": provider.anthropic_path,
                "headers": headers,
                "static_models": static_models,
                "auth": {
                    "mode": provider.auth.mode,
                    "header": provider.auth.header,
                    "scheme": provider.auth.scheme,
                },
            }
        )
    return rendered


_SECRET_HEADER_NAMES: Final = frozenset(
    {"authorization", "x-api-key", "api-key", "x-auth-token"}
)


def _looks_like_secret(header_name: str) -> bool:
    return header_name.casefold() in _SECRET_HEADER_NAMES


def compute_runtime_fingerprint(config: AppConfig) -> str:
    """Compute the deterministic, secret-safe runtime fingerprint.

    The fingerprint is stable across repeated parses of semantically
    equivalent configurations: account / provider / header / static-model
    collections are sorted by an immutable key so order does not change
    the digest.  Credentials and env-var names that map to secrets are
    replaced with ``"<redacted>"`` so the fingerprint never exposes
    sensitive material.

    Fields that are intentionally omitted from the fingerprint are
    catalogued in :data:`_RUNTIME_FINGERPRINT_OMITTED`.  Add new fields
    only after confirming they do not influence behaviorally-relevant
    runtime decisions.
    """
    sections: list[tuple[str, object]] = []

    server_section = _canonical_section(
        "server",
        {
            "host": config.server.host,
            "port": config.server.port,
            "api_key_env": config.server.api_key_env,
            "log_level": config.server.log_level,
            "access_log": config.server.access_log,
            "threads": config.server.threads,
        },
    )
    if server_section is not None:
        sections.append((server_section.name, server_section.entries))

    for section_name in (
        "upstream",
        "database",
        "models",
        "routing",
        "limits",
        "pricing",
        "dashboard",
        "security",
        "metrics",
        "backup",
        "dns_cache",
        "network",
        "transcoder",
        "compression",
        "cache",
        "model_info",
    ):
        section = getattr(config, section_name)
        projection = _canonical_section(section_name, section.model_dump())
        if projection is not None:
            sections.append((projection.name, projection.entries))

    section = _canonical_section("providers", _ordered_providers(config))
    if section is not None:
        sections.append((section.name, section.entries))

    section = _canonical_section("accounts", _ordered_accounts(config))
    if section is not None:
        sections.append((section.name, section.entries))

    section = _canonical_section(
        "proxies",
        [
            {"name": name, "url_env": proxy.url_env}
            for name, proxy in sorted(config.proxies.items())
        ],
    )
    if section is not None:
        sections.append((section.name, section.entries))

    section = _canonical_section(
        "model_overrides",
        {key: True for key in sorted(config.model_overrides)},
    )
    if section is not None:
        sections.append((section.name, section.entries))

    section = _canonical_section(
        "model_capabilities",
        {key: True for key in sorted(config.model_capabilities)},
    )
    if section is not None:
        sections.append((section.name, section.entries))

    section = _canonical_section(
        "force_segmentation",
        config.force_segmentation,
    )
    if section is not None:
        sections.append((section.name, section.entries))

    sections.sort(key=lambda row: row[0])
    payload = repr(sections).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _check_stale_contracts_typed(
    config: AppConfig, config_path: Path
) -> tuple[ConfigValidationWarning, ...]:
    """Typed sibling of ``cli_full._check_stale_contracts``.

    Returns the same set of advisories as the legacy string-list helper,
    but as structured dataclasses so callers do not need to parse free-form
    text.  Mirrors the legacy implementation one-for-one to keep behaviour
    stable.
    """
    warnings: list[ConfigValidationWarning] = []
    raw = load_raw_config(str(config_path))
    raw_providers_section = get_section(raw, "providers")
    raw_providers: dict[str, object] = raw_providers_section

    for provider in config.providers.values():
        endpoint = provider.models_endpoint
        raw_section_obj: object = raw_providers.get(provider.id, {})
        raw_section: dict[str, object] = cast(
            "dict[str, object]",
            raw_section_obj if isinstance(raw_section_obj, dict) else {},
        )

        if (
            endpoint is not None
            and endpoint.method == "DISABLED"
            and not provider.static_models
        ):
            warnings.append(
                ConfigValidationWarning(
                    code="provider.disabled_endpoint_no_static_models",
                    section=f"providers.{provider.id}",
                    message=(
                        "models_endpoint is DISABLED but static_models is "
                        "empty; the catalog will not list any models from "
                        "this provider"
                    ),
                )
            )

        if (
            endpoint is not None
            and endpoint.method == "DISABLED"
            and provider.verify.require_models
        ):
            warnings.append(
                ConfigValidationWarning(
                    code="provider.disabled_endpoint_require_models",
                    section=f"providers.{provider.id}",
                    message=(
                        "models_endpoint is DISABLED but verify.require_models "
                        "is true; the contract is contradictory"
                    ),
                )
            )

        if (
            "anthropic_path" in raw_section
            and provider.anthropic_path
            and "anthropic" not in provider.protocols
        ):
            warnings.append(
                ConfigValidationWarning(
                    code="provider.anthropic_path_unused",
                    section=f"providers.{provider.id}",
                    message=(
                        "anthropic_path is set but 'anthropic' is not in "
                        "protocols; the field will be ignored"
                    ),
                )
            )

        if (
            "openai_path" in raw_section
            and provider.openai_path
            and "openai" not in provider.protocols
        ):
            warnings.append(
                ConfigValidationWarning(
                    code="provider.openai_path_unused",
                    section=f"providers.{provider.id}",
                    message=(
                        "openai_path is set but 'openai' is not in protocols; "
                        "the field will be ignored"
                    ),
                )
            )

        parsed_base = urlsplit(provider.base_url)
        if (
            parsed_base.hostname == "api.minimax.io"
            and "openai" in provider.protocols
            and parsed_base.path.rstrip("/") != "/anthropic"
        ):
            warnings.append(
                ConfigValidationWarning(
                    code="provider.minimax_openai_surface",
                    section=f"providers.{provider.id}",
                    message=(
                        "api.minimax.io token-plan keys should use the "
                        "Anthropic-compatible MiniMax contract "
                        "(base_url='https://api.minimax.io/anthropic', "
                        "protocols=['anthropic'], anthropic_path='/v1/messages', "
                        "auth.header='x-api-key'); the OpenAI "
                        "/v1/chat/completions surface can return upstream "
                        "'insufficient balance (1008)' for token-plan keys"
                    ),
                )
            )

        if endpoint is not None:
            try:
                compose_provider_url(provider, endpoint.path)
            except ConfigError:
                warnings.append(
                    ConfigValidationWarning(
                        code="provider.duplicate_v1_segment",
                        section=f"providers.{provider.id}",
                        message=(
                            "base_url + models_endpoint.path produces a "
                            "duplicate /v1 segment; see docs/providers.md"
                        ),
                    )
                )

        if provider.auth.mode != "none":
            for header in provider.headers:
                if header.name.casefold() == "authorization":
                    warnings.append(
                        ConfigValidationWarning(
                            code="provider.static_authorization_header",
                            section=f"providers.{provider.id}",
                            message=(
                                f"static header 'Authorization' is set but "
                                f"auth.mode is '{provider.auth.mode}'; the "
                                "auth header will be replaced"
                            ),
                        )
                    )
                    break

        if (
            provider.auth.mode == "api_key"
            and provider.auth.header == "Authorization"
            and "anthropic" in provider.protocols
        ):
            warnings.append(
                ConfigValidationWarning(
                    code="provider.anthropic_authorization_header",
                    section=f"providers.{provider.id}",
                    message=(
                        "auth.mode='api_key' with header='Authorization' "
                        "looks wrong; Anthropic-compatible providers "
                        "typically use header='x-api-key'"
                    ),
                )
            )

        if raw_section:
            has_legacy_key = (
                "models_method" in raw_section or "models_path" in raw_section
            )
            has_endpoint_table = "models_endpoint" in raw_section
            if has_legacy_key and not has_endpoint_table:
                warnings.append(
                    ConfigValidationWarning(
                        code="provider.legacy_models_method",
                        section=f"providers.{provider.id}",
                        message=(
                            "using legacy models_method/models_path; consider "
                            f"migrating to [[providers.{provider.id}.models_endpoint]]"
                        ),
                    )
                )

    return tuple(warnings)


def validate_config_file(path: str | Path) -> ConfigValidationResult:
    """Validate ``path`` and return an immutable structured result.

    The function performs no I/O outside the immediate config path and is
    safe to call from server-side code, the ``check-config`` Click command,
    and the ``rehash`` CLI preflight.  It never raises ``SystemExit`` and
    never imports Click.

    Raises:
        ConfigFileAccessError: ``path`` could not be opened or read.
        ConfigParseError: TOML decoding raised :class:`tomllib.TOMLDecodeError`.
        ConfigSchemaError: Pydantic model validation rejected the config.
        ConfigStartupAuthError: ``require_auth_at_startup`` raised.
        ConfigAccountCredentialError: account credential check raised.
        ConfigInternalError: any other unexpected exception was caught.
    """
    config_path = Path(path).resolve()
    try:
        raw_bytes = config_path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigFileAccessError(
            f"{_VALIDATION_ERROR_PREFIX}: config file not found: {config_path}"
        ) from exc
    except OSError as exc:
        raise ConfigFileAccessError(
            f"{_VALIDATION_ERROR_PREFIX}: cannot read {config_path}: {exc}"
        ) from exc

    content_digest = hashlib.sha256(raw_bytes).hexdigest()

    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigParseError(
            f"{_VALIDATION_ERROR_PREFIX}: invalid TOML in {config_path}: {exc}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ConfigParseError(
            f"{_VALIDATION_ERROR_PREFIX}: {config_path} is not valid UTF-8: {exc}"
        ) from exc

    from eggpool.models.config import AppConfig

    try:
        config = AppConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigSchemaError(
            f"{_VALIDATION_ERROR_PREFIX}: schema validation failed: {exc}"
        ) from exc

    try:
        require_auth_at_startup(config.server.resolved_api_key)
    except RuntimeError as exc:
        raise ConfigStartupAuthError(
            f"{_VALIDATION_ERROR_PREFIX}: server API key rejected: {exc}"
        ) from exc

    try:
        config.validate_account_credentials()
    except ConfigError as exc:
        raise ConfigAccountCredentialError(
            f"{_VALIDATION_ERROR_PREFIX}: account credential check failed: {exc}"
        ) from exc
    except Exception as exc:
        raise ConfigInternalError(
            f"{_VALIDATION_ERROR_PREFIX}: account credential check failed: {exc}"
        ) from exc

    try:
        config.validate_optional_dependencies()
    except ConfigError as exc:
        raise ConfigSchemaError(
            f"{_VALIDATION_ERROR_PREFIX}: optional dependency check failed: {exc}"
        ) from exc

    warnings = _check_stale_contracts_typed(config, config_path)

    try:
        runtime_fingerprint = compute_runtime_fingerprint(config)
    except Exception:
        runtime_fingerprint = ""

    return ConfigValidationResult(
        config=config,
        source_path=config_path,
        content_digest=content_digest,
        runtime_fingerprint=runtime_fingerprint,
        warnings=warnings,
    )


def check_stale_contracts(
    config: AppConfig, config_path: str | Path
) -> tuple[ConfigValidationWarning, ...]:
    """Public typed stale-contract advisory helper.

    Mirrors the legacy ``cli_full._check_stale_contracts`` shape (string
    return was the historical contract) but returns structured
    :class:`ConfigValidationWarning` records.  New code should call this
    directly instead of importing the underscore-prefixed helper from
    :mod:`eggpool.cli`.
    """
    return _check_stale_contracts_typed(config, Path(config_path))


__all__ = [
    "ConfigAccountCredentialError",
    "ConfigFileAccessError",
    "ConfigInternalError",
    "ConfigParseError",
    "ConfigSchemaError",
    "ConfigStartupAuthError",
    "ConfigValidationError",
    "ConfigValidationResult",
    "ConfigValidationWarning",
    "check_stale_contracts",
    "compute_runtime_fingerprint",
    "validate_config_file",
]
