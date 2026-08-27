"""Cache-stability integration tests for the body transcoders."""

from __future__ import annotations

import json

import pytest

from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.cache_stability import (
    CACHE_BOUNDARY_ANNOTATION_CAP,
    CacheBoundaryAnnotation,
    CacheBoundaryTracker,
)
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.errors import TranscodeLossError
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic


def _context(client: str, upstream: str) -> TranscodeContext:
    return TranscodeContext(
        request_id="phase3-test",
        client_protocol=client,
        upstream_protocol=upstream,
    )


class TestOpenAIToAnthropicCacheStability:
    def setup_method(self) -> None:
        self.transcoder = OpenAIToAnthropic()
        self.context = _context("openai", "anthropic")

    def test_preserves_tool_cache_control(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "search"},
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
        result, warnings = self.transcoder.encode_request(payload, self.context)
        assert result["tools"][0]["cache_control"] == {"type": "ephemeral"}
        invalid = [
            w for w in warnings if w.get("kind") == "cache_control_invalid_shape"
        ]
        assert invalid == []
        assert any(
            annotation.kind == "preserved"
            and annotation.source_path == "tools[0].cache_control"
            for annotation in self.context.cache_boundary_tracker.annotations
        )

    def test_records_invalid_shape_annotation(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "search"},
                    "cache_control": {"oops": True},
                }
            ],
        }
        _, warnings = self.transcoder.encode_request(payload, self.context)
        kinds = [w.get("kind") for w in warnings]
        assert "cache_control_invalid_shape" in kinds
        annotations = self.context.cache_boundary_tracker.annotations
        assert any(a.kind == "dropped_invalid_shape" for a in annotations)

    def test_emits_stable_prefix_preserved_when_boundaries_match(
        self,
    ) -> None:
        # cache_control on tools[] is the only OpenAI-side placement
        # the transcoder currently preserves verbatim; this drives
        # the boundaries through encode_request and asserts the
        # structured summary warning fires.
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "search"},
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
        _, warnings = self.transcoder.encode_request(payload, self.context)
        kinds = [w.get("kind") for w in warnings]
        assert "stable_prefix_preserved" in kinds


class TestAnthropicToOpenAICacheStability:
    def setup_method(self) -> None:
        self.transcoder = AnthropicToOpenAI()
        self.context = _context("anthropic", "openai")

    def test_top_level_cache_control_emits_feature_disabled(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "cache_control": {"type": "ephemeral"},
        }
        _, warnings = self.transcoder.encode_request(payload, self.context)
        kinds = [w.get("kind") for w in warnings]
        assert "cache_control_feature_disabled" in kinds

    def test_tools_cache_control_emits_unsupported_target(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "search",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
        _, warnings = self.transcoder.encode_request(payload, self.context)
        kinds = [w.get("kind") for w in warnings]
        assert "cache_control_unsupported_by_target_protocol" in kinds
        annotations = self.context.cache_boundary_tracker.annotations
        assert any(
            a.kind == "dropped_unsupported_target"
            and a.source_protocol == "anthropic"
            and a.target_protocol == "openai"
            for a in annotations
        )

    @pytest.mark.parametrize("cache_control", ["ephemeral", {"type": "1h"}])
    def test_invalid_cache_control_records_annotation(
        self, cache_control: object
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Hi",
                            "cache_control": cache_control,
                        }
                    ],
                }
            ],
        }
        _, warnings = self.transcoder.encode_request(payload, self.context)
        assert any(w.get("kind") == "cache_control_invalid_shape" for w in warnings)
        assert any(
            a.kind == "dropped_invalid_shape"
            and a.source_protocol == "anthropic"
            and a.target_protocol == "openai"
            for a in self.context.cache_boundary_tracker.annotations
        )

    def test_message_block_cache_control_emits_unsupported_target(
        self,
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Long prompt",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        }
        _, warnings = self.transcoder.encode_request(payload, self.context)
        kinds = [w.get("kind") for w in warnings]
        assert "cache_control_unsupported_by_target_protocol" in kinds

    def test_provider_extension_on_tool_emits_warning(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "search",
                    "defer_loading": True,
                }
            ],
        }
        _, warnings = self.transcoder.encode_request(payload, self.context)
        kinds = [w.get("kind") for w in warnings]
        assert "provider_extension_not_preserved" in kinds


class TestCacheBoundaryTrackerCap:
    def test_tracker_caps_at_64(self) -> None:
        tracker = CacheBoundaryTracker()
        annotation = CacheBoundaryAnnotation(
            kind="preserved",
            source_protocol="openai",
            target_protocol="anthropic",
            source_path="x",
            target_path="x",
            cache_control_type="ephemeral",
        )
        for _ in range(CACHE_BOUNDARY_ANNOTATION_CAP + 10):
            tracker.record(annotation)
        assert len(tracker.annotations) == CACHE_BOUNDARY_ANNOTATION_CAP
        assert tracker.dropped_count == 10


