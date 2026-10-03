"""
A Django JSON response encoded by msgspec, for views outside DRF (the
``msgspec`` extra). It does not import DRF.
"""

import msgspec
from django.http import HttpResponse

from fastdrf.msgspec._convert import caller_first, encoder

__all__ = ["JsonResponse"]


class JsonResponse(HttpResponse):
    """
    An HTTP response with a JSON body encoded by msgspec.

    Like Django's ``JsonResponse``, but the body is encoded by msgspec: the
    types registered with ``register_msgspec_type`` are understood, and the
    output differs from ``DjangoJSONEncoder`` as listed in
    docs/django-utilities.md. ``enc_hook`` converts the objects msgspec does
    not encode before the registry does; it raises ``NotImplementedError``
    for the others.
    """

    def __init__(self, data, *, safe=True, enc_hook=None, **kwargs):
        if safe and not isinstance(data, dict):
            raise TypeError(
                "In order to allow non-dict objects to be serialized set the "
                "safe parameter to False."
            )
        kwargs.setdefault("content_type", "application/json")
        super().__init__(content=_encode(data, enc_hook), **kwargs)


def _encode(data, enc_hook):
    if enc_hook is None:
        return encoder().encode(data)
    # A hook per call is rare: its encoder is not kept.
    return msgspec.json.Encoder(enc_hook=caller_first(enc_hook)).encode(data)
