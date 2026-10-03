"""Serializers that are a function of their class; coroutine hooks refused."""

import inspect
import weakref
from inspect import iscoroutinefunction

from django.core.exceptions import FieldDoesNotExist
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


# Attributes DRF sets on a serializer instance over a class attribute of the same
# name. Any other instance attribute that shadows one of the class may change
# what DRF does: a method assigned to the instance, fields materialized (and
# perhaps edited), a ``Meta`` or ``url_field_name`` of its own.
_DRF_SHADOWS = frozenset({"_creation_counter", "initial", "default_empty_html"})

# Declared serializers are deep-copied into every instance, which builds them
# again from their arguments; these hooks could make the copy differ.
_BUILD_HOOKS = (*_FIELD_HOOKS, "bind", "__deepcopy__", "__new__")


class _StaticClasses:
    """
    :func:`is_static` and the per-class answers it keeps, for one rule of
    which hooks are the project's (``user_defines``): fastdrf's, or that of a
    package built on it whose own bases count as framework code (aiodrf's).
    """

    def __init__(self, user_defines):
        self.user_defines = user_defines
        # class -> the names an instance must not shadow, or None when it is
        # not static
        self.classes = weakref.WeakKeyDictionary()

    def is_static(self, serializer):
        """
        Return True if the fields of ``serializer`` (the child of a list
        serializer), and those of the serializers nested in it, are a
        function of its class.

        That holds for an instance built with the usual arguments only that
        shadows nothing of its class (no fields materialized, no method
        assigned), of a class that builds its fields with DRF's code alone
        from declarations and a model, whose declared fields are DRF's,
        children included, and whose declared serializers are static too.
        The compiler, the input recognizer and the classification below keep
        per-class answers for these alone.
        """
        if isinstance(serializer, serializers.ListSerializer):
            serializer = serializer.child
        names = self.shadow_names(type(serializer))
        return (
            names is not None
            and _PLAIN_KWARGS.issuperset(serializer._kwargs)
            and names.isdisjoint(vars(serializer))
        )

    def shadow_names(self, cls):
        """
        The names an instance of ``cls`` must not set for its fields to be
        those of its class (:func:`is_static`), or None when they never are.
        """
        try:
            return self.classes[cls]
        except KeyError:
            pass
        meta = getattr(cls, "Meta", None)
        model = getattr(meta, "model", None)
        static = (
            issubclass(cls, serializers.Serializer)
            # A child may build fields from its parent's context, or change
            # them when bound.
            and not getattr(meta, "depth", 0)
            and not self.user_defines(cls, *_BUILD_HOOKS)
            # The project's code, run whenever a ModelSerializer builds its
            # fields.
            and not (
                issubclass(cls, serializers.ModelSerializer)
                and model is not None
                and _model_fields_call_code(model)
            )
            and all(
                self.static_declaration(field)
                for field in cls._declared_fields.values()
            )
        )
        names = frozenset(dir(cls)) - _DRF_SHADOWS if static else None
        # Racing threads store the same value.
        return self.classes.setdefault(cls, names)

    def static_declaration(self, field):
        if isinstance(field, serializers.ListSerializer):
            return not self.user_defines(
                field, *_BUILD_HOOKS
            ) and self.static_declaration(field.child)
        if isinstance(field, serializers.BaseSerializer):
            return self.shadow_names(type(field)) is not None
        # DRF's own fields; a custom one may bind differently per parent.
        # DRF's collections and to-many relations bind the child they were
        # declared with.
        return type(field) in _BUILTIN_FIELDS and all(
            self.static_declaration(child)
            for child in (
                getattr(field, "child", None),
                getattr(field, "child_relation", None),
            )
            if child is not None
        )


_statics = _StaticClasses(user_defines)
depends_on_classification(_statics.classes)
_static_classes = _statics.classes
is_static = _statics.is_static
_instance_shadow_names = _statics.shadow_names
_static_declaration = _statics.static_declaration


# Attributes DRF materializes on an instance when first read. Once any of
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
        # The list's own hooks first: they represent the children.
        return (
            iscoroutinefunction(type(serializer).to_representation)
            or user_defines(serializer, "ato_representation", "adata")
            or has_async_representation(serializer.child)
        )
    if not isinstance(serializer, serializers.Serializer):
        return iscoroutinefunction(type(serializer).to_representation)
    if iscoroutinefunction(
        getattr(type(serializer), "to_representation", None)
    ) or user_defines(serializer, "ato_representation", "adata"):
        return True
    model = getattr(getattr(serializer, "Meta", None), "model", None)
    return any(
        _async_field(serializer, field, model) for field in serializer.fields.values()
    )


def _async_field(serializer, field, model):
    if isinstance(field, serializers.BaseSerializer):
        return has_async_representation(field)
    if iscoroutinefunction(type(field).to_representation):
        return True
    if isinstance(field, fields.SerializerMethodField):
        # DRF calls ``get_<field>``: a coroutine function gives a coroutine.
        return _returns_coroutine(getattr(serializer, field.method_name, None))
    return _async_source(model, getattr(field, "source_attrs", ()))


def _returns_coroutine(function):
    """
    Whether calling ``function`` gives a coroutine: a coroutine function, or
    one behind wrappers that keep ``__wrapped__`` (``functools.wraps``).
    """
    if function is None:
        return False
    if iscoroutinefunction(function):
        return True
    try:
        return iscoroutinefunction(inspect.unwrap(function))
    except ValueError:  # a ``__wrapped__`` cycle
        return False


def _async_source(model, source_attrs):
    """Whether ``source_attrs`` read from ``model`` end at a coroutine function."""
    if model is None or not source_attrs:
        return False
    for name in source_attrs[:-1]:
        model = _related_model(model, name)
        if model is None:
            return False
    return _async_attribute(model, source_attrs[-1])


@class_cache
def _related_model(model, name):
    # The model a forward relation such as ``book`` in ``book.title`` leads to.
    try:
        field = model._meta.get_field(name)
    except FieldDoesNotExist:
        return None
    return field.related_model if field.many_to_one or field.one_to_one else None


@class_cache
def _async_attribute(model, name):
    try:
        attribute = inspect.getattr_static(model, name)
    except AttributeError:
        return False
    if isinstance(attribute, property):
        attribute = attribute.fget
    elif isinstance(attribute, (staticmethod, classmethod)):
        attribute = attribute.__func__
    return attribute is not None and _returns_coroutine(attribute)