class TestTranscodeContextDefault:
    def test_default_tracker_is_empty(self) -> None:
        context = TranscodeContext(
            request_id="x",
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        assert context.cache_boundary_tracker.annotations == []
        assert context.cache_boundary_tracker.dropped_count == 0


class TestLossPolicyReject:
    """Verify ``loss_policy="reject"`` enforces cache-control loss rejection.

    The plan specifies that when the operator has configured
    ``loss_policy = "reject"`` on the transcoder, any protected cache
    boundary that would be lost during translation must cause the
    request to be rejected with HTTP 400. The ``warn`` default
    preserves the v1 behaviour where the request proceeds and the
    loss is recorded in ``loss_warnings`` for audit.
    """

    def test_anthropic_to_openai_rejects_top_level_cache_control(self) -> None:
        transcoder = AnthropicToOpenAI()
        context = _context("anthropic", "openai")
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "cache_control": {"type": "ephemeral"},
        }
        with pytest.raises(TranscodeLossError) as exc_info:
            transcoder.encode_request(payload, context, loss_policy="reject")
        assert "protected boundary" in str(exc_info.value) or "cache_control" in str(
            exc_info.value
        )
        assert any(
            w.get("kind") == "cache_control_feature_disabled"
            for w in exc_info.value.loss_warnings
        )

    def test_anthropic_to_openai_rejects_tool_cache_control(self) -> None:
        transcoder = AnthropicToOpenAI()
        context = _context("anthropic", "openai")
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {"name": "search", "cache_control": {"type": "ephemeral"}},
            ],
        }
        with pytest.raises(TranscodeLossError) as exc_info:
            transcoder.encode_request(payload, context, loss_policy="reject")
        assert any(
            w.get("kind") == "cache_control_unsupported_by_target_protocol"
            for w in exc_info.value.loss_warnings
        )

    def test_anthropic_to_openai_rejects_message_block_cache_control(
        self,
    ) -> None:
        transcoder = AnthropicToOpenAI()
        context = _context("anthropic", "openai")
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Long prompt",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        }
        with pytest.raises(TranscodeLossError):
            transcoder.encode_request(payload, context, loss_policy="reject")

    def test_anthropic_to_openai_warn_mode_does_not_raise(self) -> None:
        transcoder = AnthropicToOpenAI()
        context = _context("anthropic", "openai")
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "cache_control": {"type": "ephemeral"},
        }
        out, warnings = transcoder.encode_request(payload, context, loss_policy="warn")
        assert "cache_control" not in out
        assert any(w.get("kind") == "cache_control_feature_disabled" for w in warnings)

    def test_anthropic_to_openai_clean_payload_does_not_raise(self) -> None:
        transcoder = AnthropicToOpenAI()
        context = _context("anthropic", "openai")
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        out, warnings = transcoder.encode_request(
            payload, context, loss_policy="reject"
        )
        assert "messages" in out
        # No cache-control loss warnings in a clean payload
        cache_kinds = {
            "cache_control_unsupported_by_target_protocol",
            "cache_control_feature_disabled",
            "cache_control_invalid_shape",
            "provider_extension_not_preserved",
            "stable_prefix_reordered_canonically",
        }
        assert not any(
            isinstance(w.get("kind"), str) and w["kind"] in cache_kinds
            for w in warnings
        )

    def test_openai_to_anthropic_rejects_invalid_cache_control_shape(
        self,
    ) -> None:
        transcoder = OpenAIToAnthropic()
        context = _context("openai", "anthropic")
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "search"},
                    "cache_control": {"oops": True},
                }
            ],
        }
        with pytest.raises(TranscodeLossError) as exc_info:
            transcoder.encode_request(payload, context, loss_policy="reject")
        assert any(
            w.get("kind") == "cache_control_invalid_shape"
            for w in exc_info.value.loss_warnings
        )

    def test_openai_to_anthropic_warn_mode_does_not_raise(self) -> None:
        transcoder = OpenAIToAnthropic()
        context = _context("openai", "anthropic")
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "search"},
                    "cache_control": {"oops": True},
                }
            ],
        }
        out, warnings = transcoder.encode_request(payload, context, loss_policy="warn")
        assert "tools" in out
        assert any(w.get("kind") == "cache_control_invalid_shape" for w in warnings)

    def test_openai_to_anthropic_clean_payload_does_not_raise(self) -> None:
        transcoder = OpenAIToAnthropic()
        context = _context("openai", "anthropic")
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        out, warnings = transcoder.encode_request(
            payload, context, loss_policy="reject"
        )
        assert "messages" in out
        # No cache-control loss warnings in a clean payload
        cache_kinds = {
            "cache_control_unsupported_by_target_protocol",
            "cache_control_feature_disabled",
            "cache_control_invalid_shape",
            "provider_extension_not_preserved",
            "stable_prefix_reordered_canonically",
        }
        assert not any(
            isinstance(w.get("kind"), str) and w["kind"] in cache_kinds
            for w in warnings
        )

    def test_reject_mode_pins_unknown_tool_extension_fields(self) -> None:
        """Deliberate strictness: unknown vendor tool fields are protected.

        ``provider_extension_not_preserved`` stays in
        ``CACHE_CONTROL_LOSS_KINDS`` so ``loss_policy="reject"`` fails
        loudly when a tool carries fields this translator cannot
        preserve (e.g. a future Anthropic ``defer_loading`` extension).
        Dropping such fields silently would change client-visible tool
        semantics without any operator-facing signal.
        """
        transcoder = AnthropicToOpenAI()
        context = _context("anthropic", "openai")
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "search",
                    "description": "Search",
                    "input_schema": {"type": "object"},
                    "defer_loading": True,
                },
            ],
        }
        with pytest.raises(TranscodeLossError) as exc_info:
            transcoder.encode_request(payload, context, loss_policy="reject")
        assert any(
            w.get("kind") == "provider_extension_not_preserved"
            for w in exc_info.value.loss_warnings
        )

    def test_reject_mode_pins_canonical_prefix_boundary_mismatch(self) -> None:
        """Deliberate strictness: prefix boundary mismatch is protected.

        ``stable_prefix_reordered_canonically`` stays in
        ``CACHE_CONTROL_LOSS_KINDS`` so ``loss_policy="reject"`` rejects
        requests whose cache boundaries do not survive translation
        intact instead of silently reordering/dropping them.
        """
        transcoder = OpenAIToAnthropic()
        context = _context("openai", "anthropic")
        payload: dict[str, object] = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "stable prefix",
                            "prompt_cache_breakpoint": {"mode": "explicit"},
                        },
                    ],
                },
                {"role": "user", "content": "Hi"},
            ],
        }
        with pytest.raises(TranscodeLossError) as exc_info:
            transcoder.encode_request(payload, context, loss_policy="reject")
        assert any(
            w.get("kind") == "stable_prefix_reordered_canonically"
            for w in exc_info.value.loss_warnings
        )
        assert any(
            a.kind == "reordered" for a in context.cache_boundary_tracker.annotations
        )


