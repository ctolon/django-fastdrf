"""
Compile DRF serializers into msgspec Structs, pydantic models or Python readers.

With ``FASTDRF["SERIALIZER_BACKEND"] = "msgspec"`` (or ``"pydantic"``, or
``"python"``, which needs neither), or ``Meta.serializer_backend`` on a
single serializer, django-fastdrf produces ``serializer.data`` with the
compiled class instead of DRF's per-field Python code. (Input is a separate
module with a narrower rule: :mod:`fastdrf.inputs`.)

A serializer is compiled as a whole or not at all. In the default
``"strict"`` parity mode only fields whose output is known to be identical
to DRF's are accepted:

* ``ModelSerializer`` fields backed by concrete model fields of matching
  types (strings, integers, floats, booleans, UUIDs, dates, times), and
  choices whose keys have the model field's type,
* datetimes and decimals output as strings, as DRF's ``to_representation``
  outputs them with the field's options: in the current time zone, quantized
  in the thread's decimal context, both read once per output,
* ``PrimaryKeyRelatedField`` and ``SlugRelatedField`` for forward foreign
  keys, and for many-to-many fields and reverse foreign keys (``many=True``),
* ``ModelField`` (what ``ModelSerializer`` builds for a ``GeneratedField``)
  and ``FileField``/``ImageField`` of the model's file fields,
* the column of a forward foreign key (``<fk>_id``), typed as its target field,
* a dotted source through foreign keys that cannot be null,
* a DRF scalar field reading a value of the instance alone (an annotation),
* fields, keys and model fields registered with :mod:`fastdrf.registry`,
* nested serializers for forward foreign keys, compiled recursively, and
  nested ``many=True`` serializers (DRF's own list serializer) for
  many-to-many fields and reverse foreign keys: ``manager.all()``, prefetched
  or queried, as DRF iterates it.

Every compiled field outputs ``None`` as ``null``, as DRF does, whatever the
model says. Anything else (``SerializerMethodField``, a ``many=True`` field on
anything but a related manager, other dotted sources, custom fields,
``to_representation`` or ``get_attribute`` overrides or methods assigned to
an instance, a list serializer with its own ``to_representation``, a nested
serializer whose model is not the related objects') keeps the serializer on
DRF, and so does JSON (DRF leaves values such as ``Decimal`` to its encoder).

The compiled class represents instances of the serializer's model, which
have every attribute it reads (:func:`declines_source`). DRF represents
anything else, such as a mapping or the dicts of ``QuerySet.values()``:
it skips a field whose key is missing, or outputs its default. In "strict"
parity DRF also represents an instance the compiled class fails to read
(:func:`unreadable_source`).
A serializer whose fields are a function of its class (:func:`is_static`)
is analyzed once per class, without building its fields for each instance;
any other is analyzed per :func:`signature` of the instance, so serializers
that change their fields per instance compile one variant per field set, up
to :data:`MAX_VARIANTS`.
``"fast"`` parity additionally accepts a ``DecimalField`` output as a
``Decimal`` or with methods of its own (rendered as ``str(value)`` rather
than quantized), plain ``Serializer`` fields without a model field, and JSON
fields. ``report_details()`` reports structural eligibility and the reason
when a serializer cannot be compiled.
"""

import contextvars
import datetime
import decimal
import inspect
import operator
import threading
import typing
import uuid
import weakref
import zoneinfo
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import GetSetDescriptorType
from typing import Any, cast

from django.conf import settings
from django.core.exceptions import (
    FieldDoesNotExist,
    ImproperlyConfigured,
    ObjectDoesNotExist,
)
from django.core.signals import setting_changed
from django.db import models
from django.db.models.fields.related_descriptors import (
    ForwardManyToOneDescriptor,
    ManyToManyDescriptor,
    ReverseManyToOneDescriptor,
)
from django.db.models.query_utils import DeferredAttribute
from django.utils import timezone
from django.utils.encoding import is_protected_type
from rest_framework import ISO_8601, fields, relations, serializers
from rest_framework.settings import api_settings

from fastdrf._classify import (
    _async_field,
    _returns_coroutine,
    has_async_representation,
    is_static,
)
from fastdrf._compiled import fields_from_class
from fastdrf.compat import BigIntegerField
from fastdrf.settings import fastdrf_settings
from fastdrf.signals import left_to_drf
from fastdrf.utils import (
    class_cache,
    definer,
    depends_on_classification,
    is_framework_class,
    user_defines,
)

__all__ = [
    "MAX_VARIANTS",
    "NotCompilable",
    "OutputField",
    "OutputSpec",
    "analyze",
    "compiled_for",
    "report",
    "report_details",
    "signature",
]


class UnreadableValue(ValueError):
    """
    A value the compiled output would not represent as DRF does (a string
    subclass for a ``CharField``, a value of another type through a relation):
    the backends raise their error for it, and DRF represents the source
    (:func:`unreadable_source`).
    """


class NotCompilable(Exception):
    def __init__(self, reason: str, *, code: str = "unsupported_field") -> None:
        super().__init__(reason)
        self.code = code


@dataclass(frozen=True, slots=True)
class Eligibility:
    """Structural eligibility of this instance, not a runtime fallback count."""

    code: str = "eligible"
    reason: str | None = None
    #: The fields its own field represents in the compiled output.
    delegated: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return self.reason is None


class OutputField:
    """
    One key of the output. ``type`` is an :class:`OutputSpec` for a nested
    serializer; ``many`` means a list of them, read from a related manager
    the way DRF's ``ListSerializer`` reads it (:func:`related_items`).
    ``convert``, when set, produces the output from a value that is not None
    (DRF's own ``to_representation``); the backend then only encodes it.
    """

    __slots__ = (
        "attribute",
        "awaits",
        "convert",
        "delegated",
        "key",
        "many",
        "nullable",
        "type",
    )

    def __init__(
        self,
        key: Any,
        attribute: str,
        type: Any,
        nullable: bool,
        many: bool = False,
        convert: Callable[[Any], Any] | None = None,
        delegated: bool = False,
        awaits: bool = False,
    ) -> None:
        self.key = key
        self.attribute = attribute
        self.type = type
        self.nullable = nullable
        self.many = many
        self.convert = convert
        # Represented by the serializer's own field (:func:`fill_delegated`),
        # with a coroutine an asynchronous caller awaits (``awaits``).
        self.delegated = delegated
        self.awaits = awaits


class OutputSpec:
    __slots__ = ("fields", "name")

    def __init__(self, name: str, fields: list[OutputField]) -> None:
        self.name = name
        self.fields = fields

    @property
    def delegated(self) -> tuple[str, ...]:
        """The delegated fields, those of nested serializers as ``key.field``."""
        names = []
        for field in self.fields:
            if field.delegated:
                names.append(field.key)
            elif isinstance(field.type, OutputSpec):
                names.extend(f"{field.key}.{name}" for name in field.type.delegated)
        return tuple(names)

    def delegation(self) -> "Delegation | None":
        """
        What :func:`fill_delegated` represents, or None. Nested ``many=True``
        serializers are analyzed without delegation, so only those of
        foreign keys have any.
        """
        entries: list[Any] = []
        for field in self.fields:
            if field.delegated:
                entries.append(field.key)
            elif (
                isinstance(field.type, OutputSpec)
                and (plan := field.type.delegation()) is not None
            ):
                entries.append((field.key, field.attribute, plan))
        if not entries:
            return None
        awaits = any(field.awaits for field in self.fields if field.delegated) or any(
            entry[2].awaits for entry in entries if not isinstance(entry, str)
        )
        return Delegation(tuple(entries), awaits)


@dataclass(frozen=True, slots=True)
class Delegation:
    """
    The delegated fields of a compiled output in DRF's field order: a field's
    key, or ``(key, attribute, Delegation)`` for a serializer nested on a
    foreign key that has delegated fields of its own.
    """

    entries: tuple[Any, ...]
    #: Whether a field gives a coroutine, which only an asynchronous caller
    #: awaits (``compiled_for(..., awaits=True)``).
    awaits: bool = False


_INTEGERS = {
    "AutoField",
    "BigAutoField",
    "SmallAutoField",
    "IntegerField",
    "BigIntegerField",
    "SmallIntegerField",
    "PositiveIntegerField",
    "PositiveBigIntegerField",
    "PositiveSmallIntegerField",
}
_STRINGS = {
    "CharField",
    "TextField",
    "SlugField",
    "EmailField",
    "URLField",
    "GenericIPAddressField",
    "IPAddressField",
    "FilePathField",
}

# DRF field class -> (python type, model field internal types it is exact for).
_SCALARS = (
    (fields.BooleanField, bool, {"BooleanField", "NullBooleanField"}),
    (fields.IntegerField, int, _INTEGERS),
    (fields.FloatField, float, {"FloatField"}),
    (fields.UUIDField, uuid.UUID, {"UUIDField"}),
    (fields.DateTimeField, datetime.datetime, {"DateTimeField"}),
    (fields.DateField, datetime.date, {"DateField"}),
    (fields.TimeField, datetime.time, {"TimeField"}),
    (fields.JSONField, typing.Any, {"JSONField"}),
    (fields.CharField, str, _STRINGS),
)
_BIG_INTEGER = BigIntegerField


@dataclass(frozen=True, slots=True)
class _Registration:
    """
    A field class registered with :mod:`fastdrf.registry`: the options its
    output depends on, which the variant :func:`signature` keeps, and the
    factory of its representation of a value that is not None (None to keep
    the serializer on DRF).
    """

    #: ``(attribute, key function or None)``.
    options: tuple[tuple[str, Callable[[Any], Any] | None], ...]
    representation: Callable[[Any], Callable[[Any], Any] | None]


