"""JSON response rendering with the optional msgspec codec."""

import msgspec
from django.utils.functional import Promise
from rest_framework.renderers import JSONRenderer

from fastdrf.renderers import _DATA_RENDERERS, _without_indent

__all__ = ["MsgspecJSONRenderer", "enc_hook"]


def enc_hook(obj):
    # msgspec only encodes exact ``str``. ``ErrorDetail`` is a subclass and
    # ``str.__str__`` copies it into a plain string; lazy translations are
    # proxies that ``str()`` evaluates.
    if isinstance(obj, str):
        return str.__str__(obj)
    if isinstance(obj, Promise):
        return str(obj)
    if hasattr(obj, "tolist"):
        # numpy arrays and scalars, like DRF's encoder.
        return obj.tolist()
    if hasattr(obj, "__iter__") and not isinstance(obj, (bytes, dict)):
        # QuerySets and other iterables DRF's encoder turns into lists.
        return list(obj)
    # DRF's encoder fails with ``json``'s error; code handling it sees the same.
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class MsgspecJSONRenderer(JSONRenderer):
    """
    JSON renderer backed by ``msgspec.json``.

    It differs from DRF's encoder in a few documented ways: ``timedelta``
    renders as an ISO 8601 duration instead of seconds, ``bytes`` as base64,
    float NaN/infinity as ``null`` instead of raising, raw ``Decimal`` values
    keep their digits (``1.50`` instead of ``1.5``), float exponents are
    spelled ``1e300`` instead of ``1e+300``, and an aware ``time`` renders
    where DRF raises. Data msgspec cannot encode as DRF does (a ``True`` or
    ``None`` key, a non-finite ``Decimal``) and indented output (the
    browsable API, or ``; indent=`` in the Accept header) go through DRF's
    renderer.
    """

    # DRF creates a renderer per request; encoders are thread-safe, so one
    # is shared. DRF's encoder writes raw Decimals as numbers; ``DecimalField``
    # already produced strings when ``COERCE_DECIMAL_TO_STRING`` is on.
    encoder = msgspec.json.Encoder(enc_hook=enc_hook, decimal_format="number")

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if data is None:
            return b""
        # ``get_indent`` parses the media type; it is called only when it
        # could answer something other than None.
        if not _without_indent(
            self, MsgspecJSONRenderer, accepted_media_type, renderer_context
        ) and (
            # ``indent=0`` in the context is an indentation for ``json``.
            self.get_indent(accepted_media_type, renderer_context or {}) is not None
        ):
            return super().render(data, accepted_media_type, renderer_context)
        try:
            output = self.encoder.encode(data)
        except TypeError:
            # A key ``json`` writes as a string (``True``, ``None``) and
            # msgspec refuses, or a value neither encodes: DRF's bytes, or
            # DRF's error.
            return JSONRenderer.render(
                self, data, accepted_media_type, renderer_context
            )
        if b"NaN" in output or b"Infinity" in output:
            # A non-finite Decimal, which msgspec writes as a bare token that
            # no JSON parser reads, or the words inside a string: DRF's
            # error, or DRF's bytes.
            return JSONRenderer.render(
                self, data, accepted_media_type, renderer_context
            )
        if b"\xe2\x80" in output:
            # Escape the separators DRF escapes so the output can be embedded
            # in JavaScript.
            output = output.replace(b"\xe2\x80\xa8", b"\\u2028").replace(
                b"\xe2\x80\xa9", b"\\u2029"
            )
        return output


_DATA_RENDERERS[MsgspecJSONRenderer] = None
