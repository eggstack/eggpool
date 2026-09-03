"""Built-in provider-bound thinking control contracts.

This module contains manually curated contracts for known provider
deployments where the capability metadata alone is insufficient to
determine the accepted thinking controls.  These contracts serve as
the authoritative fallback when catalog/model-info data is missing
or ambiguous.

Contract precedence (highest to lowest):

1. Operator overrides (``[model_capabilities."<model>".thinking.control_contract]``).
2. Built-in contracts from this module (matched by provider identity
   first, then URL pattern fallback).
3. Inferred contracts from capability metadata.

Built-in matching precedence:

1. Exact ``provider_id`` match (highest specificity).
2. ``provider_kind`` match (provider family).
3. ``provider_base_url`` pattern match (lowest specificity, compatibility).
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

    Contracts are scoped by provider identity (ID or kind), base URL
    pattern, model identity, and protocol endpoint to avoid false
    positives.  When ``provider_id_pattern`` is set and the selected
    provider ID matches, it takes precedence over ``provider_kind``
    and URL-pattern matching.
    """

    provider_id_pattern: str | None = None
    provider_kind_pattern: str | None = None
    provider_base_url_pattern: str | None = None
    model_id_pattern: str = ".*"
    protocol: str = ""
    priority: int = 0


@dataclass(frozen=True, slots=True)
class BuiltinProviderContract:
    """A manually curated provider-bound thinking control contract."""

    key: ProviderContractKey
    contract: ThinkingControlContract


# ---------------------------------------------------------------------------
# OpenCode Go Muse Spark 1.2 Contributor contract
# ---------------------------------------------------------------------------
# The OpenCode Go model list does not include capability metadata, so this
# model needs a curated contract for its Responses/OpenAI-family path. The
# effort vocabulary is mirrored from the current models.dev/OpenCode model
# record, including the newer ``minimal`` and ``xhigh`` levels.
_OPENCODE_GO_MUSE_CONTRACT = BuiltinProviderContract(
    key=ProviderContractKey(
        provider_id_pattern=r"^opencode-go$",
        model_id_pattern=r"^muse-spark-1\.2-contributor$",
        protocol="openai",
        priority=10,
    ),
    contract=ThinkingControlContract(
        mode="effort_or_budget",
        request_fields=["thinking", "reasoning_effort"],
        accepted_efforts=["minimal", "low", "medium", "high", "xhigh"],
        effort_to_budget_tokens={
            "minimal": 1024,
            "low": 1024,
            "medium": 4096,
            "high": 16384,
            "xhigh": 24576,
        },
        explicit_budget_min=1024,
        explicit_budget_max=131072,
        historical_reasoning_content="accepted",
        source="provider_catalog",
    ),
)

_OPENCODE_GO_MUSE_URL_COMPAT_CONTRACT = BuiltinProviderContract(
    key=ProviderContractKey(
        provider_base_url_pattern=r".*opencode\.ai/zen/go/v1.*",
        model_id_pattern=r"^muse-spark-1\.2-contributor$",
        protocol="openai",
        priority=10,
    ),
    contract=_OPENCODE_GO_MUSE_CONTRACT.contract,
)

# ---------------------------------------------------------------------------
# OpenCode Go MiniMax-M3 contract
# ---------------------------------------------------------------------------
# OpenCode Go routes MiniMax-M3 through its own proxy.  The canonical
# provider ID is ``opencode-go`` and the default upstream URL is
# ``https://opencode.ai/zen/go/v1``.  Empirically the upstream accepts
# both ``thinking`` (Anthropic Messages) and ``reasoning_effort``
# (OpenAI Chat Completions) for low/medium/high, matching the native
# MiniMax contract.  The OpenAI-to-Messages adapter represents the
# selected effort as Anthropic ``thinking.budget_tokens``, so this
# deployment accepts either equivalent control form.
_OPENCODE_GO_MINIMAX_CONTRACT = BuiltinProviderContract(
    key=ProviderContractKey(
        provider_id_pattern=r"^opencode-go$",
        model_id_pattern=r".*minimax.*m3.*|.*m3.*minimax.*",
        protocol="anthropic",
        priority=10,
    ),
    contract=ThinkingControlContract(
        mode="effort_or_budget",
        request_fields=["thinking", "reasoning_effort"],
        accepted_efforts=["low", "medium", "high"],
        effort_aliases={"med": "medium"},
        effort_to_budget_tokens={"low": 1024, "medium": 4096, "high": 16384},
        historical_reasoning_content="accepted",
        source="manual_override",
    ),
)