# Filled by :mod:`fastdrf.registry`, at start-up, before serializers are
# compiled; a registration forgets what was compiled before it.
#: Serializer field class (the definer of its ``to_representation``) -> its
#: registration, for a model column's value.
_FIELD_REPRESENTATIONS: dict[type, _Registration] = {}
#: Primary key relation class -> its registration, for a related key.
_KEY_REPRESENTATIONS: dict[type, _Registration] = {}
#: Model field classes of other packages whose descriptor (None for
#: Django's) runs no code with effects at read time -> that descriptor class.
_DJANGO_READ_FIELDS: dict[type, type | None] = {}
_BY_INTERNAL_TYPE = {
    internal: python_type
    for _, python_type, internals in _SCALARS
    for internal in internals
}


#: Options of a field instance that :func:`analyze` depends on. They are read
#: through :func:`_option` only, so :func:`signature` cannot miss one.
_FIELD_ATTRS = (
    "format",
    "uuid_format",
    "binary",
    "timezone",
    "pk_field",
    "coerce_to_string",
    "max_digits",
    "decimal_places",
    "localize",
    "rounding",
    "normalize_output",
    "slug_field",
    "use_url",
    # A SerializerMethodField's method decides whether it can be delegated.
    "method_name",
)
_ABSENT = object()


def _option(field: Any, attr: str) -> Any:
    if attr not in _FIELD_ATTRS:
        raise ValueError(
            f"{attr!r} is not part of the signature; add it to _FIELD_ATTRS."
        )
    return getattr(field, attr, _ABSENT)


def analyze(
    serializer: serializers.BaseSerializer,
    parity: str = "strict",
    delegate: bool = False,
    awaits: bool = False,
) -> "OutputSpec":
    """
    Return the :class:`OutputSpec` of ``serializer`` or raise NotCompilable.
    With ``delegate``, a field the backend cannot represent is represented by
    its own code (:func:`fill_delegated`) instead; nested serializers are
    analyzed without, so a nested one that cannot be compiled is a field
    represented by its own code as a whole. With ``awaits`` a delegated
    field may give a coroutine, for an asynchronous caller.
    """
    cls = type(serializer)
    if _custom(cls, "to_representation"):
        raise NotCompilable(
            f"{cls.__qualname__} overrides to_representation()", code="custom_hook"
        )
    if awaits and user_defines(cls, "ato_representation", "adata"):
        raise NotCompilable(
            f"{cls.__qualname__} represents itself asynchronously", code="custom_hook"
        )
    meta = getattr(serializer, "Meta", None)
    model = getattr(meta, "model", None)
    if model is None and parity == "strict":
        raise NotCompilable(
            f"{cls.__qualname__} is not a ModelSerializer", code="model_required"
        )

    output = []
    for field in serializer._readable_fields:  # type: ignore[attr-defined]
        name = f"{cls.__qualname__}.{field.field_name}"
        try:
            output.append(_compiled_field(field, name, model, parity, delegate, awaits))
        except NotCompilable:
            if not delegate or model is None:
                raise
            # DRF's code for this field, in the compiled output.
            output.append(_delegated(field, name, model, awaits))
    if output and all(output_field.delegated for output_field in output):
        raise NotCompilable(
            f"{cls.__qualname__} has no field the backend represents",
            code="nothing_compiled",
        )
    return OutputSpec(f"{cls.__name__}Compiled", output)


def _compiled_field(
    field: Any, name: str, model: Any, parity: str, delegate: bool, awaits: bool
) -> OutputField:
    if field.source == "*":
        raise NotCompilable(
            f"{name} has source={field.source!r}", code="unsupported_source"
        )
    if _custom(type(field), "get_attribute"):
        raise NotCompilable(f"{name} overrides get_attribute()", code="custom_hook")
    output_field = _output_field(field, name, model, parity, delegate, awaits)
    if parity == "strict" and not _instance_only(model, output_field.attribute):
        _check_framework_read(model, output_field.attribute)
    return output_field


def _delegated(field: Any, name: str, model: Any, awaits: bool) -> OutputField:
    """
    A field the serializer's own field represents, after the compiled
    output: the compiled class holds its key, in DRF's order, and reads
    ``_state``, which every model instance has, in its place. A field that
    gives a coroutine is delegated for an asynchronous caller only; a nested
    serializer whose representation awaits cannot be (DRF's synchronous code
    would leave its coroutines unawaited).
    """
    if isinstance(field, serializers.BaseSerializer):
        if has_async_representation(field):
            raise NotCompilable(
                f"{name} is a serializer represented asynchronously",
                code="custom_hook",
            )
        waits = False
    else:
        if _returns_coroutine(field.get_attribute):
            raise NotCompilable(
                f"{name} reads its value with a coroutine", code="custom_hook"
            )
        waits = _async_field(field.parent, field, model)
    if waits and not awaits:
        hook = (
            getattr(field.parent, field.method_name, None)
            if isinstance(field, fields.SerializerMethodField)
            else field.to_representation
        )
        what = getattr(hook, "__qualname__", None) or "its source"
        raise NotCompilable(
            f"{name} is represented by a coroutine, {what}()", code="custom_hook"
        )
    return OutputField(
        field.field_name,
        "_state",
        typing.Any,
        True,
        convert=_held_in_place,
        delegated=True,
        awaits=waits,
    )


def _held_in_place(value: Any) -> None:
    return None


#: What :meth:`DelegatedStep.read` returns once the step is done (a skipped
#: field, or None as None).
DONE = object()


class DelegatedStep:
    """
    One delegated field of one item, as DRF's ``Serializer.to_representation``
    represents it: :meth:`read` its attribute, :meth:`represent` it, and
    :meth:`write` the result. A synchronous caller does it in a row
    (:func:`fill_delegated`); an asynchronous one awaits what is awaitable.
    """

    __slots__ = ("field", "instance", "row")

    def __init__(self, row: dict[str, Any], field: Any, instance: Any) -> None:
        self.row = row
        self.field = field
        self.instance = instance

    def read(self) -> Any:
        """The field's attribute, or :data:`DONE` (skipped, or None)."""
        try:
            attribute = self.field.get_attribute(self.instance)
        except fields.SkipField:
            del self.row[self.field.field_name]
            return DONE
        check = (
            attribute.pk if isinstance(attribute, relations.PKOnlyObject) else attribute
        )
        if check is None:
            self.row[self.field.field_name] = None
            return DONE
        return attribute

    def represent(self, attribute: Any) -> Any:
        if attribute is None:
            # An awaited attribute that was None: DRF's None check, after it.
            return None
        return self.field.to_representation(attribute)

    def write(self, value: Any) -> None:
        self.row[self.field.field_name] = value


def delegated_steps(
    serializer: serializers.BaseSerializer,
    delegation: "Delegation",
    items: list[Any],
    rows: list[dict[str, Any]],
) -> Iterator[DelegatedStep]:
    """
    The delegated fields of ``serializer`` (the child of a list) for
    ``items`` and their compiled ``rows``, item by item and in field order,
    a serializer nested on a foreign key at its place, for a related object
    that has a row. Each step is taken before the next is made.
    """
    live = _live_fields(serializer, delegation)
    for row, instance in zip(rows, items, strict=True):
        yield from _row_steps(live, row, instance)


def fill_delegated(
    serializer: serializers.BaseSerializer,
    delegation: "Delegation",
    items: list[Any],
    rows: list[dict[str, Any]],
) -> None:
    """Take every :func:`delegated_steps` step synchronously."""
    for step in delegated_steps(serializer, delegation, items, rows):
        attribute = step.read()
        if attribute is not DONE:
            step.write(step.represent(attribute))


def _live_fields(serializer: Any, delegation: "Delegation") -> list[Any]:
    fields_ = serializer.fields
    return [
        (fields_[entry],)
        if isinstance(entry, str)
        else (entry[0], entry[1], _live_fields(fields_[entry[0]], entry[2]))
        for entry in delegation.entries
    ]


def _row_steps(live: list[Any], row: dict[str, Any], instance: Any) -> Iterator[Any]:
    for entry in live:
        if len(entry) == 3:
            key, attribute, nested = entry
            nested_row = row.get(key)
            if nested_row is not None:
                yield from _row_steps(nested, nested_row, getattr(instance, attribute))
            continue
        yield DelegatedStep(row, entry[0], instance)


def _output_field(
    field: Any,
    name: str,
    model: Any,
    parity: str,
    delegate: bool = False,
    awaits: bool = False,
) -> OutputField:
    """The :class:`OutputField` of one readable field, or raise NotCompilable."""
    if len(field.source_attrs) > 1:
        return _through_relations(field, name, model, parity)
    attribute = field.source_attrs[0]
    model_field = _model_field(model, attribute)

    if isinstance(field, relations.ManyRelatedField):
        return _primary_keys(field, name, model, attribute, parity)
    if isinstance(field, serializers.ListSerializer):
        if _custom(type(field), "to_representation"):
            raise NotCompilable(
                f"{name} uses {type(field).__qualname__}, which overrides to_representation()",
                code="custom_list",
            )
        related_model = _related_manager_model(model, attribute)
        if related_model is None:
            raise NotCompilable(
                f"{name} is not a to-many relation of {model.__name__ if model else None}",
                code="unsupported_relation",
            )
        child = cast(serializers.BaseSerializer, field.child)
        _check_related_model(name, child, related_model)
        nested = analyze(child, parity)
        return OutputField(field.field_name, attribute, nested, True, many=True)
    if isinstance(field, serializers.BaseSerializer):
        if model_field is None or not (
            model_field.many_to_one or model_field.one_to_one
        ):
            raise NotCompilable(f"{name} is not a forward foreign key")
        _check_related_model(name, field, model_field.related_model)
        # The related object is read once and kept by Django: its delegated
        # fields read it again without a query (a related manager would).
        nested = analyze(field, parity, _delegates(field, delegate), awaits)
        return OutputField(field.field_name, attribute, nested, True)
    if isinstance(field, fields.ModelField):
        return _model_field_output(field, name, model_field, parity)
    if isinstance(field, fields.FileField):
        return _file(field, name, model_field)
    if isinstance(field, relations.SlugRelatedField):
        return _slug(field, name, model_field, parity)
    if isinstance(field, relations.RelatedField):
        return _primary_key(field, name, model, model_field, parity)
    if model_field is None and _instance_only(model, attribute):
        return _instance_value(field, name, attribute)
    registration = _FIELD_REPRESENTATIONS.get(definer(type(field), "to_representation"))
    if registration is not None:
        if model_field is None or model_field.is_relation:
            raise NotCompilable(f"{name} is not a column of the model")
        convert = _registered_representation(registration, field, name)
        return OutputField(
            field.field_name, attribute, typing.Any, True, convert=convert
        )
    if _holds_objects(model, _column_field(model_field, attribute), attribute):
        # A package's model field (a phone number, a country) may give its
        # own objects, which DRF's field converts like an instance's value.
        return _instance_value(field, name, attribute)

    return _scalar(field, name, model_field, attribute, parity)


