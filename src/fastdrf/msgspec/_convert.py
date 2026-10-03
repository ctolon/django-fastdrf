"""
Conversions msgspec needs for Django objects, shared by the renderer and the
Django utilities (:mod:`fastdrf.msgspec.http`, :mod:`fastdrf.msgspec.html`).
"""

import functools

from django.utils.functional import Promise

from fastdrf.registry import msgspec_hooks


def convert(obj):
    """
    The registered encoding of ``obj``, or its text for a lazy or subclassed
    string; ``NotImplemented`` for anything else.
    """
    hooks = msgspec_hooks(type(obj))
    if hooks is not None and hooks.encode is not None:
        # A type of the project's (fastdrf.registry.register_msgspec_type),
        # first: it may be iterable or a str subclass too.
        return hooks.encode(obj)
    # msgspec only encodes exact ``str``. ``SafeString``, ``ErrorDetail`` and
    # ``TextChoices`` members are subclasses, which ``str.__str__`` copies
    # into a plain string; lazy translations are proxies that ``str()``
    # evaluates.
    if isinstance(obj, str):
        return str.__str__(obj)
    if isinstance(obj, Promise):
        return str(obj)
    return NotImplemented


def django_hook(obj):
    """The conversions of ``DjangoJSONEncoder`` that msgspec does not make."""
    value = convert(obj)
    if value is NotImplemented:
        # The error of ``json`` with Django's encoder.
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
    return value


@functools.cache
def encoder():
    """
    The encoder of the Django utilities, shared by threads as msgspec
    encoders can be. A Decimal is a string, as ``DjangoJSONEncoder`` writes it.
    """
    import msgspec

    return msgspec.json.Encoder(enc_hook=django_hook)


def caller_first(enc_hook):
    """
    ``enc_hook`` before the registry and the Django conversions: one that
    raises ``NotImplementedError`` leaves the value to them, as a msgspec
    schema serializer's ``Meta.enc_hook`` does.
    """
    from fastdrf.registry import _encoder

    registered = _encoder(enc_hook)

    def hook(obj):
        try:
            return registered(obj)
        except NotImplementedError:
            # Declined by ``enc_hook``, and not a registered type.
            return django_hook(obj)

    return hook
