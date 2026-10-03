"""Opt-in pydantic-core JSON output, independent of serializer selection."""

from pydantic_core import to_json
from rest_framework.renderers import JSONRenderer

from fastdrf.renderers import _DATA_RENDERERS, _without_indent

__all__ = ["PydanticJSONRenderer"]

# DRF's encoder is stateless. Its default handles lazy translations, QuerySets,
# numpy-like objects and mappings not handled natively by pydantic-core.
_encoder_class = JSONRenderer.encoder_class
_default = _encoder_class().default


class PydanticJSONRenderer(JSONRenderer):
    """
    Render Python data using pydantic-core's JSON representation.

    Decimals are strings, bytes are URL-safe base64, timedeltas are ISO 8601
    durations, and non-finite floats are null. Indentation, custom encoders,
    ASCII-only and noncompact output use DRF before encoding starts. Encoding
    failures are never retried on an already consumed iterator.
    """

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if data is None:
            return b""
        if (
            self.ensure_ascii
            or not self.compact
            or self.encoder_class is not _encoder_class
            or (
                not _without_indent(
                    self, PydanticJSONRenderer, accepted_media_type, renderer_context
                )
                and self.get_indent(accepted_media_type, renderer_context or {})
                is not None
            )
        ):
            return super().render(data, accepted_media_type, renderer_context)
        output = to_json(
            data,
            bytes_mode="base64",
            inf_nan_mode="null",
            fallback=_default,
        )
        # DRF escapes these separators for JSON embedded in JavaScript.
        if b"\xe2\x80" in output:
            output = output.replace(b"\xe2\x80\xa8", b"\\u2028").replace(
                b"\xe2\x80\xa9", b"\\u2029"
            )
        return output


_DATA_RENDERERS[PydanticJSONRenderer] = None