def _holds_objects(model: Any, column: Any, attribute: str) -> bool:
    """
    Whether the column of ``attribute`` may hold objects of its model field's
    own, loaded or not: the field converts what the database returns, or
    its descriptor is not Django's.
    """
    if column is None or is_framework_class(type(column)):
        return False
    if user_defines(type(column), "from_db_value"):
        return True
    descriptor = inspect.getattr_static(model, attribute, None)
    return not (
        isinstance(descriptor, DeferredAttribute)
        and is_framework_class(type(descriptor))
    )


def _registered_representation(
    registration: _Registration, field: Any, name: str
) -> Callable[[Any], Any]:
    convert = registration.representation(field)
    if convert is None:
        raise NotCompilable(
            f"{name} is a {type(field).__name__} its registration declines"
        )
    return convert


def _scalar(
    field: Any, name: str, model_field: Any, attribute: str, parity: str
) -> OutputField:
    python_type = _scalar_type(
        field, name, _column_field(model_field, attribute), parity
    )
    convert = None
    if python_type is datetime.datetime:
        python_type, convert = typing.Any, _datetime_representation(field)
    elif python_type is decimal.Decimal and _drf_decimal_string(field):
        python_type, convert = typing.Any, _decimal_representation(field)
    elif parity == "strict" and python_type in _TEXT_REPRESENTATIONS:
        python_type, convert = typing.Any, _TEXT_REPRESENTATIONS[python_type]
    elif python_type is str and isinstance(field, fields.CharField):
        # ``str(value)``: a subclass (an enum member) may render otherwise
        # than the value the backends would output.
        python_type, convert = typing.Any, _exact_string
    elif (
        parity == "strict"
        and python_type is int
        and type(field) is fields.ReadOnlyField
    ):
        # DRF outputs the value itself: a subclass of int (an enum member)
        # stays one in ``.data``, which the backends would make an int.
        python_type, convert = typing.Any, _exact_integer
    # DRF represents None as None whatever the field: an unsaved instance's
    # id, a value the project set.
    return OutputField(field.field_name, attribute, python_type, True, convert=convert)


def _instance_only(model: Any, attribute: str) -> bool:
    """
    Whether ``attribute`` of an instance of ``model`` can only be the
    instance's own value (an annotation, a value the view set): the class
    has no such attribute, so reading it runs no code.
    """
    return (
        model is not None
        and inspect.getattr_static(model, attribute, _ABSENT) is _ABSENT
        and not user_defines(model, "__getattribute__", "__getattr__")
    )


def _instance_value(field: Any, name: str, attribute: str) -> OutputField:
    """
    A DRF scalar field reading an instance's own value, of whatever type: the
    value (or what a method stored there returns, as DRF's ``get_attribute``
    calls it) with the field's DRF representation. When the instance has no
    such value DRF skips the field; the compiled class cannot, and DRF
    represents the instance (:func:`unreadable_source`).
    """
    field_class = definer(type(field), "to_representation")
    if isinstance(field, fields.DecimalField):
        if not _drf_decimal_string(field):
            raise NotCompilable(f"{name} is a DecimalField output as a Decimal")
        represent = _decimal_representation(field)
    else:
        python_type = next(
            (
                python_type
                for drf_class, python_type, _ in _SCALARS
                if isinstance(field, drf_class)
                and field_class is definer(drf_class, "to_representation")
                and drf_class not in _STRICT_DECLINES
            ),
            None,
        )
        if python_type is None or (
            _BIG_INTEGER is not None and field_class is _BIG_INTEGER
        ):
            raise NotCompilable(f"{name} is a {type(field).__name__}")
        _check_format(field, name, python_type)
        represent = (
            _datetime_representation(field)
            if python_type is datetime.datetime
            else _REPRESENTATIONS[python_type]
        )

    def read(value: Any) -> Any:
        if fields.is_simple_callable(value):
            try:
                value = value()
            except (AttributeError, KeyError) as exc:
                # DRF's ``get_attribute``, which does not mask them.
                raise ValueError(
                    f'Exception raised in callable attribute "{attribute}"; '
                    f"original exception was: {exc}"
                ) from exc
        return None if value is None else represent(value)

    return OutputField(field.field_name, attribute, typing.Any, True, convert=read)


def _model_field_output(
    field: Any, name: str, model_field: Any, parity: str
) -> OutputField:
    """
    DRF's ``ModelField`` (what ``ModelSerializer`` builds for a field it has
    no class for, such as a ``GeneratedField``): the value when it is of a
    type Django protects (numbers, dates, None), else ``str(value)``, which
    is what ``Field.value_to_string`` returns.
    """
    if definer(type(field), "to_representation") is not fields.ModelField:
        raise NotCompilable(f"{name} is a {type(field).__name__}")
    own = field.model_field
    if (
        model_field is not own
        or own.is_relation
        or own.attname != field.source_attrs[0]
        or any(
            definer(type(own), method) is not models.Field
            for method in ("value_from_object", "value_to_string")
        )
    ):
        raise NotCompilable(f"{name} is a ModelField of {type(own).__name__}")
    python_type = _BY_INTERNAL_TYPE.get(own.get_internal_type())
    if python_type is None or (
        parity == "strict" and python_type not in (*_RAW_VALUE_TYPES, float)
    ):
        # A Decimal or a date stays an object in DRF's ``.data``.
        raise NotCompilable(f"{name} is a ModelField of a {own.get_internal_type()}")
    return OutputField(
        field.field_name,
        field.source_attrs[0],
        typing.Any,
        True,
        convert=_model_field_value,
    )


def _file(field: Any, name: str, model_field: Any) -> OutputField:
    """
    DRF's ``FileField`` (and ``ImageField``) of a model's file field: the
    file's URL, absolute with the context's request, or its name.
    """
    if definer(type(field), "to_representation") is not fields.FileField:
        raise NotCompilable(f"{name} is a {type(field).__name__}")
    if model_field is None or not isinstance(model_field, models.FileField):
        raise NotCompilable(f"{name} is not a file field of the model")
    use_url = _option(field, "use_url")
    if use_url is _ABSENT:
        use_url = api_settings.UPLOADED_FILES_USE_URL

    def represent(value: Any) -> Any:
        if not value:
            return None
        if not use_url:
            return value.name
        try:
            url = value.url
        except AttributeError:
            return None
        request = _call().serializer_context.get("request")
        return url if request is None else request.build_absolute_uri(url)

    return OutputField(
        field.field_name, field.source_attrs[0], typing.Any, True, convert=represent
    )


def _model_field_value(value: Any) -> Any:
    return value if is_protected_type(value) else str(value)


def _unchanged_raw(value: Any) -> Any:
    # A ``ReadOnlyField``'s value, as DRF outputs it, when the backends
    # render it as DRF's encoder does; anything else is DRF's.
    if isinstance(value, (str, int)):
        return value
    raise UnreadableValue(f"{value!r} is not a string or an integer")


def _exact_integer(value: Any) -> int:
    if type(value) is int:
        return value
    raise UnreadableValue(f"{value!r} is not an int")


def _exact_string(value: Any) -> str:
    if type(value) is str:
        return value
    raise UnreadableValue(f"{value!r} is not a str")


def _column_field(model_field: Any, attribute: str) -> Any:
    if (
        model_field is not None
        and model_field.is_relation
        and attribute == model_field.attname
    ):
        # The column of a foreign key (``<fk>_id``) holds a value of its
        # target field, which Django converts with that field's code.
        return model_field.target_field
    return model_field


def _primary_keys(
    field: Any, name: str, model: Any, attribute: str, parity: str
) -> OutputField:
    """
    DRF's ``ManyRelatedField`` of primary keys or slugs: ``[item.pk for item
    in manager.all()]`` (or the slug column), prefetched or queried, and
    ``[]`` for an unsaved instance, whose related manager the compiled class
    cannot read (DRF represents it then, see :func:`unreadable_source`).
    """
    child = field.child_relation
    if _custom(type(field), "to_representation"):
        raise NotCompilable(f"{name} is a {type(field).__name__}")
    related_model = _related_manager_model(model, attribute)
    if related_model is None:
        raise NotCompilable(
            f"{name} is not a to-many relation of {model.__name__ if model else None}",
            code="unsupported_relation",
        )
    if isinstance(child, relations.SlugRelatedField):
        slug = _slug_column(child, name, related_model, parity)
        return OutputField(
            field.field_name, attribute, typing.Any, True, convert=_slug_list(slug)
        )
    convert = _key_representation(child, name)
    if definer(related_model, "pk") is not models.Model:
        raise NotCompilable(
            f"{name} reads {related_model.__name__}.pk, which the model overrides",
            code="custom_hook",
        )
    if convert is not None:
        return OutputField(
            field.field_name, attribute, typing.Any, True, convert=_key_list(convert)
        )
    target = _column_field(related_model._meta.pk, related_model._meta.pk.attname)
    _check_plain_key(name, target)
    python_type = _BY_INTERNAL_TYPE.get(target.get_internal_type())
    if python_type is None:
        raise NotCompilable(f"{name} points to a {target.get_internal_type()} key")
    _check_raw_value(name, python_type, parity)
    return OutputField(
        field.field_name, attribute, typing.Any, True, convert=_primary_key_list
    )