# MiniMax's own native Anthropic endpoint — accepts effort controls.
_MINIMAX_NATIVE_CONTRACT = BuiltinProviderContract(
    key=ProviderContractKey(
        provider_id_pattern=r"^minimax$",
        model_id_pattern=r".*minimax.*m3.*|.*m3.*minimax.*",
        protocol="anthropic",
        priority=10,
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

# OpenCode Go URL compatibility — same effort-or-budget contract as the ID-based
# rule, for providers configured with an OpenCode Go upstream URL but a
# non-canonical provider ID.
_OPENCODE_GO_URL_COMPAT_CONTRACT = BuiltinProviderContract(
    key=ProviderContractKey(
        provider_base_url_pattern=r".*opencode\.ai.*",
        model_id_pattern=r".*minimax.*m3.*|.*m3.*minimax.*",
        protocol="anthropic",
        priority=10,
    ),
    contract=ThinkingControlContract(
        mode="effort_or_budget",
        request_fields=["thinking", "reasoning_effort"],
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
        priority=20,
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
        priority=20,
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

# All built-in contracts, in declaration order (used for iteration).
BUILTIN_CONTRACTS: tuple[BuiltinProviderContract, ...] = (
    _OPENCODE_GO_MUSE_CONTRACT,
    _OPENCODE_GO_MUSE_URL_COMPAT_CONTRACT,
    _OPENCODE_GO_MINIMAX_CONTRACT,
    _MINIMAX_NATIVE_CONTRACT,
    _OPENCODE_GO_URL_COMPAT_CONTRACT,
    _ANTHROPIC_NATIVE_CONTRACT,
    _OPENAI_NATIVE_CONTRACT,
)


def _match_key(
    key: ProviderContractKey,
    *,
    provider_id: str,
    provider_kind: str | None,
    provider_base_url: str,
    model_id: str,
    protocol: str,
) -> tuple[bool, int]:
    """Check if a key matches the given context.

    Returns ``(matched, specificity)`` where specificity is:

    - 3 for exact ``provider_id`` match
    - 2 for ``provider_kind`` match
    - 1 for ``provider_base_url`` pattern match
    - 0 for no match
    """
    if key.protocol and key.protocol != protocol:
        return False, 0

    if not re.search(key.model_id_pattern, model_id, re.I):
        return False, 0

    # Provider ID match (highest specificity).
    if (
        key.provider_id_pattern
        and provider_id
        and re.search(key.provider_id_pattern, provider_id, re.I)
    ):
        return True, 3

    # Provider kind match.
    if (
        key.provider_kind_pattern
        and provider_kind
        and re.search(key.provider_kind_pattern, provider_kind, re.I)
    ):
        return True, 2

    # Base URL pattern match (lowest specificity, compatibility).
    if (
        key.provider_base_url_pattern
        and provider_base_url
        and re.search(key.provider_base_url_pattern, provider_base_url, re.I)
    ):
        return True, 1

    return False, 0


def lookup_builtin_contract(
    *,
    provider_id: str = "",
    provider_kind: str | None = None,
    provider_base_url: str = "",
    model_id: str,
    protocol: str,
) -> ThinkingControlContract | None:
    """Look up a built-in contract for the given provider/model/protocol.

    Matching precedence (highest specificity wins):

    1. Exact ``provider_id`` match.
    2. ``provider_kind`` match.
    3. ``provider_base_url`` pattern match.

    Within the same specificity level, the entry with the lowest
    ``priority`` value wins.  Returns ``None`` when no built-in
    contract matches.

    Raises ``ValueError`` when two built-in rules match at the same
    specificity and priority (ambiguous).
    """
    matches: list[tuple[BuiltinProviderContract, int]] = []

    for entry in BUILTIN_CONTRACTS:
        matched, specificity = _match_key(
            entry.key,
            provider_id=provider_id,
            provider_kind=provider_kind,
            provider_base_url=provider_base_url,
            model_id=model_id,
            protocol=protocol,
        )
        if matched:
            matches.append((entry, specificity))

    if not matches:
        return None

    # Select by highest specificity first, then lowest priority within
    # that specificity level.  This ensures a more-specific rule (e.g.
    # provider_id match) always wins over a less-specific rule (e.g.
    # URL match) even when the less-specific rule has a numerically
    # lower priority.
    best_specificity = max(s for _, s in matches)
    best_priority = min(e.key.priority for e, s in matches if s == best_specificity)
    best = [
        e
        for e, s in matches
        if s == best_specificity and e.key.priority == best_priority
    ]

    if len(best) > 1:
        patterns = [
            f"provider_id={e.key.provider_id_pattern!r} "
            f"kind={e.key.provider_kind_pattern!r} "
            f"url={e.key.provider_base_url_pattern!r}"
            for e in best
        ]
        raise ValueError(
            f"Ambiguous built-in contracts at priority={best_priority} "
            f"specificity={best_specificity}: {'; '.join(patterns)}"
        )

    entry = best[0]
    logger.debug(
        "builtin_contract_match "
        "provider_id=%s kind=%s url=%s model=%s protocol=%s "
        "specificity=%d priority=%d -> mode=%s",
        provider_id,
        provider_kind,
        provider_base_url,
        model_id,
        protocol,
        best_specificity,
        entry.key.priority,
        entry.contract.mode,
    )
    return entry.contract


def validate_no_ambiguous_contracts() -> list[str]:
    """Detect ambiguous built-in rules that would fail at runtime.

    Returns a list of human-readable error messages for any pair of
    rules that could match the same (provider_id, provider_kind,
    provider_base_url, model_id, protocol) context with equal
    specificity and priority.  An empty list means no ambiguities.
    """
    errors: list[str] = []

    for i, a in enumerate(BUILTIN_CONTRACTS):
        for b in BUILTIN_CONTRACTS[i + 1 :]:
            # Two rules are ambiguous if they have the same priority
            # AND their key patterns could overlap at the same
            # specificity level.  We check by testing a synthetic
            # context that matches both.
            if a.key.priority != b.key.priority:
                continue

            # Rules at different specificity classes cannot be
            # ambiguous — the more-specific rule always wins.
            a_spec = _specificity_class(a.key)
            b_spec = _specificity_class(b.key)
            if a_spec != b_spec:
                continue

            # Check if the more-specific patterns are subsets.
            a_has_id = a.key.provider_id_pattern is not None
            b_has_id = b.key.provider_id_pattern is not None
            a_has_kind = a.key.provider_kind_pattern is not None
            b_has_kind = b.key.provider_kind_pattern is not None

            # Both have provider_id — different patterns can't be
            # ambiguous (each matches a different ID).
            if a_has_id and b_has_id:
                continue

            # Same kind or both have kind — potential overlap.
            if (
                a_has_kind
                and b_has_kind
                and a.key.provider_kind_pattern == b.key.provider_kind_pattern
            ):
                errors.append(
                    f"Ambiguous: same priority={a.key.priority} and "
                    f"identical kind pattern={a.key.provider_kind_pattern!r}: "
                    f"{a.key} vs {b.key}"
                )
                continue

            # Check URL+model+protocol overlap.
            protocol_match = (
                a.key.protocol == b.key.protocol
                or not a.key.protocol
                or not b.key.protocol
            )
            url_match = (
                a.key.provider_base_url_pattern == b.key.provider_base_url_pattern
                or not a.key.provider_base_url_pattern
                or not b.key.provider_base_url_pattern
            )
            model_match = a.key.model_id_pattern == b.key.model_id_pattern
            if protocol_match and url_match and model_match:
                errors.append(
                    f"Ambiguous: same priority={a.key.priority}, "
                    f"protocol={a.key.protocol!r}, "
                    f"url={a.key.provider_base_url_pattern!r}, "
                    f"model={a.key.model_id_pattern!r}: "
                    f"{a.key} vs {b.key}"
                )

    return errors


def _specificity_class(key: ProviderContractKey) -> int:
    """Return the specificity class for ambiguity checking.

    Rules at different specificity classes cannot be ambiguous because
    the more-specific rule always wins at runtime.
    """
    if key.provider_id_pattern:
        return 3
    if key.provider_kind_pattern:
        return 2
    if key.provider_base_url_pattern:
        return 1
    return 0


def resolve_control_contract(
    *,
    capability: ThinkingCapability,
    provider_id: str = "",
    provider_kind: str | None = None,
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
    builtin = lookup_builtin_contract(
        provider_id=provider_id,
        provider_kind=provider_kind,
        provider_base_url=provider_base_url,
        model_id=model_id,
        protocol=protocol,
    )
    if builtin is not None:
        return builtin

    # 3. Inferred from legacy fields.
    from eggpool.catalog.capabilities import infer_control_contract

    return infer_control_contract(capability)
