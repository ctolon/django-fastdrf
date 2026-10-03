"""JSON response rendering with the optional msgspec codec."""

import contextlib
import copy
import decimal
import functools
import threading
from collections import deque
from collections.abc import Iterator

import msgspec
from django.conf import settings
from django.core.signals import setting_changed
from django.db.models import QuerySet
from django.dispatch import receiver
from django.utils.functional import Promise
from rest_framework.renderers import JSONRenderer

from fastdrf.registry import _MSGSPEC_TYPES, msgspec_hooks
from fastdrf.renderers import _DATA_RENDERERS, _without_indent

__all__ = ["MsgspecJSONRenderer", "enc_hook"]


# The conversions performed by ``_render_hook`` during a render,
# per thread (an encoder runs in the rendering thread); ``_kept`` says some
# thread kept any, avoiding thread-local access until the first hook call.
_read = threading.local()
_kept = False
_encoder_class = JSONRenderer.encoder_class

# ``settings.DEBUG``, read once: a lazy settings attribute costs more than a
# small render's checks.
_debug: bool | None = None


@receiver(setting_changed)
def _forget_debug(*, setting, **kwargs):
    global _debug
    if setting == "DEBUG":
        _debug = None


def _debugging() -> bool:
    global _debug
    if _debug is None:
        _debug = bool(settings.DEBUG)
    return _debug


def enc_hook(obj):
    hooks = msgspec_hooks(type(obj))
    if hooks is not None and hooks.encode is not None:
        # A type of the project's (fastdrf.registry.register_msgspec_type),
        # first: it may be iterable or a str subclass too.
        return hooks.encode(obj)
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
    # The rest in DRF's encoder's order: a QuerySet as a list, a mapping (a
    # MappingProxyType, a ChainMap) as a dict, other iterables as lists.
    if isinstance(obj, QuerySet):
        return list(obj)
    if hasattr(obj, "__getitem__"):
        with contextlib.suppress(Exception):
            return list(obj) if isinstance(obj, (list, tuple)) else dict(obj)
    elif hasattr(obj, "__iter__") and not isinstance(obj, (bytes, dict)):
        return list(obj)
    # DRF's encoder fails with ``json``'s error; code handling it sees the same.
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _render_hook(obj):
    if isinstance(obj, str) and (
        not _MSGSPEC_TYPES or msgspec_hooks(type(obj)) is None
    ):
        # A pure conversion: DRF's encoder makes the same string again, with
        # nothing to replay (an ``ErrorDetail``, a ``TextChoices`` member).
        return str.__str__(obj)
    if isinstance(obj, Promise) and msgspec_hooks(type(obj)) is None:
        return str(obj)
    # Replay state belongs to the renderer, not to callers using the public
    # encoding hook on its own.
    try:
        value = enc_hook(obj)
    except Exception as error:
        _remember_conversion(obj, error, failed=True)
        raise
    return _remember_conversion(obj, value)


def _has_decimal_key(value):
    # Whether a hook's result holds a Decimal key at any depth: msgspec writes
    # one as a bare number, which is not JSON. Only hook results are looked
    # at; they are small, and the data's own keys are the project's.
    if isinstance(value, dict):
        return any(
            isinstance(key, decimal.Decimal) or _has_decimal_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_decimal_key(item) for item in value)
    return False


def _remember_conversion(obj, value, *, failed=False):
    # Replay each occurrence, not just iterators: registered hooks, mappings
    # and tolist() may also consume or have effects. Keep failures as well as
    # values.
    global _kept
    _kept = True
    saved = vars(_read).setdefault("items", {})
    if id(obj) not in saved:
        saved[id(obj)] = (obj, deque())
    saved[id(obj)][1].append((value, failed))
    if not failed and _has_decimal_key(value):
        _read.decimal_key = True
    return value