def _unsaved(manager: Any) -> bool:
    # DRF's ``ManyRelatedField.get_attribute``: no relations before the
    # instance is saved. The manager's instance is the one represented.
    instance = getattr(manager, "instance", None)
    return instance is not None and instance.pk is None


def _primary_key_list(manager: Any) -> list[Any]:
    if _unsaved(manager):
        return []
    return [item.pk for item in related_items(manager)]


def _key_list(convert: Callable[[Any], Any]) -> Callable[[Any], list[Any]]:
    def keys(manager: Any) -> list[Any]:
        if _unsaved(manager):
            return []
        return [convert(item.pk) for item in related_items(manager)]

    return keys


def _slug_list(slug: str) -> Callable[[Any], list[Any]]:
    def slugs(manager: Any) -> list[Any]:
        if _unsaved(manager):
            return []
        return [getattr(item, slug) for item in related_items(manager)]

    return slugs


def _slug(field: Any, name: str, model_field: Any, parity: str) -> OutputField:
    """
    DRF's ``SlugRelatedField`` on a forward foreign key: the slug column of
    the related object, unchanged, and None without one.
    """
    if model_field is None or not (model_field.many_to_one or model_field.one_to_one):
        raise NotCompilable(f"{name} is not a forward foreign key")
    slug = _slug_column(field, name, model_field.related_model, parity)
    return OutputField(
        field.field_name,
        model_field.name,
        typing.Any,
        True,
        convert=operator.attrgetter(slug),
    )


def _slug_column(field: Any, name: str, related_model: Any, parity: str) -> str:
    """The column a slug field of DRF's own outputs unchanged, or raise."""
    if definer(type(field), "to_representation") is not relations.SlugRelatedField:
        raise NotCompilable(f"{name} is a {type(field).__name__}")
    slug = _option(field, "slug_field")
    model_field = _model_field(related_model, slug)
    if model_field is None or model_field.is_relation:
        raise NotCompilable(
            f"{name} outputs {related_model.__name__}.{slug}, which is not a column",
            code="unsupported_source",
        )
    _check_plain_key(name, model_field)
    python_type = _BY_INTERNAL_TYPE.get(model_field.get_internal_type())
    if python_type is None:
        raise NotCompilable(f"{name} outputs a {model_field.get_internal_type()}")
    _check_raw_value(name, python_type, parity)
    if parity == "strict":
        _check_framework_read(related_model, slug)
    return slug


def _check_plain_key(name: str, model_field: Any) -> None:
    """
    Raise NotCompilable for a key or slug field that converts what the
    database returns: its values may be objects of its own (a ``str``
    subclass), which DRF outputs unchanged and the backends cannot.
    """
    if not is_framework_class(type(model_field)) and user_defines(
        type(model_field), "from_db_value"
    ):
        raise NotCompilable(
            f"{name} reads a {type(model_field).__name__}, which converts its values"
        )


def _through_relations(field: Any, name: str, model: Any, parity: str) -> OutputField:
    """
    A scalar field whose dotted source follows foreign keys that cannot be
    null to a column of the last related model.

    The first relation is read as the instance's attribute, the others as
    DRF's ``get_attribute`` reads them: a missing related row gives None.
    A null foreign key would make DRF skip the key or output the field's
    default, which a compiled class cannot, so those stay on DRF.
    """
    if isinstance(
        field,
        (
            serializers.BaseSerializer,
            relations.RelatedField,
            relations.ManyRelatedField,
        ),
    ):
        raise NotCompilable(
            f"{name} has source={field.source!r}", code="unsupported_source"
        )
    if isinstance(field, fields.ChoiceField):
        # DRF maps the value through the field's choices, which the variant
        # signature does not keep.
        raise NotCompilable(
            f"{name} is a ChoiceField with source={field.source!r}",
            code="unsupported_source",
        )
    *path, attribute = field.source_attrs
    current = model
    for step in path:
        relation = _model_field(current, step)
        if (
            relation is None
            or not (relation.many_to_one or relation.one_to_one)
            or step != relation.name
        ):
            raise NotCompilable(
                f"{name}: {step} is not a foreign key of "
                f"{current.__name__ if current else None}",
                code="unsupported_source",
            )
        if relation.null:
            raise NotCompilable(
                f"{name}: {current.__name__}.{step} can be null",
                code="unsupported_source",
            )
        if parity == "strict":
            _check_framework_read(current, step)
        current = relation.related_model
    model_field = _model_field(current, attribute)
    python_type = _scalar_type(
        field, name, _column_field(model_field, attribute), parity
    )
    if model_field is None:
        raise NotCompilable(f"{name} has no model field")
    if parity == "strict":
        _check_framework_read(current, attribute)
    if isinstance(field, fields.ReadOnlyField):
        # Read through a relation, the value cannot be given to DRF to
        # render: in either parity, only values the backends render as DRF.
        _check_raw_value(name, python_type, "strict")
        represent: Callable[[Any], Any] = _unchanged_raw
    elif python_type is datetime.datetime:
        represent = _datetime_representation(field)
    elif python_type is decimal.Decimal and _drf_decimal_string(field):
        represent = _decimal_representation(field)
    else:
        try:
            represent = _REPRESENTATIONS[python_type]
        except KeyError:
            raise NotCompilable(f"{name} reads a {python_type}") from None
    rest = (*path[1:], attribute)

    def through(value: Any) -> Any:
        for step in rest:
            if value is None:
                # DRF's get_attribute fails on it; its field decides.
                raise UnreadableValue(f"{name} reads {step} of None")
            try:
                value = getattr(value, step)
            except ObjectDoesNotExist:
                return None
        return None if value is None else represent(value)

    return OutputField(field.field_name, path[0], typing.Any, True, convert=through)


_RELATION_DESCRIPTORS = (ForwardManyToOneDescriptor, ReverseManyToOneDescriptor)


def _check_framework_read(model: Any, attribute: str) -> None:
    """
    Raise NotCompilable unless reading ``attribute`` of an instance of
    ``model`` runs Django's code only: Django's descriptor of a field class
    of Django's, and for a relation read lazily, a manager whose
    ``get_queryset()`` and ``all()`` are Django's. In "strict" parity DRF may
    read the source again when the compiled class could not
    (:func:`unreadable_source`), which repeats Django's reads only.
    """
    where = f"{model.__name__}.{attribute}"
    descriptor = inspect.getattr_static(model, attribute, None)
    if _registered_read(model, attribute, descriptor):
        return
    if not (
        isinstance(descriptor, (DeferredAttribute, *_RELATION_DESCRIPTORS))
        and is_framework_class(type(descriptor))
    ):
        raise NotCompilable(
            f"{where} is read by {type(descriptor).__name__}", code="custom_hook"
        )
    field: Any = descriptor.field
    if not (
        is_framework_class(type(field))
        or _DJANGO_READ_FIELDS.get(type(field), 0) is None
    ):
        raise NotCompilable(f"{where} is a {type(field).__name__}", code="custom_hook")
    if isinstance(descriptor, ForwardManyToOneDescriptor):
        # What ``get_queryset()`` of the descriptor reads a missing object with.
        manager = field.remote_field.model._base_manager
    elif isinstance(descriptor, ReverseManyToOneDescriptor):
        # The related manager is a subclass of the default manager's class.
        manager = _related_manager_model(model, attribute)._default_manager
    else:
        return
    if user_defines(manager, "get_queryset", "all"):
        raise NotCompilable(
            f"{where} is read through {type(manager).__name__}", code="custom_hook"
        )


def _registered_read(model: Any, attribute: str, descriptor: Any) -> bool:
    # The descriptor a registered model field installs on its own name.
    field = getattr(descriptor, "field", None)
    registered = _DJANGO_READ_FIELDS.get(type(field))
    return (
        registered is not None
        and type(descriptor) is registered
        and attribute in (field.name, field.attname)
        and issubclass(model, field.model)
    )


def _custom(cls: type, name: str) -> bool:
    # Defined by a class that is neither DRF's nor fastdrf's.
    return user_defines(cls, name)


def _model_field(model: Any, attribute: str) -> Any:
    if model is None:
        return None
    try:
        field = model._meta.get_field(attribute)
    except FieldDoesNotExist:
        return None
    return field if field.concrete else None


def _related_manager_model(model: Any, attribute: str) -> Any:
    """
    The model of the objects of ``attribute`` if it is a related manager on
    every instance of ``model``, else None: Django's descriptor of a
    many-to-many field (either side) or of a reverse foreign key. It is a
    data descriptor, so an instance cannot hide it, and reading it cannot
    fail the way a property can (DRF skips a field whose attribute raises
    AttributeError; the compiled class cannot).
    """
    if model is None:
        return None
    try:
        descriptor = inspect.getattr_static(model, attribute)
    except AttributeError:
        return None
    if type(descriptor) is ManyToManyDescriptor:
        return (
            descriptor.rel.related_model if descriptor.reverse else descriptor.rel.model
        )
    if type(descriptor) is ReverseManyToOneDescriptor:
        return descriptor.rel.related_model
    return None


