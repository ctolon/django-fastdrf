"""``serializer.data`` from a compiled class, for fastdrf's serializer bases."""

from django.db import models
from rest_framework import serializers

from fastdrf._classify import (
    _instance_shadow_names,
    has_async_representation,
    is_static,
)
from fastdrf._inspection import _PLAIN_KWARGS
from fastdrf.settings import fastdrf_settings
from fastdrf.utils import user_defines

#: Set by :mod:`fastdrf.mixins` on a serializer that a generic view built,
#: validated and saved with framework code alone (:func:`fields_from_class`).
FIELDS_FROM_CLASS = "_fastdrf_fields_from_class"


def fields_from_class(serializer):
    """
    :func:`~fastdrf._classify.is_static` for a serializer whose fields exist
    on the instance because framework code built and used them: a generic
    view's serializer after validation and the save, when no
    ``get_serializer*`` or ``perform_*`` of the project's and no method of
    the serializer's class could have edited them. Only the write mixins of
    :mod:`fastdrf.mixins` mark such an instance (:data:`FIELDS_FROM_CLASS`).
    """
    if not vars(serializer).get(FIELDS_FROM_CLASS, False):
        return False
    if isinstance(serializer, serializers.ListSerializer):
        serializer = serializer.child
    return _instance_shadow_names(type(serializer)) is not None and (
        _PLAIN_KWARGS.issuperset(serializer._kwargs)
    )


def compiled_data(serializer):
    """
    ``serializer.data`` produced by a compiled class, when
    ``SERIALIZER_BACKEND`` (or ``Meta.serializer_backend``) asks for one and
    the serializer compiles (see :mod:`fastdrf.compiler`), None for
    DRF's code.
    """
    source = serializer.instance
    if (
        (
            fastdrf_settings.SERIALIZER_BACKEND == "drf"
            and not _declares_backend(serializer)
        )
        or user_defines(serializer, "data")
        or source is None
        or getattr(serializer, "_errors", None)
        or hasattr(serializer, "_data")
    ):
        return None
    from fastdrf.compiler import compiled_for, declines_source

    # A static serializer is looked up by its class before it is classified:
    # classifying one with nested serializers builds all their fields, which
    # is what the compiled class saves.
    if not is_static(serializer) and has_async_representation(serializer):
        return None
    encoder = compiled_for(serializer)
    if encoder is None:
        return None
    if not isinstance(serializer, serializers.ListSerializer) and declines_source(
        serializer, source
    ):
        return None
    return _producer(serializer, encoder, source)


def _producer(serializer, encoder, source):
    if isinstance(serializer, serializers.ListSerializer):

        def produce(serializer):
            from fastdrf.compiler import declines_source

            items = (
                source.all()
                if isinstance(source, models.manager.BaseManager)
                else source
            )
            items = list(items)
            if declines_source(serializer, items):
                # DRF's ListSerializer.to_representation, on the items read.
                serializer._data = serializer.to_representation(items)
                return serializer.data
            try:
                serializer._data = encoder.dump_many(items, serializer.context)
            except encoder.error as exc:
                serializer._data = _unreadable(serializer, exc, items)
            return serializer.data

    else:

        def produce(serializer):
            try:
                serializer._data = encoder.dump(source, serializer.context)
            except encoder.error as exc:
                serializer._data = _unreadable(serializer, exc, source)
            return serializer.data

    return produce


def _unreadable(serializer, error, source):
    """
    DRF's representation of a source the compiled class failed to read, or
    the backend's ``error`` when that is the result
    (:func:`fastdrf.compiler.unreadable_source`). The backends take any
    exception raised while reading an attribute for a missing attribute;
    DRF's own read raises it as itself.
    """
    from fastdrf.compiler import unreadable_source

    if not unreadable_source(serializer, error):
        raise error
    return serializer.to_representation(source)


def _declares_backend(serializer):
    target = (
        serializer.child
        if isinstance(serializer, serializers.ListSerializer)
        else serializer
    )
    return (
        getattr(getattr(target, "Meta", None), "serializer_backend", None) is not None
    )
