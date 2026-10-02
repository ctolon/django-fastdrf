"""Conservative hook inspection and bounded class-keyed optimization caches."""

import functools
import importlib
import inspect
import threading
import weakref

from django.db import models
from django.db.models import fields as model_fields
from django.db.models import manager, query_utils
from django.db.models.fields import (
    files,
    json,
    related,
    related_descriptors,
    reverse_related,
)
from rest_framework import fields, relations, serializers

__all__ = [
    "CLASS_CACHE_SIZE",
    "class_cache",
    "definer",
    "depends_on_classification",
    "framework_base",
    "is_framework_class",
    "user_defines",
]

# Register actual imported classes, never a user-controlled module prefix.
# Weak: a base registered with :func:`framework_base` may be created at run
# time, and the registry must not keep it alive.
_DEFAULTS = weakref.WeakSet([object, type])
for _module in (
    models,
    manager,
    query_utils,
    model_fields,
    files,
    json,
    related,
    related_descriptors,
    reverse_related,
    fields,
    relations,
    serializers,
):
    _DEFAULTS.update(
        value for value in vars(_module).values() if issubclass(type(value), type)
    )
del _module
try:
    from django.db.models.fields import composite
except ImportError:  # Django < 5.2
    pass
else:
    _DEFAULTS.update(
        value for value in vars(composite).values() if issubclass(type(value), type)
    )

# Modules of Django's contrib applications that define model fields: they
# import models, so their classes are registered once the application
# registry is ready, for the applications installed.
_CONTRIB_MODULES = {
    "django.contrib.contenttypes": ("django.contrib.contenttypes.fields",),
}
_contrib_registered = False


def _register_contrib():
    global _contrib_registered
    from django.apps import apps

    if not apps.ready:
        return
    _contrib_registered = True
    for app, modules in _CONTRIB_MODULES.items():
        if apps.is_installed(app):
            for name in modules:
                module = importlib.import_module(name)
                _DEFAULTS.update(
                    value
                    for value in vars(module).values()
                    if issubclass(type(value), type) and value.__module__ == name
                )
    _clear_dependents()


# Per decorated function. Weak keys alone cannot collect a class captured by
# a cached value or argument (a field template refers to its serializer's
# model); publishing at most this many entries before eviction bounds such
# cycles without modifying application classes.
CLASS_CACHE_SIZE = 1024


def _forget(cache, key, reference):
    # Called by the garbage collector, possibly while the cache's lock is
    # held on this thread: no lock here. A newer entry under the same key
    # (another class) stays.
    entry = cache.get(key)
    if entry is not None and entry[0] is reference:
        cache.pop(key, None)


def class_cache(function):
    """
    Memoize ``function(cls, *args)`` with weak class keys and bounded
    publication.

    ``functools.cache`` holds a strong reference to every class it has seen,
    and DRF creates classes at request time (``Meta.depth`` builds a nested
    serializer class per instance). Hits take no lock; a value is computed
    without one, since it may run the project's constructors or field hooks,
    and published only if no ``cache_clear()`` happened meanwhile.
    """
    # Keyed by the class's identity, which a dict looks up at C speed; the
    # weak reference tells a live class from a later one that reused the
    # identity, and forgets the entry when the class goes away.
    cache = {}
    generation = 0
    published = 0
    lock = threading.Lock()

    @functools.wraps(function)
    def cached(cls, *args):
        nonlocal published
        entry = cache.get(id(cls))
        if entry is not None and entry[0]() is cls:
            try:
                return entry[1][args]
            except KeyError:
                pass
        # Racing callers compute the same value; the first one is kept.
        started = generation
        value = function(cls, *args)
        with lock:
            # A clear meanwhile means what ``function`` reads changed: the
            # value may be stale and is not published.
            if started == generation:
                entry = cache.get(id(cls))
                if entry is not None and entry[0]() is not cls:
                    entry = None  # a dead class's identity, reused
                if entry is not None and args in entry[1]:
                    return entry[1][args]
                if published >= CLASS_CACHE_SIZE:
                    cache.clear()
                    published = 0
                    entry = None
                if entry is None:
                    key = id(cls)
                    forget = functools.partial(_forget, cache, key)
                    entry = cache[key] = (weakref.ref(cls, forget), {})
                entry[1][args] = value
                published += 1
        return value

    def cache_clear():
        nonlocal generation, published
        with lock:
            generation += 1
            cache.clear()
            published = 0

    def cache_size():
        with lock:
            return sum(len(values) for _, values in list(cache.values()))

    cached.cache_clear = cache_clear
    cached.cache_size = cache_size
    return cached


# Class caches whose answers are made from :func:`user_defines` and
# :func:`is_framework_class`: a registration made later drops them.
_DEPENDENTS = []


def depends_on_classification(cache):
    """
    Register a class cache whose answers depend on which classes are the
    framework's: a :func:`class_cache` function (its ``cache_clear``), or a
    mapping or cache object with ``clear()``. :func:`framework_base` clears it.
    """
    _DEPENDENTS.append(getattr(cache, "cache_clear", None) or cache.clear)
    return cache


def _clear_dependents():
    _user_defines.cache_clear()
    for clear in _DEPENDENTS:
        clear()


def framework_base(cls):
    """
    Register a package-owned base without modifying it or any DRF class.
    Register at import time: what was decided before this registration
    treated ``cls`` as the project's code, and is dropped. The registry does
    not keep a dynamically created class alive.
    """
    _DEFAULTS.add(cls)
    _clear_dependents()
    return cls


def is_framework_class(cls):
    if not _contrib_registered:
        _register_contrib()
    return cls in _DEFAULTS


def definer(cls, name):
    """The class in ``cls.__mro__`` whose ``__dict__`` defines ``name``, or None."""
    return next((base for base in cls.__mro__ if name in vars(base)), None)


def user_defines(obj, *names):
    """
    Whether any of the hooks ``names`` of ``obj`` (a class or an instance)
    is the project's code: defined by a class that is not Django's, DRF's or
    a registered :func:`framework_base`, or assigned to the instance.
    """
    if inspect.isclass(obj):
        cls = obj
    else:
        if not getattr(obj, "__dict__", {}).keys().isdisjoint(names):
            return True
        cls = type(obj)
    return _user_defines(cls, names)


@class_cache
def _user_defines(cls, names):
    return any(
        owner is not None and owner not in _DEFAULTS
        for owner in (definer(cls, name) for name in names)
    )
