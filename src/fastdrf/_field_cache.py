"""Unbound field templates for declarative ModelSerializer subclasses."""

import copy

from django.core.exceptions import FieldDoesNotExist
from django.core.signals import setting_changed
from django.db.models.manager import BaseManager
from django.utils.functional import lazy
from rest_framework import serializers
from rest_framework.settings import api_settings
from rest_framework.utils.field_mapping import get_unique_error_message
from rest_framework.validators import UniqueValidator

from fastdrf._classify import _model_fields_call_code
from fastdrf._field_options import field_options
from fastdrf._inspection import _FIELD_HOOKS
from fastdrf.utils import class_cache, depends_on_classification, user_defines

_FIELD_STATE = frozenset(
    {
        "Meta",
        "_declared_fields",
        "url_field_name",
        "serializer_field_mapping",
        "serializer_related_field",
        "serializer_related_to_field",
        "serializer_url_field",
        "serializer_choice_field",
        *_FIELD_HOOKS,
    }
)


def _static_fields(serializer):
    """
    Whether DRF would build the same fields for every instance of the class.
    The constructor's arguments do not matter: DRF's ``__init__`` only stores
    them, and a class with an ``__init__`` of its own is not static.
    """
    return _static_class(type(serializer)) and _FIELD_STATE.isdisjoint(vars(serializer))


@depends_on_classification
@class_cache
def _static_class(cls):
    meta = getattr(cls, "Meta", None)
    model = getattr(meta, "model", None)
    return (
        not user_defines(cls, "__new__", *_FIELD_HOOKS)
        # ``Meta.depth`` builds a serializer class per nested relation.
        and not getattr(meta, "depth", 0)
        and (model is None or not _model_fields_call_code(model))
    )


@depends_on_classification
@class_cache
def _field_template(cls):
    # Built on an instance of its own: a static class's fields are the same
    # for every instance, and this one is never bound or handed out. DRF's
    # ``get_fields``: a static class does not override it, and the cached
    # ``get_fields`` of fastdrf's base (or of a package built on this cache)
    # is the caller.
    serializer = cls()
    template = serializers.ModelSerializer.get_fields(serializer)
    model = getattr(getattr(cls, "Meta", None), "model", None)
    if model is not None:
        extra_kwargs = serializer.get_extra_kwargs()
        # Validators DRF built from the model: not those of a declared field
        # or given in ``extra_kwargs``, which DRF keeps as the project wrote them.
        built = {
            name: field
            for name, field in template.items()
            if name not in cls._declared_fields
            and "validators" not in extra_kwargs.get(name, {})
        }
        _translate_unique_messages(model, built)
    return template


def _translate_unique_messages(model, fields):
    """
    DRF formats the message of a ``UniqueValidator`` it builds for a model
    field (``get_unique_error_message``) in the language active at the time.
    Every copy of the template shares its validators, as DRF shares declared
    ones: format the message again whenever it is read instead.
    """
    for name, field in fields.items():
        try:
            model_field = model._meta.get_field(field.source or name)
        except FieldDoesNotExist:
            continue
        message = get_unique_error_message(model_field)
        if message is None:
            continue
        translated = lazy(get_unique_error_message, str)(model_field)
        for validator in field._kwargs.get("validators", ()):
            # Only DRF's message: one given in ``extra_kwargs`` stays.
            if type(validator) is UniqueValidator and validator.message == message:
                validator.message = translated


@depends_on_classification
@class_cache
def _template_memo(cls):
    """
    ``copy.deepcopy`` memo entries that keep the managers DRF passed to the
    fields it built (``queryset=related_model._default_manager``) shared by
    every copy, as DRF's per-instance build shares them. Deep-copying one
    fails on state a manager may hold (a lock, a client). Declared fields are
    left to ``deepcopy``, as DRF copies them.
    """
    declared = cls._declared_fields
    memo = {}
    for name, field in _field_template(cls).items():
        if name not in declared:
            _collect_managers(field, memo)
    return memo


def _collect_managers(field, memo):
    for value in field._kwargs.values():
        if isinstance(value, BaseManager):
            memo[id(value)] = value
        elif hasattr(value, "_kwargs"):  # ``child_relation`` of a to-many field
            _collect_managers(value, memo)


@depends_on_classification
@class_cache
def _field_copy_plan(cls):
    from fastdrf._field_copy import plan_fields

    return plan_fields(_field_template(cls), shared=_template_memo(cls))


@depends_on_classification
@class_cache
def _compiled_field_copy_plan(cls):
    from fastdrf._field_copy import compile_fields

    return compile_fields(_field_template(cls), shared=_template_memo(cls))


@depends_on_classification
@class_cache
def _declared_copy_plan(cls):
    from fastdrf._field_copy import compile_fields

    return compile_fields(cls._declared_fields)


def _clear_field_templates(*, setting, **kwargs):
    # ``REST_FRAMEWORK["URL_FIELD_NAME"]`` names a field. Settings that fields
    # read when instantiated are read again by each copy.
    if setting in ("FASTDRF", "REST_FRAMEWORK"):
        _field_template.cache_clear()
        _template_memo.cache_clear()
        _field_copy_plan.cache_clear()
        _compiled_field_copy_plan.cache_clear()
        _declared_copy_plan.cache_clear()


setting_changed.connect(_clear_field_templates)


# ``get_fields`` of the serializer bases built on this cache (fastdrf's, or
# another package's): ``build`` is the next ``get_fields`` in the MRO, DRF's.


def _serializer_fields(serializer, build):
    """
    A serializer's fields: a copy plan of its declared fields with
    ``FIELD_COPY_MODE = "compiled"``, else ``build()``. Model serializers use
    their own, guarded template (:func:`_model_serializer_fields`).
    """
    if not isinstance(serializer, serializers.ModelSerializer):
        enabled, mode = field_options(serializer)
        if (
            enabled
            and mode == "compiled"
            and "_declared_fields" not in vars(serializer)
        ):
            return _declared_copy_plan(type(serializer))()
    return build()


def _model_serializer_fields(serializer, build):
    """
    A model serializer's fields from its class's template (built once) when
    field caching applies to it, else ``build()``.
    """
    enabled, mode = field_options(serializer)
    if not (enabled and _static_fields(serializer)):
        return build()
    if serializer.url_field_name is None:
        # As DRF's ``get_fields`` sets it.
        serializer.url_field_name = api_settings.URL_FIELD_NAME
    cls = type(serializer)
    if mode == "compiled":
        return _compiled_field_copy_plan(cls)()
    if mode == "clone":
        return _field_copy_plan(cls)()
    return copy.deepcopy(_field_template(cls), dict(_template_memo(cls)))
