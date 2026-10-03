"""
Django's ``json_script`` encoded by msgspec (the ``msgspec`` extra). The
template filter is in the ``fastdrf_msgspec`` template library.
"""

import msgspec
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from fastdrf.msgspec._convert import caller_first, encoder

__all__ = ["json_script"]


def json_script(value, element_id=None, *, enc_hook=None):
    """
    Encode ``value`` as JSON in a ``<script type="application/json">`` tag,
    with an ``id`` when ``element_id`` is given, as Django's ``json_script``
    does. ``<``, ``>`` and ``&`` are escaped as Django escapes them, and
    U+2028 and U+2029, which Django's ASCII output never holds, too.
    """
    if enc_hook is None:
        encoded = encoder().encode(value)
    else:
        encoded = msgspec.json.Encoder(enc_hook=caller_first(enc_hook)).encode(value)
    json_str = (
        encoded.replace(b"<", b"\\u003C")
        .replace(b">", b"\\u003E")
        .replace(b"&", b"\\u0026")
        # U+2028 and U+2029 are E2 80 A8 and E2 80 A9 in UTF-8.
        .replace(b"\xe2\x80\xa8", b"\\u2028")
        .replace(b"\xe2\x80\xa9", b"\\u2029")
        .decode()
    )
    if element_id:
        template = '<script id="{}" type="application/json">{}</script>'
        args = (element_id, mark_safe(json_str))
    else:
        template = '<script type="application/json">{}</script>'
        args = (mark_safe(json_str),)
    return format_html(template, *args)
