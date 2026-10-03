"""DRF's JSON renderer with the encoder it builds per call kept."""

from rest_framework import renderers
from rest_framework.compat import LONG_SEPARATORS, SHORT_SEPARATORS

__all__ = ["JSONRenderer"]

# DRF's members as this module found them; a project that replaces one later
# gets DRF's ``render`` back.
_DRF_RENDER = renderers.JSONRenderer.render
_DRF_GET_INDENT = renderers.JSONRenderer.get_indent


def _without_indent(renderer, owner, accepted_media_type, renderer_context):
    """
    Whether ``renderer`` may render without calling ``get_indent``: its class
    renders with ``owner.render`` and DRF's ``get_indent``, the instance has
    no ``get_indent`` of its own, and no media-type parameter or context
    ``indent`` can ask for an indentation.
    """
    cls = type(renderer)
    return (
        cls.render is owner.render
        and cls.get_indent is _DRF_GET_INDENT
        and "get_indent" not in vars(renderer)
        and not (accepted_media_type and ";" in accepted_media_type)
        and not (renderer_context and renderer_context.get("indent") is not None)
    )


def _dumps_encoder(renderer):
    """
    The encoder ``json.dumps`` builds in DRF's ``JSONRenderer.render``: its
    arguments, the ones ``dumps`` passes explicitly included.
    """
    return renderer.encoder_class(
        skipkeys=False,
        ensure_ascii=renderer.ensure_ascii,
        check_circular=True,
        allow_nan=not renderer.strict,
        indent=None,
        separators=SHORT_SEPARATORS if renderer.compact else LONG_SEPARATORS,
        default=None,
        sort_keys=False,
    )


class JSONRenderer(renderers.JSONRenderer):
    """
    DRF's ``JSONRenderer`` that keeps the encoder ``render`` builds per call,
    one per ``(encoder_class, ensure_ascii, compact, strict)``, read when it
    renders (a project may change them after import): DRF's bytes.

    Indented output, a subclass's own ``render`` or ``get_indent``, and a
    replaced ``JSONRenderer.render`` or ``get_indent`` of DRF's go through
    DRF's ``render``. The encoder is shared by threads: an ``encoder_class``
    must not keep state between ``encode`` calls, as DRF's does not.
    """

    _encoders = {}

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if (
            data is None
            or renderers.JSONRenderer.render is not _DRF_RENDER
            or not _without_indent(
                self, JSONRenderer, accepted_media_type, renderer_context
            )
        ):
            return super().render(data, accepted_media_type, renderer_context)
        key = (self.encoder_class, self.ensure_ascii, self.compact, self.strict)
        encoder = self._encoders.get(key)
        if encoder is None:
            encoder = self._encoders.setdefault(key, _dumps_encoder(self))
        # DRF's ``render`` output after ``json.dumps``.
        ret = encoder.encode(data)
        return ret.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").encode()


# Exact renderer classes whose bytes depend on the data, the media type and
# the indentation only (not on DRF's ``Response``), so that
# :class:`fastdrf.response.DataResponse` renders with them -> the renderer it
# uses instead, or None for the accepted one.
_DATA_RENDERERS = {renderers.JSONRenderer: JSONRenderer(), JSONRenderer: None}