def _replay(saved):
    value, failed = saved[1].popleft()
    if failed:
        raise value
    return value


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
    encoder = msgspec.json.Encoder(enc_hook=_render_hook, decimal_format="number")

    def render(self, data, accepted_media_type=None, renderer_context=None):
        # Hooks may render another response in this thread. Each call owns its
        # replay buffers; a nested call must not consume or clear its parent's.
        previous = _read.__dict__.copy() if _kept else None
        if previous:
            _read.__dict__.clear()
        try:
            return self._render(data, accepted_media_type, renderer_context)
        finally:
            if _kept:
                _read.__dict__.clear()
                if previous:
                    _read.__dict__.update(previous)

    def _render(self, data, accepted_media_type, renderer_context):
        if data is None:
            return b""
        if (
            self.ensure_ascii
            or not self.compact
            or self.encoder_class is not _encoder_class
        ):
            # The project's UNICODE_JSON or COMPACT_JSON, which msgspec's
            # compact UTF-8 output does not follow: DRF's bytes.
            return self._drf_render(data, accepted_media_type, renderer_context)
        # ``get_indent`` parses the media type; it is called only when it
        # could answer something other than None.
        if not _without_indent(
            self, MsgspecJSONRenderer, accepted_media_type, renderer_context
        ) and (
            # ``indent=0`` in the context is an indentation for ``json``.
            self.get_indent(accepted_media_type, renderer_context or {}) is not None
        ):
            return self._drf_render(data, accepted_media_type, renderer_context)
        try:
            output = self.encoder.encode(data)
        except TypeError:
            # A key ``json`` writes as a string (``True``, ``None``) and
            # msgspec refuses, or a value neither encodes: DRF's bytes, or
            # DRF's error.
            return self._drf_render(data, accepted_media_type, renderer_context)
        # A one-byte search is memchr, far cheaper than a longer needle on a
        # large body: look for the first letter before the word.
        if (
            (b"N" in output and b"NaN" in output)
            or (b"I" in output and b"Infinity" in output)
        ) and not _valid_json(output):
            # A non-finite Decimal, which msgspec writes as a bare token that
            # no JSON parser reads (the words inside a string are valid
            # JSON): DRF's error.
            return self._drf_render(data, accepted_media_type, renderer_context)
        if (
            (_debug if _debug is not None else _debugging())
            or (_kept and vars(_read).get("decimal_key"))
        ) and not _valid_json(output):
            # A Decimal key, which msgspec writes as a bare number: invalid
            # JSON. A conversion hook's result is checked for one as it is
            # made. Finding one in the data itself takes a pass over the
            # output as long as the encoding (a datetime or a URL has colons
            # too), so that is checked while developing, where DRF's error
            # shows. Serializer output has string keys only.
            return self._drf_render(data, accepted_media_type, renderer_context)
        # U+2028 and U+2029 are E2 80 A8 and E2 80 A9 in UTF-8.
        if b"\xe2" in output and b"\xe2\x80" in output:
            # Escape the separators DRF escapes so the output can be embedded
            # in JavaScript.
            output = output.replace(b"\xe2\x80\xa8", b"\\u2028").replace(
                b"\xe2\x80\xa9", b"\\u2029"
            )
        return output

    def _drf_render(self, data, accepted_media_type, renderer_context):
        # DRF's renderer, with an encoder that knows the registered types, on
        # a copy: a renderer may be shared by threads (a stream's).
        renderer = copy.copy(self)
        renderer.encoder_class = _with_registered_types(self.encoder_class)
        if isinstance(data, Iterator) and id(data) in vars(_read).get("items", {}):
            # DRF's renderer reads a root iterator before its encoder does.
            data = _replay(_read.items[id(data)])
        try:
            return JSONRenderer.render(
                renderer, data, accepted_media_type, renderer_context
            )
        finally:
            vars(_read).pop("items", None)


def _valid_json(output):
    try:
        msgspec.json.decode(output)
    except msgspec.DecodeError:
        return False
    return True


@functools.cache
def _with_registered_types(encoder_class):
    """``encoder_class`` encoding the types of ``register_msgspec_type`` too."""

    class Encoder(encoder_class):
        def default(self, obj):
            read = vars(_read).get("items", {}).get(id(obj))
            if read is not None and read[0] is obj and read[1]:
                # Read by msgspec's attempt.
                return _replay(read)
            hooks = msgspec_hooks(type(obj))
            if hooks is not None and hooks.encode is not None:
                return hooks.encode(obj)
            return super().default(obj)

    Encoder.__qualname__ = Encoder.__name__ = f"Registered{encoder_class.__name__}"
    return Encoder


_DATA_RENDERERS[MsgspecJSONRenderer] = None
