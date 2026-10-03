"""Opt-in orjson output, independent of serializer selection."""

import orjson
from rest_framework.renderers import JSONRenderer

from fastdrf.renderers import _DATA_RENDERERS, _without_indent

__all__ = ["ORJSONRenderer"]

_encoder_class = JSONRenderer.encoder_class
_default = _encoder_class().default


class ORJSONRenderer(JSONRenderer):
    """Native orjson output with DRF's default for unsupported Python values.

    Formatting and custom encoder requests use DRF before encoding starts.
    Native encoding errors propagate without retrying consumed iterators.
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
                    self, ORJSONRenderer, accepted_media_type, renderer_context
                )
                and self.get_indent(accepted_media_type, renderer_context or {})
                is not None
            )
        ):
            return super().render(data, accepted_media_type, renderer_context)
        output = orjson.dumps(data, default=_default)
        if b"\xe2\x80" in output:
            output = output.replace(b"\xe2\x80\xa8", b"\\u2028").replace(
                b"\xe2\x80\xa9", b"\\u2029"
            )
        return output


_DATA_RENDERERS[ORJSONRenderer] = None
