"""
msgspec integration (the ``msgspec`` extra).

* :class:`MsgspecSerializer`: a DRF serializer validated and represented by
  a msgspec ``Struct``.
* :class:`MsgspecJSONRenderer` / :class:`MsgspecJSONParser`: JSON renderer
  and parser.
* ``FASTDRF["SERIALIZER_BACKEND"] = "msgspec"``: compile existing DRF
  serializers to Structs (see :mod:`.compiler`).

The names are imported on first use, so importing this package does not
need msgspec.
"""

import importlib

__all__ = [
    "MsgspecJSONParser",
    "MsgspecJSONRenderer",
    "MsgspecSerializer",
    "serializer_for",
]

_MODULES = {
    "MsgspecJSONParser": "parsers",
    "MsgspecJSONRenderer": "renderers",
    "MsgspecSerializer": "serializers",
    "serializer_for": "serializers",
}


def __getattr__(name):
    if name not in _MODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{_MODULES[name]}")
    return getattr(module, name)