def _check_related_model(name: str, serializer: Any, related_model: Any) -> None:
    # The related objects have every field of the nested serializer's model
    # only when they are instances of it; DRF skips what they lack.
    model = getattr(getattr(serializer, "Meta", None), "model", None)
    if model is not None and not issubclass(related_model, model):
        raise NotCompilable(
            f"{name} represents {related_model.__name__} objects with a serializer of "
            f"{model.__name__}",
            code="unsupported_relation",
        )


def related_items(value: Any) -> Any:
    """What DRF's ``ListSerializer.to_representation`` iterates for ``value``."""
    return value.all() if isinstance(value, models.manager.BaseManager) else value


def _key_representation(field: Any, name: str) -> Callable[[Any], Any] | None:
    """
    None when a primary key relation outputs the key unchanged (DRF's own,
    without ``pk_field``), else the representation of a key registered for
    its class or its ``pk_field``'s (:data:`_FIELD_REPRESENTATIONS`,
    :data:`_KEY_REPRESENTATIONS`); raise NotCompilable for any other.
    """
    own = definer(type(field), "to_representation")
    pk_field = _option(field, "pk_field")
    if own is relations.PrimaryKeyRelatedField:
        if pk_field is None:
            return None
        target = pk_field
        registration = _FIELD_REPRESENTATIONS.get(
            definer(type(pk_field), "to_representation")
        )
    else:
        target = field
        registration = None if pk_field is not None else _KEY_REPRESENTATIONS.get(own)
    if registration is None:
        raise NotCompilable(f"{name} is a {type(field).__name__}")
    return _registered_representation(registration, target, name)


def _primary_key(
    field: Any, name: str, model: Any, model_field: Any, parity: str
) -> OutputField:
    convert = _key_representation(field, name)
    if model_field is None or not (model_field.many_to_one or model_field.one_to_one):
        raise NotCompilable(f"{name} is not a forward foreign key")
    if definer(model, "serializable_value") is not models.Model:
        # DRF reads the key with ``serializable_value()``, not the column.
        raise NotCompilable(
            f"{name} is read by {model.__name__}.serializable_value()",
            code="custom_hook",
        )
    if convert is not None:
        return OutputField(
            field.field_name, model_field.attname, typing.Any, True, convert=convert
        )
    target = model_field.target_field
    _check_plain_key(name, target)
    python_type = _BY_INTERNAL_TYPE.get(target.get_internal_type())
    if python_type is None:
        raise NotCompilable(f"{name} points to a {target.get_internal_type()} key")
    _check_raw_value(name, python_type, parity)
    return OutputField(field.field_name, model_field.attname, python_type, True)


def _scalar_type(field: Any, name: str, model_field: Any, parity: str) -> Any:
    field_class = definer(type(field), "to_representation")
    if field_class.__module__ != "rest_framework.fields":
        raise NotCompilable(f"{name} uses {type(field).__name__}.to_representation()")

    if isinstance(field, fields.DecimalField):
        if parity == "strict":
            _check_decimal(field, name)
        return decimal.Decimal
    if isinstance(field, fields.ChoiceField):
        if isinstance(field, fields.MultipleChoiceField):
            raise NotCompilable(f"{name} is a MultipleChoiceField")
        if model_field is None:
            if parity == "strict":
                raise NotCompilable(f"{name} has no model field")
            return typing.Any
        python_type = _from_model_field(name, model_field, parity)
        # DRF maps the value to the choice key of the same string (1 for
        # "1"); the model field's type is the output's only when they agree.
        if any(type(key) is not python_type for key in field.choices):
            raise NotCompilable(
                f"{name} has choices of another type than its model field"
            )
        return python_type
    if _BIG_INTEGER is not None and field_class is _BIG_INTEGER:
        # What ModelSerializer builds for BigAutoField and BigIntegerField:
        # IntegerField's output unless it is coerced to a string.
        if _option(field, "coerce_to_string") in (_ABSENT, None):
            coerced = api_settings.COERCE_BIGINT_TO_STRING
        else:
            coerced = _option(field, "coerce_to_string")
        if coerced:
            raise NotCompilable(f"{name} is a BigIntegerField coerced to a string")
        if model_field is None:
            if parity == "strict":
                raise NotCompilable(f"{name} has no model field")
        elif model_field.get_internal_type() not in _INTEGERS:
            raise NotCompilable(
                f"{name} is a BigIntegerField on a {model_field.get_internal_type()}"
            )
        return int
    if isinstance(field, fields.ReadOnlyField):
        if model_field is None:
            raise NotCompilable(f"{name} reads a non-field attribute")
        python_type = _from_model_field(name, model_field, parity)
        _check_raw_value(name, python_type, parity)
        return python_type

    for drf_class, python_type, internals in _SCALARS:
        if not isinstance(field, drf_class):
            continue
        if field_class is not definer(drf_class, "to_representation"):
            raise NotCompilable(f"{name} is a {type(field).__name__}")
        if model_field is None:
            if parity == "strict":
                raise NotCompilable(f"{name} has no model field")
        elif model_field.get_internal_type() not in internals:
            raise NotCompilable(
                f"{name} is a {drf_class.__name__} on a {model_field.get_internal_type()}"
            )
        if parity == "strict" and drf_class in _STRICT_DECLINES:
            raise NotCompilable(
                f"{name} is a {drf_class.__name__} ({_STRICT_DECLINES[drf_class]})"
            )
        _check_format(field, name, python_type)
        return python_type
    raise NotCompilable(f"{name} is a {type(field).__name__}")


# Identical to DRF for what the database returns, not for every value a
# project may put on an instance, so "strict" leaves them to DRF.
_STRICT_DECLINES = {
    fields.JSONField: "DRF leaves non-JSON values (Decimal, tuple) to its encoder",
}


#: What a field whose value DRF outputs unchanged (``ReadOnlyField``,
#: ``PrimaryKeyRelatedField``) can be compiled as in "strict" parity: types
#: that are their own JSON form. A UUID or a date stays a Python object in
#: DRF's ``.data``, and an int in a FloatField stays an int; the backends
#: would output a string or a float.
_RAW_VALUE_TYPES = (str, int, bool)


def _check_raw_value(name: str, python_type: Any, parity: str) -> None:
    if parity == "strict" and python_type not in _RAW_VALUE_TYPES:
        raise NotCompilable(
            f"{name} outputs a {python_type.__name__} unchanged, which DRF's encoder renders"
        )


def _from_model_field(name: str, model_field: Any, parity: str) -> Any:
    python_type = _BY_INTERNAL_TYPE.get(model_field.get_internal_type())
    if python_type is None or python_type is datetime.datetime:
        raise NotCompilable(f"{name} reads a {model_field.get_internal_type()}")
    if python_type is typing.Any and parity == "strict":
        raise NotCompilable(
            f"{name} reads a JSONField ({_STRICT_DECLINES[fields.JSONField]})"
        )
    return python_type


def _check_format(field: Any, name: str, python_type: Any) -> None:
    if python_type is typing.Any and _option(field, "binary") is True:
        raise NotCompilable(f"{name} renders JSON as bytes")
    if python_type is uuid.UUID and _option(field, "uuid_format") != "hex_verbose":
        raise NotCompilable(f"{name} renders UUIDs as {field.uuid_format}")
    setting = {
        datetime.datetime: "DATETIME_FORMAT",
        datetime.date: "DATE_FORMAT",
        datetime.time: "TIME_FORMAT",
    }.get(python_type)
    if setting is None:
        return
    output_format = _option(field, "format")
    if output_format is _ABSENT:
        output_format = getattr(api_settings, setting)
    if output_format is None or output_format.lower() != ISO_8601:
        raise NotCompilable(f"{name} is not rendered as ISO 8601")
    if python_type is datetime.datetime and (
        _option(field, "timezone") is not _ABSENT
        or definer(type(field), "enforce_timezone") is not fields.DateTimeField
        or definer(type(field), "default_timezone") is not fields.DateTimeField
    ):
        # A time zone of the field's own is an object the signature does not
        # tell apart; the others are code of the project's.
        raise NotCompilable(f"{name} has a time zone of its own")


#: DRF's own output for the text forms of UUIDs, dates and times in "strict"
#: parity (hex_verbose and ISO 8601, see :func:`_check_format`). A value that
#: is still the text it was set to (``Model(code="...")``) comes out as DRF
#: outputs it, unchanged, where the backends would parse and format it again.
_TEXT_REPRESENTATIONS: dict[Any, Callable[[Any], Any]] = {
    uuid.UUID: str,
    datetime.date: fields.DateField(format=ISO_8601).to_representation,
    datetime.time: fields.TimeField(format=ISO_8601).to_representation,
}


#: DRF's ``to_representation`` of the scalar fields :func:`_scalar_type`
#: accepts, for a value read through relations (:func:`_through_relations`),
#: which the backend receives already converted.
_REPRESENTATIONS: dict[Any, Any] = {
    str: str,
    int: int,
    float: float,
    bool: fields.BooleanField().to_representation,
    uuid.UUID: str,
    datetime.date: fields.DateField(format=ISO_8601).to_representation,
    datetime.time: fields.TimeField(format=ISO_8601).to_representation,
    decimal.Decimal: str,
    typing.Any: lambda value: value,
}


def _check_decimal(field: Any, name: str) -> None:
    """
    Raise NotCompilable unless DRF's own ``DecimalField`` code represents
    ``field`` as a string, which :func:`_decimal_representation` repeats.
    A ``Decimal`` in ``.data`` is left to the renderer's encoder by DRF.
    """
    for method in ("to_representation", "quantize"):
        if definer(type(field), method) is not fields.DecimalField:
            raise NotCompilable(f"{name} overrides {method}()", code="custom_hook")
    if not _drf_decimal_string(field):
        raise NotCompilable(f"{name} is a DecimalField output as a Decimal")


