"""
DRF's create and update mixins whose saved serializer is represented by the
compiled encoder of its class.

After ``is_valid()`` a serializer's fields exist on the instance, so they
could have been edited, and the compiler looks its encoder up by those fields
(:func:`fastdrf.compiler.signature`). When the view built, validated and saved
the serializer with framework code alone, the fields are those of its class:
these mixins mark it so, and ``serializer.data`` uses the class's encoder.
"""

import inspect

from rest_framework import mixins, status
from rest_framework.response import Response
from rest_framework.serializers import ListSerializer

from fastdrf.settings import fastdrf_settings
from fastdrf.utils import (
    class_cache,
    depends_on_classification,
    framework_base,
    is_framework_class,
)
from fastdrf.views import _framework_only

__all__ = ["CreateModelMixin", "UpdateModelMixin"]

_SERIALIZER_FACTORIES = (
    "get_serializer",
    "get_serializer_class",
    "get_serializer_context",
)
# Project code under any of these names may build, change or save the
# serializer another way: the mark is not set.
_CREATES = ("create", "perform_create", *_SERIALIZER_FACTORIES)
_UPDATES = ("update", "partial_update", "perform_update", *_SERIALIZER_FACTORIES)


@framework_base
class CreateModelMixin(mixins.CreateModelMixin):
    """
    DRF's ``CreateModelMixin``; with a compiled serializer backend, the
    response data of a create done by framework code alone is produced by
    the encoder of the serializer's class.
    """

    def create(self, request, *args, **kwargs):
        if not _framework_only(self, _CREATES):
            return super().create(request, *args, **kwargs)
        # DRF's ``create``.
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        _mark_fields_from_class(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(
            serializer.data, status=status.HTTP_201_CREATED, headers=headers
        )


@framework_base
class UpdateModelMixin(mixins.UpdateModelMixin):
    """
    DRF's ``UpdateModelMixin``; with a compiled serializer backend, the
    response data of an update done by framework code alone is produced by
    the encoder of the serializer's class.
    """

    def update(self, request, *args, **kwargs):
        if not _framework_only(self, _UPDATES):
            return super().update(request, *args, **kwargs)
        # DRF's ``update``.
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        _mark_fields_from_class(serializer)

        if getattr(instance, "_prefetched_objects_cache", None):
            # If 'prefetch_related' has been applied to a queryset, we need to
            # forcibly invalidate the prefetch cache on the instance.
            instance._prefetched_objects_cache = {}

        return Response(serializer.data)


def _mark_fields_from_class(serializer):
    """
    Mark ``serializer``, which the view built, validated and saved with
    framework code alone, when no method of its class could have edited its
    fields (:func:`fastdrf._compiled.fields_from_class`).
    """
    from fastdrf._compiled import FIELDS_FROM_CLASS, _declares_backend

    # Only the compiler reads the mark.
    if (
        fastdrf_settings.SERIALIZER_BACKEND != "drf" or _declares_backend(serializer)
    ) and (
        _declarative_class(type(serializer))
        and (
            not isinstance(serializer, ListSerializer)
            or _declarative_class(type(serializer.child))
        )
    ):
        setattr(serializer, FIELDS_FROM_CLASS, True)


@depends_on_classification
@class_cache
def _declarative_class(serializer_class):
    """
    Whether no class of the project's in ``serializer_class.__mro__``
    defines a method or property: a ``validate()``, ``create()`` or
    ``to_internal_value()`` written for DRF may edit ``self.fields``.
    """
    return not any(
        inspect.isroutine(value)
        or isinstance(value, (property, classmethod, staticmethod))
        for klass in serializer_class.__mro__
        if not is_framework_class(klass)
        for value in vars(klass).values()
    )
