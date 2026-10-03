"""
``{% load fastdrf_msgspec %}``: Django's ``json_script`` filter encoded by
msgspec (:func:`fastdrf.msgspec.html.json_script`), in the templates that
load it. Found when ``"fastdrf"`` is in ``INSTALLED_APPS``.
"""

from django import template

register = template.Library()


@register.filter(is_safe=True)
def json_script(value, element_id=None):
    """
    Output value JSON-encoded, wrapped in a <script type="application/json">
    tag (with an optional id).
    """
    # Imported here: Django's template engine imports the library of every
    # installed application, in projects without msgspec too.
    from fastdrf.msgspec.html import json_script

    return json_script(value, element_id)