def _drf_decimal_string(field: Any) -> bool:
    """Whether DRF's own code outputs ``field``'s values as strings."""
    if any(
        definer(type(field), method) is not fields.DecimalField
        for method in ("to_representation", "quantize")
    ):
        return False
    coerced = _option(field, "coerce_to_string")
    if coerced is _ABSENT:
        coerced = api_settings.COERCE_DECIMAL_TO_STRING
    return bool(coerced or _option(field, "localize") is True)


class _Call:
    """
    What DRF's ``DateTimeField`` and ``DecimalField`` read for every value,
    read once per compiled output: the current time zone (None without
    ``USE_TZ``) and the thread's decimal context with a field's precision
    and rounding. Nothing a representation runs changes them meanwhile.
    """

    __slots__ = ("_contexts", "_zone", "serializer_context")

    def __init__(self, serializer_context: Any = None) -> None:
        self._zone: Any = _ABSENT
        self._contexts: dict[tuple[Any, Any], decimal.Context] = {}
        # The root serializer's context, which DRF's fields read.
        self.serializer_context = serializer_context or {}

    def zone(self) -> Any:
        if self._zone is _ABSENT:
            self._zone = timezone.get_current_timezone() if settings.USE_TZ else None
        return self._zone

    def context(self, digits: Any, rounding: Any) -> decimal.Context:
        # What DRF's ``DecimalField.quantize`` builds for each value.
        try:
            return self._contexts[digits, rounding]
        except KeyError:
            context = decimal.getcontext().copy()
            if digits is not None:
                context.prec = digits
            if rounding is not None:
                context.rounding = rounding
            return self._contexts.setdefault((digits, rounding), context)


_CALL: contextvars.ContextVar[_Call] = contextvars.ContextVar("fastdrf_compiled_call")


def _call() -> _Call:
    # Outside a compiled output (a backend used directly), per value.
    return _CALL.get(None) or _Call()


def _in_call(dump: Callable[[Any], Any]) -> Callable[..., Any]:
    def run(source: Any, serializer_context: Any = None) -> Any:
        token = _CALL.set(_Call(serializer_context))
        try:
            return dump(source)
        finally:
            _CALL.reset(token)

    return run


_WHOLE_MINUTE = datetime.timedelta(minutes=1)

# The time zones whose methods run no project code.
_LIBRARY_ZONES = frozenset((type(None), datetime.timezone, zoneinfo.ZoneInfo))


# A converter may also convert a whole column (``column``), with the call
# state read once. ``accepts`` tells, without running project code, whether
# the column holds only values whose conversion runs none either; the
# caller converts row by row otherwise, in DRF's order.
_DECIMALS = frozenset((type(None), decimal.Decimal))
_DATETIMES = frozenset((type(None), datetime.datetime))
_TZINFO = operator.attrgetter("tzinfo")


def _decimal_column(values: list[Any]) -> bool:
    # ``str()`` of anything but a Decimal may run project code.
    return set(map(type, values)) <= _DECIMALS


def _datetime_column(values: list[Any]) -> bool:
    # So may another value's, or another time zone's, methods. Datetimes are
    # truthy: ``filter`` drops the Nones.
    return (
        set(map(type, values)) <= _DATETIMES
        and set(map(type, map(_TZINFO, filter(None, values)))) <= _LIBRARY_ZONES
        and type(_call().zone()) in _LIBRARY_ZONES
    )


def _decimal_representation(field: Any) -> Callable[[Any], Any]:
    """
    DRF's ``DecimalField.to_representation`` of ``field`` as a string:
    quantized to its places in the thread's decimal context with its digits
    and rounding, normalized if it says so, formatted without an exponent.
    A localized field is DRF's own code. The encoder keeps the field's
    options only, not the serializer.
    """
    places = _option(field, "decimal_places")
    digits = _option(field, "max_digits")
    rounding = _option(field, "rounding")
    normalize = _option(field, "normalize_output")
    if _option(field, "localize") is True:
        return fields.DecimalField(
            digits,
            places,
            localize=True,
            rounding=rounding,
            normalize_output=normalize,
        ).to_representation
    # ``Decimal(".1") ** places``, whose exponent is all quantize() reads.
    exponent = None if places is None else decimal.Decimal(1).scaleb(-places)

    def represent(value: Any, context: Any = None) -> Any:
        if not isinstance(value, decimal.Decimal):
            value = decimal.Decimal(str(value).strip())
        if exponent is not None:
            if context is None:
                context = _call().context(digits, rounding)
            value = value.quantize(exponent, context=context)
        if normalize:
            value = value.normalize()
        return f"{value:f}"

    def represent_column(values: list[Any]) -> list[Any]:
        context = _call().context(digits, rounding)
        return [
            None if value is None else represent(value, context) for value in values
        ]

    represent.accepts = _decimal_column  # type: ignore[attr-defined]
    represent.column = represent_column  # type: ignore[attr-defined]
    return represent


def _datetime_representation(field: Any) -> Callable[[Any], Any]:
    """
    DRF's ``DateTimeField.to_representation`` of ``field`` in ISO 8601
    (:func:`_check_format`): the value converted to the current time zone,
    or left naive without ``USE_TZ``. The backend formats the datetime, as
    DRF does (``Z`` for a zero offset), when its offset is a whole number of
    minutes; anything else (a naive value to make aware, an aware one to
    make naive, an overflow, an offset in seconds, a string) is DRF's own
    code, with its errors. The encoder keeps the field's options only.
    """
    drf = fields.DateTimeField(error_messages=field.error_messages)
    output_format = _option(field, "format")
    if output_format is not _ABSENT:
        drf.format = output_format
    drf_representation = drf.to_representation

    def represent(value: Any, zone: Any = _ABSENT) -> Any:
        if type(value) is not datetime.datetime:
            return drf_representation(value)
        offset = value.utcoffset()
        if zone is _ABSENT:
            zone = _call().zone()
        if offset is None or zone is None:
            return (
                value if offset is None and zone is None else drf_representation(value)
            )
        if value.tzinfo is not zone:
            try:
                value = value.astimezone(zone)
            except OverflowError:
                return drf_representation(value)
            offset = value.utcoffset()
        return drf_representation(value) if offset % _WHOLE_MINUTE else value

    def represent_column(values: list[Any]) -> list[Any]:
        zone = _call().zone()
        return [None if value is None else represent(value, zone) for value in values]

    represent.accepts = _datetime_column  # type: ignore[attr-defined]
    represent.column = represent_column  # type: ignore[attr-defined]
    return represent


# -- Cache --------------------------------------------------------------------

#: Compiled variants kept per serializer class. A serializer with dynamic
#: fields has one variant per field set, which clients may control
#: (``?fields=``); past this limit DRF produces the output.
MAX_VARIANTS = 32

# A variant's signature can refer back to its serializer through a custom
# field class. Limit class buckets as well as variants within each bucket.
MAX_SERIALIZER_CLASSES = 1024


class _SerializerCache:
    """Weak, bounded class buckets shared by output and input compilation.

    Clearing detaches whole buckets. Work already using an old bucket may
    finish, but cannot republish it after a settings change or eviction.
    """

    def __init__(self) -> None:
        self._entries: weakref.WeakKeyDictionary[type, dict] = (
            weakref.WeakKeyDictionary()
        )
        self._lock = threading.Lock()

    def get(self, cls: type) -> dict | None:
        return self._entries.get(cls)

    def get_or_create(self, cls: type) -> dict:
        entries = self.get(cls)
        if entries is not None:
            return entries
        with self._lock:
            entries = self.get(cls)
            if entries is None:
                if len(self._entries) >= MAX_SERIALIZER_CLASSES:
                    self._entries.clear()
                entries = self._entries[cls] = {}
            return entries

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# Used by _variant_encoder(): (backend, parity, signature) -> Encoder or reason.
_compiled = depends_on_classification(_SerializerCache())
# Used by _class_encoder() and loaded_encoder(): (backend, parity) -> Encoder,
# decline reason, or None for a class whose representation has a coroutine hook.
_compiled_by_class = depends_on_classification(_SerializerCache())
_lock = threading.Lock()


def signature(serializer: serializers.BaseSerializer) -> tuple[Any, ...]:
    """
    Everything :func:`analyze` reads from a serializer *instance*.

    Instances of one class can differ: fields removed in ``__init__``,
    options changed per instance, ``Meta.depth``. A nested serializer is
    described by its model and its own signature rather than by its class,
    because DRF creates a new nested class for every instance when
    ``Meta.depth`` is set.
    """
    return tuple(_field_signature(field) for field in serializer._readable_fields)  # type: ignore[attr-defined]


def _field_signature(field: Any) -> tuple[Any, ...]:
    cls = type(field)
    kind: object
    if isinstance(field, serializers.Serializer):
        meta = getattr(field, "Meta", None)
        kind = (
            getattr(meta, "model", None),
            # A nested serializer's own choice (:func:`_delegates`).
            getattr(meta, "delegate_fields", None),
            signature(field),
        )
    elif isinstance(field, serializers.ListSerializer):
        kind = (cls, _field_signature(field.child))
    elif isinstance(field, relations.ManyRelatedField):
        # :func:`_primary_keys` reads the child's class and options.
        kind = (cls, _field_signature(field.child_relation))
    elif isinstance(field, fields.DateTimeField):
        # :func:`_datetime_representation` keeps the field's messages for
        # the errors DRF raises (an overflow, a naive value).
        kind = (cls, tuple(sorted(field.error_messages.items())))
    elif isinstance(field, fields.ChoiceField):
        # :func:`_scalar_type` compiles by the types of the choice keys.
        kind = (cls, frozenset(type(key) for key in field.choices))
    else:
        kind = cls
    options = tuple(
        # ``timezone`` and ``pk_field`` hold objects; only their presence counts.
        value
        if value is None or value is _ABSENT or isinstance(value, (str, int))
        else True
        for value in (_option(field, attr) for attr in _FIELD_ATTRS)
    )
    return (
        field.field_name,
        field.source,
        kind,
        definer(cls, "to_representation"),
        definer(cls, "get_attribute"),
        options,
        _registered_options(field),
    )


