"""Provider contract keying and precedence (unit).

Tests the extended ProviderContractKey matching, deterministic precedence,
and operator override behavior for built-in contracts.
"""

from __future__ import annotations

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    ThinkingControlContract,
)
from eggpool.transcoder.builtin_contracts import (
    lookup_builtin_contract,
    resolve_control_contract,
)

# ---------------------------------------------------------------------------
# 1. OpenCode Go has no bundled model reasoning contract
# ---------------------------------------------------------------------------


class TestOpenCodeGoProviderIdentity:
    """OpenCode Go reasoning controls come from provider/model metadata."""

    def test_canonical_provider_id_does_not_resolve_effort(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract.effort == "unknown"
        assert contract.budget == "unknown"
        assert contract.accepted_efforts == []

    def test_default_opencode_go_url_configures_correctly(self) -> None:
        """The OpenCode Go URL does not provide a capability fallback."""
        contract = lookup_builtin_contract(
            provider_id="opencode-go",
            provider_base_url="https://opencode.ai/zen/go/v1",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract is None

    def test_opencode_go_resolves_without_url(self) -> None:
        """Provider ID alone is identity, not capability evidence."""
        contract = lookup_builtin_contract(
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract is None


# ---------------------------------------------------------------------------
# 2. OpenCode Go URL has no compatibility fallback
# ---------------------------------------------------------------------------


class TestOpenCodeGoUrlFallback:
    """URL identity must not synthesize model controls."""

    def test_url_fallback_does_not_resolve_effort(self) -> None:
        """URL pattern alone does not resolve the OpenCode Go contract."""
        contract = lookup_builtin_contract(
            provider_base_url="https://opencode.ai/zen/go/v1",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract is None

    def test_minimax_url_fallback_does_not_capture_native(self) -> None:
        """minimax.io URL fallback does NOT match OpenCode Go or native MiniMax."""
        contract = lookup_builtin_contract(
            provider_base_url="https://api.minimax.io/anthropic/v1",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        # No OpenCode Go or native MiniMax URL rule matches minimax.io.
        assert contract is None


# ---------------------------------------------------------------------------
# 3. Native MiniMax canonical provider ID
# ---------------------------------------------------------------------------


class TestNativeMiniMaxProviderIdentity:
    """Native MiniMax resolves its own contract by provider ID."""

    def test_canonical_provider_id_resolves_effort(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract.effort == "supported"
        assert contract.budget == "unknown"
        assert "low" in contract.accepted_efforts
        assert "medium" in contract.accepted_efforts
        assert "high" in contract.accepted_efforts

    def test_native_minimax_not_shadowed_by_opencode_go(self) -> None:
        """MiniMax native contract is distinct from OpenCode Go."""
        cap_opencode = ThinkingCapability(status="supported")
        contract_opencode = resolve_control_contract(
            capability=cap_opencode,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        cap_minimax = ThinkingCapability(status="supported")
        contract_minimax = resolve_control_contract(
            capability=cap_minimax,
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        # OpenCode Go remains unknown without provider metadata; native
        # MiniMax keeps its independently scoped provider contract.
        assert contract_opencode.mode == "unknown"
        assert contract_opencode.accepted_efforts == []
        assert contract_minimax.mode == "effort"
        assert contract_opencode is not contract_minimax


# ---------------------------------------------------------------------------
# 4. Unknown provider returns inferred behavior
# ---------------------------------------------------------------------------


class TestUnknownProviderFallback:
    """Unknown providers fall through to inferred contracts."""

    def test_unknown_provider_id_no_builtin_match(self) -> None:
        contract = lookup_builtin_contract(
            provider_id="unknown-provider",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract is None

    def test_unknown_provider_infers_from_legacy_fields(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            supported_efforts=["low", "medium", "high"],
        )
        contract = resolve_control_contract(
            capability=cap,
            provider_id="unknown-provider",
            model_id="some-model",
            protocol="openai",
        )
        assert contract.mode == "effort"
        assert contract.accepted_efforts == ["low", "medium", "high"]

    def test_unknown_provider_unknown_model_infers_unknown(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="unknown-provider",
            model_id="unknown-model",
            protocol="openai",
        )
        assert contract.mode == "unknown"


# ---------------------------------------------------------------------------
# 5. Explicit operator override wins over built-in
# ---------------------------------------------------------------------------


class TestOperatorOverridePrecedence:
    """Explicit control_contract on capability takes highest precedence."""

    def test_opencode_go_explicit_override_resolves_effort(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            control_contract=ThinkingControlContract(
                mode="effort",
                accepted_efforts=["low", "medium", "high"],
                source="manual_override",
            ),
        )
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        # Explicit provider/model metadata wins over the unknown default.
        assert contract.mode == "effort"

    def test_override_wins_over_minimax_native_builtin(self) -> None:
        cap = ThinkingCapability(
            status="supported",
            control_contract=ThinkingControlContract(
                mode="fixed",
                source="manual_override",
            ),
        )
        contract = resolve_control_contract(
            capability=cap,
            provider_id="minimax",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        # Override says fixed, but built-in says effort.
        assert contract.mode == "fixed"

    def test_override_does_not_mutate_global_registry(self) -> None:
        """An override on one capability does not affect other lookups."""
        cap_override = ThinkingCapability(
            status="supported",
            control_contract=ThinkingControlContract(
                mode="budget",
                source="manual_override",
            ),
        )
        contract_override = resolve_control_contract(
            capability=cap_override,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract_override.mode == "budget"

        # A fresh capability without metadata remains unknown.
        cap_fresh = ThinkingCapability(status="supported")
        contract_fresh = resolve_control_contract(
            capability=cap_fresh,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract_fresh.mode == "unknown"


# ---------------------------------------------------------------------------
# 6. Collapsed and provider-qualified model IDs
# ---------------------------------------------------------------------------


class TestCollapsedModelResolution:
    """Collapsed and suffixed model IDs resolve consistently."""

    def test_collapsed_minimax_m3(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        assert contract.mode == "unknown"

    def test_suffixed_minimax_m3(self) -> None:
        cap = ThinkingCapability(status="supported")
        contract = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3/opencode-go",
            protocol="anthropic",
        )
        assert contract.mode == "unknown"

    def test_collapsed_minimax_m3_matches_native_lowercase(self) -> None:
        """Model spelling does not affect the unknown metadata fallback."""
        cap = ThinkingCapability(status="supported")
        canonical = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        lowercase = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="minimax-m3",
            protocol="anthropic",
        )
        assert canonical.mode == lowercase.mode == "unknown"
        assert canonical.accepted_efforts == lowercase.accepted_efforts

    def test_both_resolve_same_contract(self) -> None:
        cap = ThinkingCapability(status="supported")
        c1 = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="anthropic",
        )
        c2 = resolve_control_contract(
            capability=cap,
            provider_id="opencode-go",
            model_id="MiniMax-M3/opencode-go",
            protocol="anthropic",
        )
        assert c1.mode == c2.mode == "unknown"


# ---------------------------------------------------------------------------
# 7. Non-MiniMax model on OpenCode Go does not inherit MiniMax contract
# ---------------------------------------------------------------------------


class TestNonMiniMaxModelExclusion:
    """Non-MiniMax models are not affected by the MiniMax-M3 contract."""

    def test_claude_on_opencode_go_no_minimax_contract(self) -> None:
        contract = lookup_builtin_contract(
            provider_id="opencode-go",
            model_id="claude-3-opus",
            protocol="anthropic",
        )
        # The MiniMax-M3 model pattern should not match claude-3-opus.
        assert contract is None

    def test_gpt_on_opencode_go_no_minimax_contract(self) -> None:
        contract = lookup_builtin_contract(
            provider_id="opencode-go",
            model_id="gpt-4o",
            protocol="anthropic",
        )
        assert contract is None


# ---------------------------------------------------------------------------
# 8. Wrong protocol does not match
# ---------------------------------------------------------------------------


class TestProtocolMismatch:
    """Protocol mismatch prevents contract matching."""

    def test_anthropic_only_rule_not_matched_by_openai(self) -> None:
        contract = lookup_builtin_contract(
            provider_id="opencode-go",
            model_id="MiniMax-M3",
            protocol="openai",
        )
        # OpenCode Go rule requires protocol="anthropic".
        assert contract is None

    def test_openai_only_rule_not_matched_by_anthropic(self) -> None:
        contract = lookup_builtin_contract(
            provider_base_url="https://api.openai.com/v1",
            model_id="gpt-4o",
            protocol="anthropic",
        )
        # OpenAI rule requires protocol="openai".
        assert contract is None
