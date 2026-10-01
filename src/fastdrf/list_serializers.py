"""
List serializers whose child refers to them weakly.

DRF binds a list serializer's child to it (``child.parent``) and the list
holds the child: a reference cycle. ``many_init`` also gives the child the
list's ``instance``, so every object of a page (and its prefetched relations)
stays alive until the cyclic garbage collector runs. Here the child's
``parent`` is a weak proxy of the list: while the list exists the child reads
it as before (``root``, ``context``, ``parent.instance``), and once nothing
else refers to the list, reference counting frees it with its instances.

Use them per serializer (``Meta.list_serializer_class``) or for a project's
base serializer (``default_list_serializer_class``)::

    from fastdrf import serializers
    from fastdrf.list_serializers import ListSerializer

    class ModelSerializer(serializers.ModelSerializer):
        default_list_serializer_class = ListSerializer

A child kept after its list is gone (``serializer.child`` stored on its own)
raises ``ReferenceError`` when it reads ``parent``; ``child.parent is
serializer`` is False for the proxy, so compare with ``==``.

:class:`SchemaListSerializer` is the same for msgspec and pydantic
serializers, and :class:`PrefetchListSerializer` adds one enrichment step per
list before its items are represented.
"""

import weakref

from django.db.models import prefetch_related_objects
from django.db.models.manager import BaseManager

from fastdrf import serializers, typed
from fastdrf.utils import framework_base

__all__ = [
    "ListSerializer",
    "PrefetchListSerializer",
    "SchemaListSerializer",
    "WeakChildMixin",
    "bind_child_weakly",
]


def bind_child_weakly(list_serializer):
    """Make the child of ``list_serializer`` refer to it through a weak proxy."""
    child = list_serializer.child
    if child is not None and child.parent is list_serializer:
        child.parent = weakref.proxy(list_serializer)


@framework_base
class WeakChildMixin:
    """For a ``ListSerializer`` subclass: bind the child weakly once it is bound."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        bind_child_weakly(self)


@framework_base
class ListSerializer(WeakChildMixin, serializers.ListSerializer):
    """fastdrf's ``ListSerializer`` with a weakly bound child."""


@framework_base
class SchemaListSerializer(WeakChildMixin, typed.SchemaListSerializer):
    """The list serializer of msgspec and pydantic serializers, weakly bound."""


class PrefetchListSerializer(ListSerializer):
    """
    The weak ``ListSerializer`` that enriches all of its items at once before
    DRF represents them, as ``prefetch_related`` does for the ORM::

        class InventoryListSerializer(PrefetchListSerializer):
            prefetch_related = ["warehouse"]  # as for QuerySet.prefetch_related

            def prefetch(self, instances):
                stock = self.context["inventory"].stock([i.sku for i in instances])
                for item in instances:
                    item.available = stock[item.sku]

    Select it with ``Meta.list_serializer_class``. Its ``to_representation``
    is its own, so the list is represented by DRF's code rather than a
    compiled class: a compiling backend with ``SERIALIZER_BACKEND_FALLBACK =
    "error"`` raises for it, as for any list serializer that overrides
    ``to_representation``.
    """

    #: Relations of the instances to load, as ``QuerySet.prefetch_related``
    #: takes them (names or ``Prefetch`` objects), with Django's
    #: ``prefetch_related_objects()``.
    prefetch_related = ()

    def prefetch(self, instances):
        """
        Load onto ``instances`` what their representation reads, for all of
        them at once. Called once per list with the materialized list, after
        ``prefetch_related``; the default does nothing.
        """

    def to_representation(self, data):
        # A manager (a nested to-many relation) is read once, as DRF reads it.
        items = data.all() if isinstance(data, BaseManager) else data
        instances = items if type(items) is list else list(items)
        if self.prefetch_related:
            prefetch_related_objects(instances, *self.prefetch_related)
        self.prefetch(instances)
        return super().to_representation(instances)
