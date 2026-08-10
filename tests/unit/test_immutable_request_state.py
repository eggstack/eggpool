"""Tests for ImmutableRequestState (Milestone F9).

Verifies:

- Frozen dataclass cannot be mutated after creation.
- All fields are frozensets.
- Construction from registry data is correct.
- Generation swap invalidates naturally.
"""

from __future__ import annotations

from eggpool.proxy.client import HOP_BY_HOP_HEADERS, LOCAL_CREDENTIAL_HEADERS
from eggpool.runtime_manager import ImmutableRequestState


class TestImmutableRequestState:
    def test_frozen_dataclass(self) -> None:
        state = ImmutableRequestState(
            provider_ids=frozenset({"p1", "p2"}),
            account_names=frozenset({"a1"}),
            hop_by_hop_headers=HOP_BY_HOP_HEADERS,
            local_credential_headers=LOCAL_CREDENTIAL_HEADERS,
        )
        try:
            state.provider_ids = frozenset({"new"})  # type: ignore[misc]
        except AttributeError:
            pass
        else:
            raise AssertionError("frozen dataclass should prevent mutation")

    def test_all_fields_are_frozensets(self) -> None:
        state = ImmutableRequestState(
            provider_ids=frozenset({"p1"}),
            account_names=frozenset({"a1"}),
            hop_by_hop_headers=HOP_BY_HOP_HEADERS,
            local_credential_headers=LOCAL_CREDENTIAL_HEADERS,
        )
        assert isinstance(state.provider_ids, frozenset)
        assert isinstance(state.account_names, frozenset)
        assert isinstance(state.hop_by_hop_headers, frozenset)
        assert isinstance(state.local_credential_headers, frozenset)
        assert isinstance(state.trusted_proxies, frozenset)

    def test_empty_state(self) -> None:
        state = ImmutableRequestState(
            provider_ids=frozenset(),
            account_names=frozenset(),
            hop_by_hop_headers=frozenset(),
            local_credential_headers=frozenset(),
        )
        assert len(state.provider_ids) == 0
        assert len(state.account_names) == 0
        assert state.trusted_proxies == frozenset()

    def test_trusted_proxies_are_generation_owned(self) -> None:
        state = ImmutableRequestState(
            provider_ids=frozenset(),
            account_names=frozenset(),
            hop_by_hop_headers=frozenset(),
            local_credential_headers=frozenset(),
            trusted_proxies=frozenset({"127.0.0.1", "::1"}),
        )
        assert state.trusted_proxies == frozenset({"127.0.0.1", "::1"})

    def test_hop_by_hop_headers_match_module_constants(self) -> None:
        state = ImmutableRequestState(
            provider_ids=frozenset(),
            account_names=frozenset(),
            hop_by_hop_headers=HOP_BY_HOP_HEADERS,
            local_credential_headers=LOCAL_CREDENTIAL_HEADERS,
        )
        assert state.hop_by_hop_headers == HOP_BY_HOP_HEADERS
        assert "connection" in state.hop_by_hop_headers
        assert "transfer-encoding" in state.hop_by_hop_headers
        assert "upgrade" in state.hop_by_hop_headers

    def test_credential_headers_match_module_constants(self) -> None:
        state = ImmutableRequestState(
            provider_ids=frozenset(),
            account_names=frozenset(),
            hop_by_hop_headers=frozenset(),
            local_credential_headers=LOCAL_CREDENTIAL_HEADERS,
        )
        assert state.local_credential_headers == LOCAL_CREDENTIAL_HEADERS
        assert "authorization" in state.local_credential_headers
        assert "x-api-key" in state.local_credential_headers

    def test_provider_ids_populated(self) -> None:
        state = ImmutableRequestState(
            provider_ids=frozenset({"openai", "anthropic", "google"}),
            account_names=frozenset(),
            hop_by_hop_headers=frozenset(),
            local_credential_headers=frozenset(),
        )
        assert state.provider_ids == frozenset({"openai", "anthropic", "google"})

    def test_account_names_populated(self) -> None:
        state = ImmutableRequestState(
            provider_ids=frozenset(),
            account_names=frozenset({"default", "team-a"}),
            hop_by_hop_headers=frozenset(),
            local_credential_headers=frozenset(),
        )
        assert state.account_names == frozenset({"default", "team-a"})

    def test_new_generation_gets_fresh_state(self) -> None:
        state_a = ImmutableRequestState(
            provider_ids=frozenset({"p1"}),
            account_names=frozenset({"a1"}),
            hop_by_hop_headers=frozenset(),
            local_credential_headers=frozenset(),
        )
        state_b = ImmutableRequestState(
            provider_ids=frozenset({"p1", "p2"}),
            account_names=frozenset({"a1", "a2"}),
            hop_by_hop_headers=frozenset(),
            local_credential_headers=frozenset(),
        )
        # State A is unchanged by State B creation
        assert state_a.provider_ids == frozenset({"p1"})
        assert state_b.provider_ids == frozenset({"p1", "p2"})

    def test_default_factory_on_generation(self) -> None:
        from dataclasses import fields

        from eggpool.runtime_manager import RuntimeGeneration

        # The default_factory on RuntimeGeneration.immutable_request_state
        # should produce an empty ImmutableRequestState
        field_info = {f.name: f for f in fields(RuntimeGeneration)}
        irs_field = field_info["immutable_request_state"]
        assert irs_field.default_factory is not None
        default_state = irs_field.default_factory()
        assert isinstance(default_state, ImmutableRequestState)
        assert len(default_state.provider_ids) == 0
        assert len(default_state.account_names) == 0
