"""Tests for MultimodalCapabilities and MediaCapability types."""

from __future__ import annotations

import contextlib

from eggpool.catalog.capabilities import (
    MediaCapability,
    ModelCapabilities,
    MultimodalCapabilities,
    dict_to_model_capabilities,
    merge_model_capabilities,
    model_capabilities_to_dict,
)


class TestMediaCapability:
    def test_defaults(self) -> None:
        mc = MediaCapability()
        assert mc.base64 is False
        assert mc.url is False
        assert mc.max_source_bytes is None

    def test_frozen(self) -> None:
        mc = MediaCapability(base64=True)
        with contextlib.suppress(AttributeError):
            mc.base64 = False  # type: ignore[misc]
        assert mc.base64 is True

    def test_with_limits(self) -> None:
        mc = MediaCapability(
            base64=True,
            url=True,
            max_source_bytes=10_000_000,
        )
        assert mc.base64 is True
        assert mc.url is True
        assert mc.max_source_bytes == 10_000_000


class TestMultimodalCapabilities:
    def test_defaults(self) -> None:
        mm = MultimodalCapabilities()
        assert mm.image_input == MediaCapability()
        assert mm.document_input == MediaCapability()
        assert mm.audio_input == MediaCapability()
        assert mm.non_text_tool_result is False
        assert mm.max_serialized_request_bytes is None

    def test_frozen(self) -> None:
        mm = MultimodalCapabilities(non_text_tool_result=True)
        with contextlib.suppress(AttributeError):
            mm.non_text_tool_result = False  # type: ignore[misc]
        assert mm.non_text_tool_result is True

    def test_custom_construction(self) -> None:
        mm = MultimodalCapabilities(
            image_input=MediaCapability(base64=True, url=True),
            document_input=MediaCapability(base64=True),
            audio_input=MediaCapability(),
            non_text_tool_result=True,
            max_serialized_request_bytes=5_000_000,
        )
        assert mm.image_input.base64 is True
        assert mm.image_input.url is True
        assert mm.document_input.base64 is True
        assert mm.document_input.url is False
        assert mm.non_text_tool_result is True
        assert mm.max_serialized_request_bytes == 5_000_000

    def test_equality(self) -> None:
        a = MultimodalCapabilities(
            image_input=MediaCapability(base64=True),
        )
        b = MultimodalCapabilities(
            image_input=MediaCapability(base64=True),
        )
        assert a == b

    def test_inequality(self) -> None:
        a = MultimodalCapabilities(
            image_input=MediaCapability(base64=True),
        )
        b = MultimodalCapabilities(
            image_input=MediaCapability(url=True),
        )
        assert a != b


class TestModelCapabilitiesMultimodal:
    def test_default_has_empty_multimodal(self) -> None:
        mc = ModelCapabilities()
        assert mc.multimodal == MultimodalCapabilities()

    def test_with_multimodal(self) -> None:
        mm = MultimodalCapabilities(
            image_input=MediaCapability(base64=True, url=True),
            document_input=MediaCapability(base64=True),
        )
        mc = ModelCapabilities(multimodal=mm)
        assert mc.multimodal.image_input.base64 is True
        assert mc.multimodal.document_input.base64 is True