def _registered_options(field: Any) -> tuple[Any, ...]:
    """The options of a registered field (and of its ``pk_field``)."""
    values: list[Any] = []
    for target, registered in (
        (field, _KEY_REPRESENTATIONS),
        (field, _FIELD_REPRESENTATIONS),
        (getattr(field, "pk_field", None), _FIELD_REPRESENTATIONS),
    ):
        if target is None:
            continue
        registration = registered.get(definer(type(target), "to_representation"))
        if registration is not None:
            for option, key in registration.options:
                value = getattr(target, option, _ABSENT)
                if key is not None and value is not _ABSENT:
                    value = key(value)
                values.append(_comparable(value))
    return tuple(values)


def _comparable(value: Any) -> Any:
    """
    ``value`` as part of a signature: containers by their items, other
    values themselves when hashable, else an object equal to nothing, which
    compiles a variant per instance.
    """
    if isinstance(value, (list, tuple)):
        return (type(value), tuple(_comparable(item) for item in value))
    if isinstance(value, (set, frozenset)):
        return (type(value), frozenset(_comparable(item) for item in value))
    if isinstance(value, dict):
        return (
            dict,
            tuple((_comparable(k), _comparable(v)) for k, v in value.items()),
        )
    try:
        hash(value)
    except TypeError:
        return object()
    return value


def compiled_for(
    serializer: serializers.BaseSerializer, *, awaits: bool = False
) -> "Encoder | None":
    """
    Return the compiled encoder for ``serializer`` (or its child), or None
    when the DRF code path should be used.

    The source is checked apart, by :func:`declines_source`. Every reason
    not to compile goes through :func:`_decline`, so that
    ``SERIALIZER_BACKEND_FALLBACK = "error"`` (or ``Meta``'s override)
    raises for each of them instead of falling back to DRF silently.
    A static serializer (:func:`is_static`) whose representation has a
    coroutine hook returns None without a reason: it is not the compiler's
    to represent, and this may be asked before it is classified.

    ``awaits``: for an asynchronous caller, which awaits the coroutines of
    delegated fields (:class:`DelegatedStep`); fastdrf's own output never.
    """
    many = isinstance(serializer, serializers.ListSerializer)
    target = serializer.child if many else serializer  # type: ignore[attr-defined]
    backend = _backend_name(target)
    if backend == "drf" or getattr(target, "schema_library", None) is not None:
        # A msgspec or pydantic serializer outputs with its own schema.
        return None
    if many:
        reason = _list_hook(serializer)
        if reason:
            return _decline(target, backend, reason)  # type: ignore[func-returns-value]  # declining returns None
    if not isinstance(target, serializers.Serializer):
        # DRF's BaseSerializer pattern: no fields, only its own methods.
        return _decline(target, backend, f"{type(target).__qualname__} has no fields")  # type: ignore[func-returns-value]  # declining returns None
    parity = fastdrf_settings.SERIALIZER_BACKEND_PARITY
    delegate = _delegates(target)
    if is_static(target) or fields_from_class(serializer):
        encoder = _class_encoder(target, backend, parity, delegate, awaits)
        if encoder is None:
            return None
    elif _custom(type(target), "to_representation"):
        # Refused whatever the fields, which a validated serializer has
        # built: its signature need not be read.
        encoder = f"{type(target).__qualname__} overrides to_representation()"
    else:
        encoder = _instance_hook(target) or _variant_encoder(
            target, backend, parity, delegate, awaits
        )
    if isinstance(encoder, str):
        return _decline(target, backend, encoder)  # type: ignore[func-returns-value]  # declining returns None
    return encoder


def declines_source(serializer: serializers.BaseSerializer, source: Any) -> bool:
    """
    Return True when the encoder :func:`compiled_for` returned would not read
    ``source`` (the instance, or the list of items of a list serializer) as
    DRF does; DRF's code represents it then.

    DRF's ``Field.get_attribute`` reads a mapping by key and an object by
    attribute, and outputs a field's default, or None, or skips the field
    when the key or attribute is missing. The compiled class requires every
    attribute. An instance of the serializer's model has each one "strict"
    parity accepts (a concrete field, a forward relation, a related manager,
    of the nested serializers' models too); a mapping need not have any.
    Without a model ("fast" parity only), any object but a mapping is read.
    """
    many = isinstance(serializer, serializers.ListSerializer)
    target = serializer.child if many else serializer  # type: ignore[attr-defined]
    model = getattr(getattr(target, "Meta", None), "model", None)
    for kind in set(map(type, source)) if many else (type(source),):
        if issubclass(kind, Mapping) if model is None else not issubclass(kind, model):
            what = "a mapping" if model is None else f"not a {model.__name__}"
            _decline(
                target,
                _backend_name(target),
                f"it represents a {kind.__qualname__}, {what}",
                code="source_declined",
            )
            return True
    return False


def unreadable_source(serializer: serializers.BaseSerializer, error: Exception) -> bool:
    """
    Return True when DRF's code is to represent the source that the encoder
    :func:`compiled_for` returned failed on with its backend's validation
    error, with DRF's output or DRF's exception; False when the error is
    the result.

    An instance of the model can still hold what the compiled class cannot
    read. Reading a forward relation whose row does not exist, or a deferred
    field of a deleted row, raises ObjectDoesNotExist, which DRF's
    ``get_attribute`` outputs as None; a value of another type than the
    model field's, set by the project, is converted by DRF's field. DRF
    reads the source again in "strict" parity only, whose compiled class
    reads through Django's code alone (:func:`_check_framework_read`): the
    second read repeats at most a query of Django's, and raises what the
    backend took for a missing attribute (a database error) as itself.
    "fast" parity reads any object and the error is its result.

    Whatever ``SERIALIZER_BACKEND_FALLBACK`` says: it is about serializers
    that cannot be compiled, which no request changes, not about what one
    instance holds (an unsaved instance's related manager, a deleted row, a
    missing annotation).
    """
    return fastdrf_settings.SERIALIZER_BACKEND_PARITY == "strict"


def _class_encoder(
    serializer: Any, backend: str, parity: str, delegate: bool, awaits: bool = False
) -> "Encoder | str | None":
    entries = _compiled_by_class.get_or_create(type(serializer))
    key = (
        (backend, parity, delegate) if not awaits else (backend, parity, delegate, True)
    )
    try:
        return entries[key]
    except KeyError:
        pass
    # A function of the class too, for a static serializer.
    encoder = (
        None
        if has_async_representation(serializer) and not (awaits and delegate)
        else _compile(serializer, backend, parity, delegate, awaits)
    )
    return entries.setdefault(key, encoder)


def _variant_encoder(
    serializer: Any, backend: str, parity: str, delegate: bool, awaits: bool = False
) -> "Encoder | str":
    variants = _compiled.get_or_create(type(serializer))
    key = (backend, parity, delegate, awaits, signature(serializer))
    try:
        return variants[key]
    except KeyError:
        if len(variants) >= MAX_VARIANTS:
            return _TOO_MANY_VARIANTS
    # Compiled outside the lock; the bound is enforced inside it, where
    # threads that compiled the same or other variants meet.
    encoder = _compile(serializer, backend, parity, delegate, awaits)
    with _lock:
        if key not in variants and len(variants) >= MAX_VARIANTS:
            return _TOO_MANY_VARIANTS
        return variants.setdefault(key, encoder)


def _compile(
    serializer: Any, backend: str, parity: str, delegate: bool, awaits: bool = False
) -> "Encoder | str":
    try:
        spec = analyze(serializer, parity, delegate, awaits)
        # Building can fail too: a backend may be unable to express what
        # the analysis accepted.
        encoder = _build(backend, spec)
    except NotCompilable as exc:
        # The reason is kept in place of the encoder.
        return str(exc)
    encoder.columns = _columns(
        getattr(getattr(serializer, "Meta", None), "model", None), spec
    )
    encoder.delegated = spec.delegation()
    encoder.backend = backend
    encoder.dump = _in_call(encoder.dump)
    encoder.dump_many = _in_call(encoder.dump_many)
    return encoder


def _columns(model: Any, spec: OutputSpec) -> tuple[str, ...] | None:
    """
    The attributes ``spec`` reads if each is a column of ``model`` read by
    Django's descriptor, which returns a loaded column from the instance
    without a query; else None. Inspects descriptors, never model values,
    and only for a model whose attribute access is Python's own.
    """
    if (
        model is None
        or model.__getattribute__ is not object.__getattribute__
        or inspect.getattr_static(model, "__getattr__", None) is not None
        or type(inspect.getattr_static(model, "__dict__", None))
        is not GetSetDescriptorType
    ):
        return None
    for field in spec.fields:
        descriptor = inspect.getattr_static(model, field.attribute, None)
        if (
            isinstance(field.type, OutputSpec)
            or not (
                isinstance(descriptor, DeferredAttribute)
                and is_framework_class(type(descriptor))
                and descriptor.field.attname == field.attribute
            )
            # A storage may perform I/O to build a file's URL.
            or isinstance(descriptor.field, models.FileField)
        ):
            return None
    return tuple(field.attribute for field in spec.fields)


