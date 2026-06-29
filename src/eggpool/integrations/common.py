"""Shared infrastructure for integration renderers."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

from eggpool.catalog.limits import (
    EffectiveModelLimits,
    ModelLimitResolver,
    conservative_limits,
)
from eggpool.config_utils import (
    detect_lan_ip,
    generate_api_key,
    read_server_api_key,
    read_server_port,
    write_server_api_key,
)
from eggpool.db.connection import Database
from eggpool.models.config import AppConfig


@dataclass(frozen=True)
class IntegrationContext:
    """Immutable context shared by all integration renderers."""

    config_path: str
    api_key: str
    base_url: str
    base_url_root: str
    host: str
    port: int
    models: list[dict[str, Any]] = field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list
    )
    collapse_models: bool = False
    config_mutated: bool = False
    transcoder_mutated: bool = False


def _load_catalog(config: AppConfig, collapse_models: bool) -> list[dict[str, Any]]:
    """Load model catalog from the database."""
    db_path = config.database.path
    models_data: list[dict[str, Any]] = []

    async def _fetch() -> list[dict[str, Any]]:
        db = Database(db_path)
        await db.connect()
        try:
            if collapse_models:
                rows = await db.fetch_all(
                    "SELECT model_id, display_name, capabilities, "
                    "source_metadata FROM models"
                )
                out: list[dict[str, Any]] = []
                for row in rows:
                    caps_raw = row["capabilities"]
                    meta_raw = row["source_metadata"]
                    caps: dict[str, Any] = json.loads(caps_raw) if caps_raw else {}
                    meta: dict[str, Any] = json.loads(meta_raw) if meta_raw else {}
                    out.append(
                        {
                            "model_id": row["model_id"],
                            "display_name": row["display_name"],
                            "capabilities": caps,
                            "source_metadata": meta,
                            "effective_limits": {},
                        }
                    )
                return out

            rows = await db.fetch_all(
                """
                SELECT DISTINCT
                    am.model_id,
                    a.provider_id,
                    COALESCE(pmm.display_name, m.display_name)
                        AS display_name,
                    COALESCE(pmm.capabilities, m.capabilities)
                        AS capabilities,
                    COALESCE(pmm.source_metadata, m.source_metadata)
                        AS source_metadata
                FROM account_models am
                JOIN accounts a ON a.id = am.account_id
                JOIN models m ON m.model_id = am.model_id
                LEFT JOIN provider_model_metadata pmm
                    ON pmm.model_id = am.model_id
                   AND pmm.provider_id = a.provider_id
                WHERE am.enabled = 1
                  AND a.enabled = 1
                  AND am.model_id <> '__deprecated__'
                  AND COALESCE(pmm.protocol, m.protocol)
                      IN ('openai', 'anthropic')
                """
            )
            if not rows:
                rows = await db.fetch_all(
                    """
                    SELECT model_id, provider_id, display_name,
                           capabilities, source_metadata
                    FROM provider_model_metadata
                    WHERE model_id <> '__deprecated__'
                      AND protocol IN ('openai', 'anthropic')
                    """
                )
            out = []
            for row in rows:
                caps_raw = row["capabilities"]
                meta_raw = row["source_metadata"]
                caps: dict[str, Any] = json.loads(caps_raw) if caps_raw else {}
                meta: dict[str, Any] = json.loads(meta_raw) if meta_raw else {}
                base_model_id = row["model_id"]
                provider_id = row["provider_id"]
                out.append(
                    {
                        "model_id": (
                            f"{base_model_id}/{provider_id}"
                            if provider_id
                            else base_model_id
                        ),
                        "base_model_id": base_model_id,
                        "provider_id": provider_id,
                        "display_name": row["display_name"],
                        "capabilities": caps,
                        "source_metadata": meta,
                        "effective_limits": {},
                    }
                )
            return out
        finally:
            await db.disconnect()

    models_data: list[dict[str, Any]] = asyncio.run(_fetch())
    return models_data


def _merge_static_models(
    models_data: list[dict[str, Any]],
    config: AppConfig,
    collapse_models: bool,
) -> list[dict[str, Any]]:
    """Merge static models from provider config into the catalog."""
    seen = {str(m.get("model_id")) for m in models_data}
    for provider_id, provider_cfg in config.providers.items():
        if not provider_cfg.static_models:
            continue
        if not any(account.enabled for account in provider_cfg.accounts):
            continue
        for static in provider_cfg.static_models:
            exposed_id = static.id if collapse_models else f"{static.id}/{provider_id}"
            if exposed_id in seen:
                continue
            capabilities: dict[str, Any] = {}
            if static.supports_tools is not None:
                capabilities["supports_tools"] = static.supports_tools
            if static.supports_vision is not None:
                capabilities["supports_vision"] = static.supports_vision
            if static.max_context_tokens is not None:
                capabilities["max_context_tokens"] = static.max_context_tokens
            if static.max_input_tokens is not None:
                capabilities["max_input_tokens"] = static.max_input_tokens
            if static.max_output_tokens is not None:
                capabilities["max_output_tokens"] = static.max_output_tokens
            models_data.append(
                {
                    "model_id": exposed_id,
                    "base_model_id": static.id,
                    "provider_id": provider_id,
                    "display_name": static.display_name or static.id,
                    "capabilities": capabilities,
                    "source_metadata": {
                        **static.source_metadata,
                        "source": "static_config",
                    },
                    "effective_limits": {},
                }
            )
            seen.add(exposed_id)
    return models_data


def _apply_limits(
    models_data: list[dict[str, Any]],
    config: AppConfig,
    collapse_models: bool,
) -> list[dict[str, Any]]:
    """Apply effective limits from config overrides and upstream metadata."""
    if not models_data:
        return models_data
    resolver = ModelLimitResolver(config)
    if collapse_models:
        for m in models_data:
            eff = resolver.resolve(
                provider_id=None,
                model_id=m["model_id"],
                capabilities=m.get("capabilities", {}),
                source_metadata=m.get("source_metadata", {}),
            )
            m["effective_limits"] = {
                "context_tokens": eff.context_tokens,
                "input_tokens": eff.input_tokens,
                "output_tokens": eff.output_tokens,
                "enforce": eff.enforce,
                "context_source": eff.context_source,
                "input_source": eff.input_source,
                "output_source": eff.output_source,
            }
    else:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for m in models_data:
            base = m.get("base_model_id", m["model_id"])
            grouped.setdefault(base, []).append(m)
        for entries in grouped.values():
            limits_list: list[EffectiveModelLimits] = []
            for m in entries:
                provider_id = m.get("provider_id")
                eff = resolver.resolve(
                    provider_id=provider_id,
                    model_id=m.get("base_model_id", m["model_id"]),
                    capabilities=m.get("capabilities", {}),
                    source_metadata=m.get("source_metadata", {}),
                )
                limits_list.append(
                    EffectiveModelLimits(
                        context_tokens=eff.context_tokens,
                        input_tokens=eff.input_tokens,
                        output_tokens=eff.output_tokens,
                        enforce=eff.enforce,
                        context_source=eff.context_source,
                        input_source=eff.input_source,
                        output_source=eff.output_source,
                    )
                )
            merged = conservative_limits(limits_list)
            merged_dict = {
                "context_tokens": merged.context_tokens,
                "input_tokens": merged.input_tokens,
                "output_tokens": merged.output_tokens,
                "enforce": merged.enforce,
                "context_source": merged.context_source,
                "input_source": merged.input_source,
                "output_source": merged.output_source,
            }
            for m in entries:
                m["effective_limits"] = merged_dict
    return models_data


def _enable_transcoder(config: AppConfig) -> bool:
    """Enable transcoding if it was disabled in config. Returns True if mutated."""
    if config.transcoder.enabled is False:
        config.transcoder.enabled = True
        return True
    return False


def build_integration_context(
    *,
    config_path: str,
    require_catalog: bool = False,
    enable_transcoder_for_openai_clients: bool = True,
) -> IntegrationContext:
    """Build a shared context for all integration renderers.

    Centralizes key read/generate/persist, port/LAN IP/base URL
    construction, catalog DB loading, static model fallback, effective
    limits, and transcoder enablement.
    """
    config_mutated = False
    key = read_server_api_key(config_path)
    if not key:
        key = generate_api_key()
        success, _warning = write_server_api_key(config_path, key)
        if not success:
            raise OSError(
                f"Cannot persist new API key to {config_path}. "
                "Refusing to proceed without a durable key."
            )
        config_mutated = True

    port = read_server_port(config_path)
    lan_ip = detect_lan_ip()
    base_url = f"http://{lan_ip}:{port}/v1"
    base_url_root = f"http://{lan_ip}:{port}"

    models_data: list[dict[str, Any]] = []
    collapse_models = False
    transcoder_mutated = False
    config: AppConfig | None = None

    with contextlib.suppress(Exception):
        config = AppConfig.from_toml(config_path)

    if config is not None:
        collapse_models = config.models.collapse_models
        with contextlib.suppress(Exception):
            models_data = _load_catalog(config, collapse_models)
        models_data = _merge_static_models(models_data, config, collapse_models)
        models_data = _apply_limits(models_data, config, collapse_models)
        if enable_transcoder_for_openai_clients:
            transcoder_mutated = _enable_transcoder(config)

    return IntegrationContext(
        config_path=config_path,
        api_key=key,
        base_url=base_url,
        base_url_root=base_url_root,
        host=lan_ip,
        port=port,
        models=models_data,
        collapse_models=collapse_models,
        config_mutated=config_mutated,
        transcoder_mutated=transcoder_mutated,
    )


def list_catalog_model_ids(ctx: IntegrationContext) -> list[str]:
    """Return sorted list of model IDs from the catalog."""
    return sorted(m["model_id"] for m in ctx.models)


def select_default_model(ctx: IntegrationContext) -> str | None:
    """Return the default model ID, or None if ambiguous.

    Conservative: only returns a value when exactly one model exists.
    """
    if len(ctx.models) == 1:
        return ctx.models[0]["model_id"]
    return None


def require_model_for_target(
    target: str, model: str | None, ctx: IntegrationContext
) -> str | None:
    """Resolve the model for a target integration.

    Returns the explicit *model* if given, the default if exactly one
    model exists, or None to let the CLI layer prompt.
    """
    if model is not None:
        return model
    return select_default_model(ctx)
