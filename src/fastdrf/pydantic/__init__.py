"""
pydantic integration (the ``pydantic`` extra).

* :class:`PydanticSerializer`: a DRF serializer validated and represented
  by a pydantic model.
* :class:`PydanticJSONParser` / :class:`PydanticJSONRenderer`: independent
  JSON transport through pydantic-core.
* ``FASTDRF["SERIALIZER_BACKEND"] = "pydantic"``: compile existing DRF
  serializers to models (see :mod:`.compiler`).

The names are imported on first use, so importing this package does not
need pydantic.
"""

import importlib

__all__ = [
    "PydanticSerializer",
    "serializer_for",
    "PydanticJSONParser",
    "PydanticJSONRenderer",
]

_MODULES = {
    "PydanticSerializer": "serializers",
    "serializer_for": "serializers",
    "PydanticJSONParser": "parsers",
    "PydanticJSONRenderer": "renderers",
}


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{_MODULES[name]}"), name)
