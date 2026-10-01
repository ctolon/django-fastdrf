"""Which serializers are a function of their class; coroutine hooks refused."""

import inspect
import weakref
from inspect import iscoroutinefunction

from django.db import models
from django.utils.choices import CallableChoiceIterator
from rest_framework import fields, relations, serializers

from fastdrf._inspection import _FIELD_HOOKS, _PLAIN_KWARGS
from fastdrf.utils import (
    class_cache,
    depends_on_classification,
    is_framework_class,
    user_defines,
)

# Exact DRF field classes, defined in DRF's modules: a class of the
# project's, or one a module only imported, may bind or represent otherwise.
_BUILTIN_FIELDS = frozenset(
    value
    for module in (fields, relations)
    for value in vars(module).values()
    if inspect.isclass(value) and value.__module__ == module.__name__
)


@depends_on_classification
@class_cache
def _model_fields_call_code(model):
    """
    Return True if building a serializer field from a field of ``model`` may
    call the project's code: callable ``choices`` or ``limit_choices_to``
    (commonly a query), a ``limit_choices_to`` applied through the related
    model's default manager of the project's (its ``get_queryset()``), a
    model field class of the project's, whose attributes DRF reads, or a
    FilePathField, which lists its directory.
    """
    for field in model._meta.get_fields():
        if isinstance(field, models.FilePathField) or not is_framework_class(
            type(field)
        ):
            return True
        if isinstance(getattr(field, "choices", None), CallableChoiceIterator):
            return True
        limit = getattr(getattr(field, "remote_field", None), "limit_choices_to", None)
        if callable(limit) or (
            limit and user_defines(field.related_model._default_manager, "get_queryset")
        ):
            return True
    return False


# What DRF sets on a serializer instance over a class attribute of the same
# name. Any other instance attribute that shadows one of the class may change
# what DRF does: a method assigned to the instance, fields materialized (and
# perhaps edited), a ``Meta`` or ``url_field_name`` of its own.
_DRF_SHADOWS = frozenset({"_creation_counter", "initial", "default_empty_html"})

# Declared serializers are deep-copied into every instance, which builds them
# again from their arguments; these hooks could make the copy differ.
_BUILD_HOOKS = (*_FIELD_HOOKS, "bind", "__deepcopy__", "__new__")

# class -> the names an instance must not shadow, or None when it is not static
_static_classes: weakref.WeakKeyDictionary[type, frozenset[str] | None] = (
    weakref.WeakKeyDictionary()
)
depends_on_classification(_static_classes)


def is_static(serializer):
    """
    Return True if the fields of ``serializer`` (the child of a list
    serializer), and those of the serializers nested in it, are a function
    of its class.

    That holds for an instance built with the usual arguments only that
    shadows nothing of its class (no fields materialized, no method assigned),
    of a class that builds its fields with DRF's code alone from declarations
    and a model, whose declared fields are DRF's, children included, and
    whose declared serializers are static too. The compiler, the input
    recognizer and the classification below keep per-class answers for these
    alone.
    """
    if isinstance(serializer, serializers.ListSerializer):
        serializer = serializer.child
    names = _instance_shadow_names(type(serializer))
    return (
        names is not None
        and _PLAIN_KWARGS.issuperset(serializer._kwargs)
        and names.isdisjoint(vars(serializer))
    )


def _instance_shadow_names(cls):
    """
    The names an instance of ``cls`` must not set for its fields to be those
    of its class (:func:`is_static`), or None when they never are.
    """
    try:
        return _static_classes[cls]
    except KeyError:
        pass
    meta = getattr(cls, "Meta", None)
    model = getattr(meta, "model", None)
    static = (
        issubclass(cls, serializers.Serializer)
        # A child may build fields from its parent's context, or change them
        # when bound.
        and not getattr(meta, "depth", 0)
        and not user_defines(cls, *_BUILD_HOOKS)
        # The project's code, run whenever a ModelSerializer builds its fields.
        and not (
            issubclass(cls, serializers.ModelSerializer)
            and model is not None
            and _model_fields_call_code(model)
        )
        and all(_static_declaration(field) for field in cls._declared_fields.values())
    )
    names = frozenset(dir(cls)) - _DRF_SHADOWS if static else None
    # Racing threads store the same value.
    return _static_classes.setdefault(cls, names)


def _static_declaration(field):
    if isinstance(field, serializers.ListSerializer):
        return not user_defines(field, *_BUILD_HOOKS) and _static_declaration(
            field.child
        )
    if isinstance(field, serializers.BaseSerializer):
        return _instance_shadow_names(type(field)) is not None
    # DRF's own fields; a custom one may bind differently per parent. DRF's
    # collections and to-many relations bind the child they were declared with.
    return type(field) in _BUILTIN_FIELDS and all(
        _static_declaration(child)
        for child in (
            getattr(field, "child", None),
            getattr(field, "child_relation", None),
        )
        if child is not None
    )


# What DRF materializes on an instance the first time it is read. Once any of
# these exists, the instance's fields may have been changed; the class says
# nothing about such an instance.
_MATERIALIZED = frozenset(
    {"fields", "_validators", "_readable_fields", "_writable_fields"}
)

# Static serializer class -> whether its representation has a coroutine hook.
_class_representation: weakref.WeakKeyDictionary[type, bool] = (
    weakref.WeakKeyDictionary()
)
depends_on_classification(_class_representation)


def _classified_by_class(serializer):
    """
    Whether :func:`has_async_representation` of ``serializer`` is a property
    of its class: its fields are (:func:`is_static`), none exists yet, and
    nothing callable was set on the instance.
    """
    state = vars(serializer)
    return (
        isinstance(serializer, serializers.Serializer)
        and is_static(serializer)
        and _MATERIALIZED.isdisjoint(state)
        # ``default`` is ``fields.empty``, a class, on every instance.
        and not any(
            callable(value) and not isinstance(value, type) for value in state.values()
        )
    )


def has_async_representation(serializer):
    """
    Whether representing ``serializer`` reaches a coroutine function: an
    ``async def to_representation`` of a serializer or field, or an
    ``ato_representation`` / ``adata`` of the project's. Such a serializer is
    not represented by a synchronous plan. A static serializer is answered
    once per class, without building its fields again.
    """
    if not _classified_by_class(serializer):
        return _async_representation(serializer)
    cls = type(serializer)
    try:
        return _class_representation[cls]
    except KeyError:
        pass
    # Racing threads store the same value.
    return _class_representation.setdefault(cls, _async_representation(serializer))


def _async_representation(serializer):
    if isinstance(serializer, serializers.ListSerializer):
        return has_async_representation(serializer.child)
    if not isinstance(serializer, serializers.Serializer):
        return iscoroutinefunction(type(serializer).to_representation)
    if iscoroutinefunction(
        getattr(type(serializer), "to_representation", None)
    ) or user_defines(serializer, "ato_representation", "adata"):
        return True
    return any(
        has_async_representation(field)
        if isinstance(field, serializers.BaseSerializer)
        else iscoroutinefunction(type(field).to_representation)
        for field in serializer.fields.values()
    )
