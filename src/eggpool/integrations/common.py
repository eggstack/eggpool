"""Shared infrastructure for integration renderers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from eggpool.catalog.capabilities import (
    apply_capability_overrides,
    dict_to_model_capabilities,
    model_capabilities_to_dict,
)
from eggpool.catalog.limits import ModelLimitResolver
from eggpool.config_utils import (
    detect_lan_ip,
    read_server_port,
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


@dataclass(frozen=True)
class ConfigsetupTargetSpec:
    """Metadata describing a configsetup target's capabilities."""

    name: str
    requires_model: bool
    mode: Literal["env", "json", "toml", "yaml", "instructions"]
    supports_dynamic_models: bool = False
    supports_direct_write: bool = False
    default_write_path: str | None = None


TARGET_SPECS: dict[str, ConfigsetupTargetSpec] = {
    "aider": ConfigsetupTargetSpec(name="aider", requires_model=False, mode="env"),
    "codex": ConfigsetupTargetSpec(name="codex", requires_model=False, mode="toml"),
    "qwen-code": ConfigsetupTargetSpec(
        name="qwen-code", requires_model=False, mode="json"
    ),
    "kilo": ConfigsetupTargetSpec(name="kilo", requires_model=False, mode="json"),
    "continue": ConfigsetupTargetSpec(
        name="continue", requires_model=True, mode="yaml"
    ),
    "cline": ConfigsetupTargetSpec(name="cline", requires_model=False, mode="json"),
    "roo-code": ConfigsetupTargetSpec(
        name="roo-code", requires_model=False, mode="json"
    ),
    "goose": ConfigsetupTargetSpec(name="goose", requires_model=True, mode="env"),
    "openhands": ConfigsetupTargetSpec(
        name="openhands", requires_model=True, mode="env"
    ),
}

_BARE_TOML_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")


def render_toml_string(value: str) -> str:
    """Render *value* as a TOML basic string."""
    return json.dumps(value, ensure_ascii=False)


def render_toml_key(value: str) -> str:
    """Render *value* as a TOML key segment.

    TOML bare keys cannot contain slashes, dots, spaces, quotes, or most
    provider/model punctuation. Quoting every unsafe segment avoids accidentally
    creating nested dotted tables or unparsable snippets.
    """
    if _BARE_TOML_KEY_RE.fullmatch(value):
        return value
    return render_toml_string(value)


def render_yaml_string(value: str) -> str:
    """Render *value* as a YAML double-quoted scalar."""
    return json.dumps(value, ensure_ascii=False)


def resolve_optional_model(
    ctx: IntegrationContext, model: str | None = None
) -> str | None:
    """Return explicit model or the sole catalog model, if unambiguous."""
    return model or select_default_model(ctx)


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


def _apply_capabilities(
    models_data: list[dict[str, Any]],
    config: AppConfig,
) -> list[dict[str, Any]]:
    """Apply configured capability overrides to integration catalog rows."""
    if not models_data:
        return models_data
    global_overrides: dict[str, dict[str, object]] = {
        k: cast("dict[str, object]", v.model_dump(exclude_none=True))
        for k, v in config.model_capabilities.items()
    }
    provider_overrides_by_id: dict[str, dict[str, dict[str, object]]] = {
        provider_id: {
            k: cast("dict[str, object]", v.model_dump(exclude_none=True))
            for k, v in provider_cfg.model_capabilities.items()
        }
        for provider_id, provider_cfg in config.providers.items()
    }
    for model in models_data:
        model_id = str(model.get("base_model_id") or model.get("model_id") or "")
        if not model_id:
            continue
        provider_raw = model.get("provider_id")
        provider_id = provider_raw if isinstance(provider_raw, str) else None
        base_capabilities_raw = model.get("capabilities", {})
        base_capabilities_dict = (
            cast("dict[str, object]", base_capabilities_raw)
            if isinstance(base_capabilities_raw, dict)
            else {}
        )
        provider_overrides = (
            provider_overrides_by_id.get(provider_id, {})
            if provider_id is not None
            else {}
        )
        base_capabilities = dict_to_model_capabilities(base_capabilities_dict)
        final_capabilities = apply_capability_overrides(
            model_id,
            base_capabilities,
            global_overrides,
            provider_overrides,
            provider_id=provider_id,
        )
        structured = model_capabilities_to_dict(final_capabilities)
        merged: dict[str, object] = dict(base_capabilities_dict)
        thinking = structured.get("thinking")
        if thinking is not None:
            merged["thinking"] = thinking
        else:
            merged.pop("thinking", None)
        model["capabilities"] = merged
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
        for m in models_data:
            provider_id = m.get("provider_id")
            eff = resolver.resolve(
                provider_id=provider_id,
                model_id=m.get("base_model_id", m["model_id"]),
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
    return models_data


def _openai_client_needs_transcoder(config: AppConfig) -> bool:
    """Check if any enabled provider requires transcoding for OpenAI clients."""
    for _provider_id, provider_cfg in config.providers.items():
        if not any(account.enabled for account in provider_cfg.accounts):
            continue
        protocols = getattr(provider_cfg, "protocols", [])
        if "openai" not in protocols and "anthropic" in protocols:
            return True
    return False


def _persist_transcoder_enabled(config_path: str, config: AppConfig) -> bool:
    """Persist [transcoder].enabled = true to the TOML file if needed.

    Returns True if the file was mutated.
    """
    from pathlib import Path

    from eggpool.toml_edit import update_section_value

    if config.transcoder.enabled:
        return False
    if not _openai_client_needs_transcoder(config):
        return False

    path = Path(config_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    result = update_section_value(
        lines,
        "transcoder",
        "enabled",
        "true",
        insert_missing_key=True,
        append_missing_section=True,
    )
    path.write_text("\n".join(result.lines) + "\n", encoding="utf-8")
    return True


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
    from eggpool.config_utils import resolve_server_api_key

    key_resolution = resolve_server_api_key(config_path)
    key = key_resolution.api_key
    config_mutated = key_resolution.config_mutated

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
        models_data = _apply_capabilities(models_data, config)
        models_data = _apply_limits(models_data, config, collapse_models)
        if enable_transcoder_for_openai_clients:
            transcoder_mutated = _persist_transcoder_enabled(config_path, config)

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
    target: str,
    model: str | None,
    ctx: IntegrationContext,
    *,
    write_mode: bool = False,
) -> str | None:
    """Resolve the model for a target integration.

    Returns the explicit *model* if given, the default if exactly one
    model exists, or None to let the CLI layer prompt.

    When *write_mode* is ``True`` and the target spec has
    ``requires_model=True``, raises ``click.ClickException`` if the
    model is ambiguous (multiple catalog models and no ``--model``).
    """
    if model is not None:
        return model
    default = select_default_model(ctx)
    if default is not None:
        return default
    spec = TARGET_SPECS.get(target)
    if write_mode and spec is not None and spec.requires_model and len(ctx.models) != 1:
        import click

        raise click.ClickException(
            f"Error: --model is required for {target!r} in write mode "
            f"because the catalog has {len(ctx.models)} models. "
            f"Run with --model <name> to select one."
        )
    return None
