"""Optional orjson HTTP transport; importing the package needs no extra."""

import importlib

__all__ = ["ORJSONParser", "ORJSONRenderer"]

_MODULES = {"ORJSONParser": "parsers", "ORJSONRenderer": "renderers"}


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{_MODULES[name]}"), name)