class TestMultimodalSerialization:
    def test_roundtrip_empty(self) -> None:
        mc = ModelCapabilities()
        d = model_capabilities_to_dict(mc)
        restored = dict_to_model_capabilities(d)
        assert restored.multimodal == MultimodalCapabilities()

    def test_roundtrip_with_image(self) -> None:
        mm = MultimodalCapabilities(
            image_input=MediaCapability(
                base64=True, url=True, max_source_bytes=5_000_000
            ),
        )
        mc = ModelCapabilities(multimodal=mm)
        d = model_capabilities_to_dict(mc)
        assert "multimodal" in d
        mm_dict = d["multimodal"]  # type: ignore[typeddict-item]
        assert isinstance(mm_dict, dict)
        assert mm_dict["image_input"]["base64"] is True
        assert mm_dict["image_input"]["url"] is True
        assert mm_dict["image_input"]["max_source_bytes"] == 5_000_000

        restored = dict_to_model_capabilities(d)
        assert restored.multimodal.image_input.base64 is True
        assert restored.multimodal.image_input.url is True
        assert restored.multimodal.image_input.max_source_bytes == 5_000_000

    def test_roundtrip_with_all_modalities(self) -> None:
        mm = MultimodalCapabilities(
            image_input=MediaCapability(base64=True, url=True),
            document_input=MediaCapability(base64=True),
            audio_input=MediaCapability(url=True),
            non_text_tool_result=True,
            max_serialized_request_bytes=10_000_000,
        )
        mc = ModelCapabilities(multimodal=mm)
        d = model_capabilities_to_dict(mc)
        restored = dict_to_model_capabilities(d)

        assert restored.multimodal.image_input.base64 is True
        assert restored.multimodal.image_input.url is True
        assert restored.multimodal.document_input.base64 is True
        assert restored.multimodal.document_input.url is False
        assert restored.multimodal.audio_input.url is True
        assert restored.multimodal.audio_input.base64 is False
        assert restored.multimodal.non_text_tool_result is True
        assert restored.multimodal.max_serialized_request_bytes == 10_000_000

    def test_dict_with_no_multimodal(self) -> None:
        d: dict[str, object] = {"thinking": {"status": "supported"}}
        restored = dict_to_model_capabilities(d)
        assert restored.multimodal == MultimodalCapabilities()

    def test_dict_with_empty_multimodal(self) -> None:
        d: dict[str, object] = {"multimodal": {}}
        restored = dict_to_model_capabilities(d)
        assert restored.multimodal == MultimodalCapabilities()

    def test_dict_with_invalid_multimodal(self) -> None:
        d: dict[str, object] = {"multimodal": "not a dict"}
        restored = dict_to_model_capabilities(d)
        assert restored.multimodal == MultimodalCapabilities()

    def test_dict_with_partial_image(self) -> None:
        d: dict[str, object] = {
            "multimodal": {
                "image_input": {"base64": True},
            }
        }
        restored = dict_to_model_capabilities(d)
        assert restored.multimodal.image_input.base64 is True
        assert restored.multimodal.image_input.url is False
        assert restored.multimodal.document_input == MediaCapability()

    def test_non_text_tool_result_roundtrip(self) -> None:
        mm = MultimodalCapabilities(non_text_tool_result=True)
        mc = ModelCapabilities(multimodal=mm)
        d = model_capabilities_to_dict(mc)
        restored = dict_to_model_capabilities(d)
        assert restored.multimodal.non_text_tool_result is True

    def test_max_serialized_request_bytes_roundtrip(self) -> None:
        mm = MultimodalCapabilities(max_serialized_request_bytes=20_000_000)
        mc = ModelCapabilities(multimodal=mm)
        d = model_capabilities_to_dict(mc)
        restored = dict_to_model_capabilities(d)
        assert restored.multimodal.max_serialized_request_bytes == 20_000_000

    def test_omits_default_multimodal_from_dict(self) -> None:
        mc = ModelCapabilities()
        d = model_capabilities_to_dict(mc)
        assert "multimodal" not in d


class TestMultimodalMerge:
    def test_merge_default_over_default(self) -> None:
        base = ModelCapabilities()
        override = ModelCapabilities()
        merged = merge_model_capabilities(base, override)
        assert merged.multimodal == MultimodalCapabilities()

    def test_merge_with_multimodal_override(self) -> None:
        base = ModelCapabilities()
        override = ModelCapabilities(
            multimodal=MultimodalCapabilities(
                image_input=MediaCapability(base64=True),
            ),
        )
        merged = merge_model_capabilities(base, override)
        assert merged.multimodal.image_input.base64 is True

    def test_merge_preserves_base_multimodal(self) -> None:
        base = ModelCapabilities(
            multimodal=MultimodalCapabilities(
                document_input=MediaCapability(base64=True),
            ),
        )
        override = ModelCapabilities()
        merged = merge_model_capabilities(base, override)
        assert merged.multimodal.document_input.base64 is True
        assert merged.multimodal.image_input == MediaCapability()

    def test_merge_override_replaces_base_multimodal(self) -> None:
        base = ModelCapabilities(
            multimodal=MultimodalCapabilities(
                image_input=MediaCapability(base64=True),
            ),
        )
        override = ModelCapabilities(
            multimodal=MultimodalCapabilities(
                image_input=MediaCapability(url=True),
            ),
        )
        merged = merge_model_capabilities(base, override)
        assert merged.multimodal.image_input.url is True
        assert merged.multimodal.image_input.base64 is False