class TestWarningsNotInModelVisibleContent:
    """Regression guard: transcoder loss warnings must never appear in
    the translated request body. Warnings live on
    ``TranscodeContext.loss_warnings``; the body is model-visible
    content. Operators can rely on the body to be the provider-cache
    boundary without contamination from transcoder diagnostics.
    """

    def test_anthropic_to_openai_warnings_not_in_body(self) -> None:
        transcoder = AnthropicToOpenAI()
        context = _context("anthropic", "openai")
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "cache_control": {"type": "ephemeral"},
            "tools": [
                {"name": "search", "cache_control": {"type": "ephemeral"}},
            ],
        }
        out, warnings = transcoder.encode_request(payload, context)
        # Every loss warning kind must be absent from the body
        for warning in warnings:
            kind = warning.get("kind", "")
            for value in out.values():
                if isinstance(value, str):
                    assert kind not in value, (
                        f"loss warning kind {kind!r} leaked into body value"
                    )
            # Recursive check: no string value in the body contains
            # any warning kind substring
            body_json = json.dumps(out)
            assert kind not in body_json, (
                f"loss warning kind {kind!r} found in serialised body"
            )

    def test_openai_to_anthropic_warnings_not_in_body(self) -> None:
        transcoder = OpenAIToAnthropic()
        context = _context("openai", "anthropic")
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "search"},
                    "cache_control": {"oops": True},
                }
            ],
        }
        out, warnings = transcoder.encode_request(payload, context)
        body_json = json.dumps(out)
        for warning in warnings:
            kind = warning.get("kind", "")
            assert kind not in body_json, (
                f"loss warning kind {kind!r} found in serialised body"
            )

    def test_anthropic_to_openai_provider_extension_not_in_body(self) -> None:
        transcoder = AnthropicToOpenAI()
        context = _context("anthropic", "openai")
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "search",
                    "defer_loading": True,
                }
            ],
        }
        out, warnings = transcoder.encode_request(payload, context)
        assert any(
            w.get("kind") == "provider_extension_not_preserved" for w in warnings
        )
        # The provider extension must be stripped from the body
        body_json = json.dumps(out)
        assert "defer_loading" not in body_json