# The exact types Django's database converters give the columns the compiler
# reads. A value of any other type, a subclass or one the project set, takes
# the ordinary path (:func:`compiled_data`), which decides DRF's fallback.
_LOADED_TYPES = frozenset(
    {
        str,
        int,
        float,
        bool,
        type(None),
        decimal.Decimal,
        uuid.UUID,
        datetime.datetime,
        datetime.date,
        datetime.time,
    }
)


@depends_on_classification
@class_cache
def _plain_meta(cls: type) -> type | None:
    """
    ``cls.Meta`` when it is a plain class and instances of ``cls`` keep
    their attributes in a plain ``__dict__``; else None. Read statically,
    once per class: ``loaded_encoder`` runs for every output.
    """
    meta = inspect.getattr_static(cls, "Meta", None)
    if (
        type(meta) is not type
        or type(inspect.getattr_static(cls, "__dict__", None))
        is not GetSetDescriptorType
    ):
        return None
    return meta


def loaded_encoder(serializer: serializers.BaseSerializer) -> "Encoder | None":
    """
    Return the encoder of a static ``serializer``'s class when it is compiled
    already and reads only columns that its instance, of the serializer's
    model itself, has loaded with values of :data:`_LOADED_TYPES`; else None.

    Nothing is analyzed or built here, no serializer field is constructed,
    and a list serializer is not looked at.
    """
    cls = type(serializer)
    if type(cls) is not serializers.SerializerMetaclass:
        return None
    entries = _compiled_by_class.get(cls)
    if not entries:
        return None
    if user_defines(
        cls,
        "data",
        "context",
        "to_representation",
        "ato_representation",
        "__getattribute__",
        "__getattr__",
        "__setattr__",
        "instance",
        "initial_data",
        "_data",
        "_errors",
        "_kwargs",
    ) or isinstance(serializer, serializers.ListSerializer):
        return None
    meta = _plain_meta(cls)
    if meta is None:
        return None
    state = vars(serializer)
    if (
        state.get("instance") is None
        or "initial_data" in state
        or "_data" in state
        or "_errors" in state
        or type(state.get("_kwargs")) is not dict
        or not is_static(serializer)
    ):
        return None
    source = state["instance"]
    # As ``getattr_static`` reads them: ``Meta`` is a plain class (above),
    # whose class dictionaries along its MRO are all there is.
    backend = delegate = None
    for klass in reversed(meta.__mro__):
        options = klass.__dict__
        backend = options.get("serializer_backend", backend)
        delegate = options.get("delegate_fields", delegate)
    if backend is not None and type(backend) is not str:
        return None
    backend = backend or fastdrf_settings.SERIALIZER_BACKEND
    if delegate is not None and type(delegate) is not bool:
        return None
    encoder = entries.get(
        (
            backend,
            fastdrf_settings.SERIALIZER_BACKEND_PARITY,
            fastdrf_settings.DELEGATE_FIELDS if delegate is None else delegate,
        )
    )
    if (
        not isinstance(encoder, Encoder)
        or encoder.columns is None
        # Its delegated fields need the serializer's fields; never the case
        # today, since a delegated field reads no column.
        or encoder.delegated is not None
    ):
        return None
    if type(source) is not inspect.getattr_static(meta, "model", None):
        return None
    loaded = vars(source)
    if any(
        name not in loaded or type(loaded[name]) not in _LOADED_TYPES
        for name in encoder.columns
    ):
        return None
    return encoder


_TOO_MANY_VARIANTS = f"its instances have more than {MAX_VARIANTS} field sets"

# What DRF's representation calls on a serializer and on its fields.
_INSTANCE_HOOKS = ("to_representation", "get_attribute")


def _list_hook(serializer: Any) -> str | None:
    """The reason the list serializer's own method keeps it on DRF, or None."""
    # It decides which items are represented, and how.
    if _custom(type(serializer), "to_representation"):
        return f"{type(serializer).__qualname__} overrides to_representation()"
    if "to_representation" in vars(serializer):
        return "the list serializer has to_representation() assigned to the instance"
    return None


def _called_parts(field: Any) -> Iterator[tuple[Any, str]]:
    """``field`` and the fields DRF calls to represent its value."""
    yield field, ""
    # A to-many relation represents each item with its child relation.
    child = getattr(field, "child_relation", None)
    if child is not None:
        yield child, " (its child_relation)"
    for owner, label in ((field, ""), (child, " (its child_relation)")):
        pk_field = getattr(owner, "pk_field", None)
        if pk_field is not None:
            yield pk_field, f"{label} (its pk_field)"


def _instance_hook(serializer: Any) -> str | None:
    """The reason an instance's own method keeps it on DRF, or None."""
    for name in _INSTANCE_HOOKS:
        if name in vars(serializer):
            return f"{name}() is assigned to the instance"
    for field in serializer._readable_fields:
        for part, label in _called_parts(field):
            for name in _INSTANCE_HOOKS:
                if name in vars(part):
                    return (
                        f"field {field.field_name!r}{label} has {name}() "
                        "assigned to the instance"
                    )
        # DRF calls the child's methods for every item of a nested list.
        child = field.child if isinstance(field, serializers.ListSerializer) else field
        if isinstance(child, serializers.Serializer):
            nested = _instance_hook(child)
            if nested:
                return f"field {field.field_name!r}: {nested}"
    return None


def _decline(
    serializer: Any, backend: str, reason: str, code: str = "not_compiled"
) -> None:
    if _fallback(serializer) == "error":
        raise ImproperlyConfigured(
            f"{type(serializer).__qualname__} cannot use the {backend} backend: {reason}."
        )
    left_to_drf(serializer, backend, code, reason)


def _fallback(serializer: Any) -> str:
    meta = getattr(serializer, "Meta", None)
    return (
        getattr(meta, "serializer_backend_fallback", None)
        or fastdrf_settings.SERIALIZER_BACKEND_FALLBACK
    )


def clear_compiled(*, setting: str, **kwargs: Any) -> None:
    # ``analyze`` reads DRF's output formats.
    if setting == "REST_FRAMEWORK":
        forget_compiled()


def forget_compiled() -> None:
    """Compile every serializer again, after a registration."""
    _compiled.clear()
    _compiled_by_class.clear()
    _plain_meta.cache_clear()


setting_changed.connect(clear_compiled)


def report(
    serializer: serializers.BaseSerializer,
    parity: str = "strict",
    backend: str | None = None,
) -> str | None:
    """Return ``None`` if ``serializer`` compiles, else the reason it does not."""
    return report_details(serializer, parity, backend).reason


def report_details(
    serializer: serializers.BaseSerializer,
    parity: str = "strict",
    backend: str | None = None,
    delegate: bool | None = None,
) -> Eligibility:
    """
    Structured counterpart of :func:`report`; codes do not depend on wording.
    ``delegate`` defaults to the serializer's ``Meta.delegate_fields``, else
    ``DELEGATE_FIELDS``.
    """
    target = serializer
    if isinstance(serializer, serializers.ListSerializer):
        reason = _list_hook(serializer)
        if reason:
            return Eligibility("custom_hook", reason)
        target = cast(serializers.BaseSerializer, serializer.child)
    if isinstance(target, serializers.Serializer):
        reason = _instance_hook(target)
        if reason:
            return Eligibility("custom_hook", reason)
    if delegate is None:
        delegate = _delegates(target)
    try:
        spec = analyze(target, parity, delegate)
        if backend not in (None, "drf"):
            _build(backend, spec)
    except NotCompilable as exc:
        return Eligibility(exc.code, str(exc))
    return Eligibility(delegated=spec.delegated)


def _delegates(serializer: Any, default: bool | None = None) -> bool:
    """
    ``Meta.delegate_fields`` of ``serializer``, else ``default`` (a nested
    serializer's parent's), else ``DELEGATE_FIELDS``.
    """
    meta = getattr(serializer, "Meta", None)
    delegate = getattr(meta, "delegate_fields", None)
    if delegate is None:
        return fastdrf_settings.DELEGATE_FIELDS if default is None else default
    if type(delegate) is not bool:
        raise ImproperlyConfigured(
            f"{type(serializer).__qualname__}.Meta.delegate_fields must be True, "
            f"False or None, not {delegate!r}."
        )
    return delegate


def _backend_name(serializer: Any) -> str:
    meta = getattr(serializer, "Meta", None)
    return (
        getattr(meta, "serializer_backend", None) or fastdrf_settings.SERIALIZER_BACKEND
    )


def _build(backend: str, spec: OutputSpec) -> "Encoder":
    if backend == "msgspec":
        from fastdrf.msgspec.compiler import build
    elif backend == "pydantic":
        from fastdrf.pydantic.compiler import build
    elif backend == "python":
        from fastdrf.output import build
    else:
        raise ValueError(f"Unknown serializer backend {backend!r}")
    return build(spec)


class Encoder:
    """
    ``dump`` and ``dump_many`` produce DRF's output; they raise ``error``,
    the backend's validation error, for a source they cannot read
    (:func:`unreadable_source`).
    """

    __slots__ = (
        "backend",
        "columns",
        "delegated",
        "dump",
        "dump_many",
        "error",
        "schema",
    )

    def __init__(
        self,
        schema: Any,
        dump: Callable[..., Any],
        dump_many: Callable[..., Any],
        error: type[Exception],
    ) -> None:
        self.schema = schema
        self.dump = dump
        self.dump_many = dump_many
        self.error = error
        # The columns it reads, when it reads nothing else (``_columns``).
        self.columns: tuple[str, ...] | None = None
        # The backend's name, set when it is compiled.
        self.backend: str | None = None
        # The fields the serializer's own fields represent (:func:`fill_delegated`).
        self.delegated: Delegation | None = None
