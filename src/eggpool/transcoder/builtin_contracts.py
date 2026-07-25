"""Built-in provider-bound thinking control contracts.

This module contains manually curated contracts for known provider
deployments where the capability metadata alone is insufficient to
determine the accepted thinking controls.  These contracts serve as
the authoritative fallback when catalog/model-info data is missing
or ambiguous.

Contract precedence (highest to lowest):

1. Operator overrides (``[model_capabilities."<model>".thinking.control_contract]``).
2. Built-in contracts from this module.
3. Inferred contracts from capability metadata.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    ThinkingControlContract,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderContractKey:
    """Lookup key for a built-in provider contract.

    Contracts are scoped by provider base URL pattern, model identity,
    and protocol endpoint to avoid false positives.
    """

    provider_base_url_pattern: str
    model_id_pattern: str
    protocol: str = ""


@dataclass(frozen=True, slots=True)
class BuiltinProviderContract:
    """A manually curated provider-bound thinking control contract."""

    key: ProviderContractKey
    contract: ThinkingControlContract


# ---------------------------------------------------------------------------
# OpenCode Go MiniMax-M3 contract
# ---------------------------------------------------------------------------

# OpenCode Go routes MiniMax-M3 through api.minimax.io with
# Anthropic-compatible protocol.  MiniMax-M3 does NOT accept
# client-selectable effort or budget controls — reasoning is
# enabled at a fixed level.
_OPENCODE_GO_MINIMAX_CONTRACT = BuiltinProviderContract(
    key=ProviderContractKey(
        provider_base_url_pattern=r".*api\.minimax\.io.*",
        model_id_pattern=r".*minimax.*m3.*|.*m3.*minimax.*",
        protocol="anthropic",
    ),
    contract=ThinkingControlContract(
        mode="fixed",
        request_fields=[],
        accepted_efforts=[],
        historical_reasoning_content="accepted",
        source="manual_override",
        effort_to_budget_tokens=None,
        explicit_budget_min=None,
        explicit_budget_max=None,
    ),
)

# MiniMax's own native Anthropic endpoint — may accept different
# controls than the OpenCode Go deployment.
_MINIMAX_NATIVE_CONTRACT = BuiltinProviderContract(
    key=ProviderContractKey(
        provider_base_url_pattern=r".*minimax\.io/anthropic.*",
        model_id_pattern=r".*minimax.*m3.*|.*m3.*minimax.*",
        protocol="anthropic",
    ),
    contract=ThinkingControlContract(
        mode="effort",
        request_fields=["thinking"],
        accepted_efforts=["low", "medium", "high"],
        effort_aliases={"med": "medium"},
        effort_to_budget_tokens={"low": 1024, "medium": 4096, "high": 16384},
        historical_reasoning_content="accepted",
        source="manual_override",
    ),
)

# Anthropic native — full effort/budget control.
_ANTHROPIC_NATIVE_CONTRACT = BuiltinProviderContract(
    key=ProviderContractKey(
        provider_base_url_pattern=r".*api\.anthropic\.com.*",
        model_id_pattern=r".*",
        protocol="anthropic",
    ),
    contract=ThinkingControlContract(
        mode="effort_or_budget",
        request_fields=["thinking"],
        accepted_efforts=["low", "medium", "high"],
        effort_aliases={"med": "medium"},
        effort_to_budget_tokens={"low": 1024, "medium": 4096, "high": 16384},
        explicit_budget_min=1024,
        explicit_budget_max=128000,
        historical_reasoning_content="accepted",
        source="provider_catalog",
    ),
)

# OpenAI native — reasoning_effort control.
_OPENAI_NATIVE_CONTRACT = BuiltinProviderContract(
    key=ProviderContractKey(
        provider_base_url_pattern=r".*api\.openai\.com.*",
        model_id_pattern=r".*",
        protocol="openai",
    ),
    contract=ThinkingControlContract(
        mode="effort",
        request_fields=["reasoning_effort"],
        accepted_efforts=["low", "medium", "high"],
        effort_aliases={"med": "medium"},
        effort_to_budget_tokens=None,
        historical_reasoning_content="accepted",
        source="provider_catalog",
    ),
)

# All built-in contracts, in priority order (first match wins).
BUILTIN_CONTRACTS: tuple[BuiltinProviderContract, ...] = (
    _OPENCODE_GO_MINIMAX_CONTRACT,
    _MINIMAX_NATIVE_CONTRACT,
    _ANTHROPIC_NATIVE_CONTRACT,
    _OPENAI_NATIVE_CONTRACT,
)


def lookup_builtin_contract(
    *,
    provider_base_url: str,
    model_id: str,
    protocol: str,
) -> ThinkingControlContract | None:
    """Look up a built-in contract for the given provider/model/protocol.

    Returns ``None`` when no built-in contract matches.
    """
    for entry in BUILTIN_CONTRACTS:
        key = entry.key
        if key.protocol and key.protocol != protocol:
            continue
        if not re.search(key.provider_base_url_pattern, provider_base_url, re.I):
            continue
        if not re.search(key.model_id_pattern, model_id, re.I):
            continue
        logger.debug(
            "builtin_contract_match provider_url=%s model=%s protocol=%s -> mode=%s",
            provider_base_url,
            model_id,
            protocol,
            entry.contract.mode,
        )
        return entry.contract
    return None


def resolve_control_contract(
    *,
    capability: ThinkingCapability,
    provider_base_url: str = "",
    model_id: str = "",
    protocol: str = "",
) -> ThinkingControlContract:
    """Resolve the effective control contract for a provider/model.

    Precedence (highest to lowest):

    1. Explicit ``control_contract`` on the capability (operator override).
    2. Built-in contract from this module.
    3. Inferred contract from legacy capability fields.
    """
    # 1. Explicit override on the capability.
    if capability.control_contract.mode != "unknown":
        return capability.control_contract

    # 2. Built-in contract.
    if provider_base_url and model_id and protocol:
        builtin = lookup_builtin_contract(
            provider_base_url=provider_base_url,
            model_id=model_id,
            protocol=protocol,
        )
        if builtin is not None:
            return builtin

    # 3. Inferred from legacy fields.
    from eggpool.catalog.capabilities import infer_control_contract

    return infer_control_contract(capability)
