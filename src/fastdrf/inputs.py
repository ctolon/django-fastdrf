"""
Validate input with a class compiled from a DRF serializer.

The compiled class is a *recognizer*, not a second validator. It accepts only
input on which DRF's own validation is known to return exactly the same
``validated_data``: JSON values that already have the types DRF converts
to, strings that are already trimmed, and so on. Whenever it does not accept
the input, for whatever reason, DRF validates it as usual, so every error,
every coercion (``"12"`` for an integer, ``"yes"`` for a boolean) and every
message stays DRF's. Parity therefore rests on two properties, which
``tests/test_inputs.py`` checks with generated input:

* what the recognizer accepts, DRF accepts;
* and with the same ``validated_data``, value for value and type for type.

A serializer takes part only if nothing but the supported field classes
decides its result: no ``validate()`` / ``validate_<field>()``, no serializer
validators (so no unique-together constraints), no relations, no callable
defaults. ``report_input_details()`` reports structural eligibility and the
reason when a serializer cannot use compiled input recognition.

Both backends recognize a conservative subset, including canonical ISO dates,
times and UUID strings. Coercions and application validators remain DRF's.
"""

import datetime
import keyword
import math
import re
import threading
import types
import typing
import uuid
from collections.abc import Callable
from typing import Any, cast

from django.core import validators as django_validators
from django.core.signals import setting_changed
from rest_framework import ISO_8601, fields, serializers
from rest_framework import validators as drf_validators
from rest_framework.settings import api_settings

from fastdrf._classify import is_static
from fastdrf.compat import BigIntegerField
from fastdrf.compiler import (
    MAX_VARIANTS,
    Eligibility,
    NotCompilable,
    _SerializerCache,
)
from fastdrf.utils import depends_on_classification, user_defines

__all__ = [
    "NOT_RECOGNIZED",
    "analyze_input",
    "recognize",
    "report_input",
    "report_input_details",
]

#: Returned by :func:`recognize` when DRF has to validate the input.
NOT_RECOGNIZED = object()

# The hooks of a serializer that take part in validation. A project's
# override of any of them means its result is not the fields' alone.
_SERIALIZER_HOOKS = (
    "is_valid",
    "run_validation",
    "validate_empty_values",
    "to_internal_value",
    "run_validators",
    "get_validators",
    "validate",
    "get_fields",
    "get_value",
    "set_value",
)
_LIST_HOOKS = (*_SERIALIZER_HOOKS, "run_child_validation")
_ASYNC_PAIRS = (
    ("is_valid", "ais_valid"),
    ("run_validation", "arun_validation"),
    ("to_internal_value", "ato_internal_value"),
    ("run_validators", "arun_validators"),
    ("validate", "avalidate"),
)
_CONSTANTS = (type(None), bool, int, float, str)
# Unsupported constants are always declined; their identity is not metadata.
# In particular, a callable limit may close over a serializer or request.
_UNSUPPORTED_CONSTANT = object()
_PLAIN_INPUT_TYPES = frozenset((*_CONSTANTS, datetime.date, datetime.time, uuid.UUID))
_SURROGATES = re.compile("[\ud800-\udfff]")


class InputField:
    __slots__ = ("default", "finish", "name", "optional", "source_attrs", "type")

    def __init__(
        self,
        name: str,
        source_attrs: list[str],
        type: Any,
        optional: bool,
        default: Any,
        finish: Callable[[Any], Any] | None,
    ) -> None:
        self.name = name
        self.source_attrs = source_attrs
        self.type = type
        #: The key may be absent (``required=False`` or a partial update).
        self.optional = optional
        #: What an absent key becomes; ``fields.empty`` leaves the field out.
        self.default = default
        #: ``finish(value)`` returns DRF's value, or :data:`NOT_RECOGNIZED`.
        self.finish = finish


class InputSpec:
    __slots__ = ("fields", "name", "partial")

    def __init__(self, name: str, fields: list[InputField], partial: bool) -> None:
        self.name = name
        self.fields = fields
        self.partial = partial


# -- Analysis -------------------------------------------------------------------


def analyze_input(
    serializer: serializers.BaseSerializer, *, backend: str = "msgspec"
) -> "InputSpec":
    """Return the :class:`InputSpec` of ``serializer`` or raise NotCompilable."""
    cls = type(serializer)
    name = cls.__qualname__
    if user_defines(cls, *_SERIALIZER_HOOKS, "__getattr__", "__getattribute__"):
        raise NotCompilable(f"{name} overrides a validation method", code="custom_hook")
    if any(user_defines(cls, *pair) for pair in _ASYNC_PAIRS):
        raise NotCompilable(f"{name} has async validation", code="async_validation")
    if serializer.validators:
        raise NotCompilable(
            f"{name} has serializer validators", code="custom_validator"
        )
    stamped = _dynamic_read_only_default(serializer)
    if stamped is not None:
        raise NotCompilable(
            f"{name}.{stamped.field_name} has a default that is not a constant",
            code="dynamic_default",
        )

    partial = bool(serializer.root.partial)
    input_fields = []
    for field in serializer._writable_fields:  # type: ignore[attr-defined]
        label = f"{name}.{field.field_name}"
        for prefix in ("validate_", "avalidate_"):
            if getattr(serializer, prefix + field.field_name, None) is not None:
                raise NotCompilable(
                    f"{label} has a {prefix}{field.field_name}() hook",
                    code="custom_hook",
                )
        if field.source == "*":
            raise NotCompilable(f"{label} has source='*'", code="unsupported_source")
        input_fields.append(_input_field(field, label, partial, backend))
    return InputSpec(f"{cls.__name__}Input", input_fields, partial)


def _dynamic_read_only_default(serializer: Any) -> Any:
    """
    A read-only field whose default DRF calls at every validation, for the
    serializer's validators (``Serializer._read_only_defaults``), or None.
    Its call may have effects or raise, so the recognizer cannot skip it.
    """
    for field in serializer.fields.values():
        if (
            field.read_only
            and field.default is not fields.empty
            and field.source != "*"
            and "." not in field.source
            and type(field.default) not in _CONSTANTS
        ):
            return field
    return None


def _input_field(field: Any, label: str, partial: bool, backend: str) -> InputField:
    default = field.default
    if default is not fields.empty and type(default) not in _CONSTANTS:
        raise NotCompilable(
            f"{label} has a default that is not a constant", code="dynamic_default"
        )
    field_type, finish = _field_type(field, label, backend)
    return InputField(
        field.field_name,
        field.source_attrs,
        field_type,
        optional=partial or not field.required,
        default=default,
        finish=finish,
    )


def _field_type(
    field: Any, label: str, backend: str
) -> tuple[Any, Callable[[Any], Any] | None]:
    """Compile a value contract, including children without input names."""
    if isinstance(field, serializers.BaseSerializer) and user_defines(
        type(field), "get_default"
    ):
        # DRF asks a nested serializer missing from full input for its
        # default: the method is the project's, with its own value.
        raise NotCompilable(f"{label} overrides get_default()", code="custom_hook")
    if isinstance(field, serializers.ListSerializer):
        field_type, finish = _nested_list(field, label, backend)
    elif isinstance(field, serializers.BaseSerializer):
        spec = analyze_input(field, backend=backend)
        field_type, finish = model_for(spec, backend), _finish_with(spec, backend)
    else:
        try:
            build = _FIELD_TYPES[type(field)]
        except KeyError:
            raise NotCompilable(f"{label} is a {type(field).__name__}") from None
        field_type, finish = build(field, label, backend)
    if field.allow_null:
        field_type = field_type | None
    return field_type, finish


def _nested_list(
    field: Any, label: str, backend: str
) -> tuple[Any, Callable[[Any], Any] | None]:
    if user_defines(type(field), *_LIST_HOOKS):
        raise NotCompilable(f"{label} has a custom list serializer")
    if field.validators:
        raise NotCompilable(f"{label} has list validators")
    spec = analyze_input(field.child, backend=backend)
    finish_item = _finish_with(spec, backend)
    constraints = {
        "min_length": max(
            getattr(field, "min_length", None) or 0, 0 if field.allow_empty else 1
        ),
        "max_length": getattr(field, "max_length", None),
    }

    def finish(items: Any) -> Any:
        values = []
        for item in items:
            value = finish_item(item)
            if value is NOT_RECOGNIZED:
                return NOT_RECOGNIZED
            values.append(value)
        return values

    items = list[model_for(spec, backend)]  # type: ignore[misc,valid-type]  # built at runtime
    return _constrained(items, constraints, backend), finish


def _constraints(field: Any, label: str, *, numeric: bool) -> dict[str, Any]:
    """The constant bounds shared by the two backends."""
    limits = (
        {
            django_validators.MinValueValidator: "ge",
            django_validators.MaxValueValidator: "le",
        }
        if numeric
        else {
            django_validators.MinLengthValidator: "min_length",
            django_validators.MaxLengthValidator: "max_length",
        }
    )
    checked_by_finish = (
        django_validators.ProhibitNullCharactersValidator,
        drf_validators.ProhibitSurrogateCharactersValidator,
    )
    arguments: dict[str, Any] = {}
    for validator in field.validators:
        kind = type(validator)
        limit = getattr(validator, "limit_value", None)
        if kind in limits and type(limit) in (int, float):
            # DRF runs every validator: two bounds of one kind are the
            # stronger one. (A callable limit is not a constant; it is
            # refused below, like every other validator.)
            name = limits[kind]
            tighter = max if name in ("ge", "min_length") else min
            arguments[name] = (
                tighter(arguments[name], limit) if name in arguments else limit
            )
        elif not (kind in checked_by_finish and not numeric):
            raise NotCompilable(
                f"{label} has a {kind.__name__}", code="custom_validator"
            )
    return arguments


def _constrained(python_type: Any, arguments: dict[str, Any], backend: str) -> Any:
    if not arguments:
        return python_type
    if backend == "pydantic":
        from pydantic import Field

        meta = Field(**arguments)
    else:
        from msgspec import Meta

        meta = Meta(**arguments)
    return typing.Annotated[python_type, meta]


def _boolean(field: Any, label: str, backend: str) -> tuple[Any, None]:
    if field.validators:
        raise NotCompilable(f"{label} has validators")
    return bool, None


def _integer(field: Any, label: str, backend: str) -> tuple[Any, None]:
    arguments = _constraints(field, label, numeric=True)
    for name, round_ in (("ge", math.ceil), ("le", math.floor)):
        limit = arguments.get(name)
        if isinstance(limit, float):
            # An integer is at least 1.5 when it is at least 2: the same bound,
            # which the backends take for an integer only as an integer.
            if not math.isfinite(limit):
                raise NotCompilable(f"{label} has an infinite bound")
            arguments[name] = round_(limit)
    return _constrained(int, arguments, backend), None


def _float(field: Any, label: str, backend: str) -> tuple[Any, Callable[[Any], Any]]:
    # The bounds as DRF compares them: an int bound a float cannot hold
    # (2**53 + 1) is rounded in the backend's constraint, which may then
    # accept what DRF refuses.
    low, high = field.min_value, field.max_value

    def finish(value: float) -> Any:
        # DRF rejects NaN and the infinities; msgspec does not.
        if not math.isfinite(value):
            return NOT_RECOGNIZED
        if (low is not None and value < low) or (high is not None and value > high):
            return NOT_RECOGNIZED
        return value

    return _constrained(
        float, _constraints(field, label, numeric=True), backend
    ), finish


def _char(field: Any, label: str, backend: str) -> tuple[Any, Callable[[Any], Any]]:
    allow_blank, trim = field.allow_blank, field.trim_whitespace
    arguments = _constraints(field, label, numeric=False)
    # DRF returns ``""`` before its length validators run, which they do
    # for every other value: the minimum is checked in ``finish`` instead.
    min_length = arguments.pop("min_length", None) if allow_blank else None

    def finish(value: str) -> Any:
        if value == "":
            return value if allow_blank else NOT_RECOGNIZED
        # DRF strips with ``str.strip()`` (which also removes control
        # characters such as ``\\x1f``) and rejects NUL and surrogates.
        if trim and value != value.strip():
            return NOT_RECOGNIZED
        # An ASCII string has no surrogates, and ``isascii()`` reads a flag.
        if "\x00" in value or (not value.isascii() and _SURROGATES.search(value)):
            return NOT_RECOGNIZED
        if min_length is not None and len(value) < min_length:
            return NOT_RECOGNIZED
        return value

    return _constrained(str, arguments, backend), finish


def _choice(field: Any, label: str, backend: str) -> tuple[Any, None]:
    if field.validators:
        raise NotCompilable(f"{label} has validators")
    keys = list(field.choices)
    if not keys or not all(type(key) in (str, int) for key in keys):
        raise NotCompilable(f"{label} has choices that are not strings or integers")
    # DRF looks ``str(data)`` up: ``1`` and ``"1"`` would be the same choice.
    if len({str(key) for key in keys}) != len(keys):
        raise NotCompilable(f"{label} has choices that collide as strings")
    if field.allow_blank and "" not in keys:
        keys.append("")
    literal = typing.Literal[tuple(keys)]  # type: ignore[valid-type]  # built at runtime
    if backend == "pydantic":
        from pydantic import BeforeValidator

        # Pydantic's Literal[1] also accepts True; DRF looks up "True",
        # not "1". Decline values that are not exact canonical choices.
        choices = {(type(key), key) for key in keys}

        def canonical(value: Any) -> Any:
            if type(value) not in (str, int) or (type(value), value) not in choices:
                raise ValueError("not a canonical choice")
            return value

        literal = typing.Annotated[literal, BeforeValidator(canonical)]
    return literal, None


def _uuid(field: Any, label: str, backend: str) -> tuple[Any, None]:
    if field.validators:
        raise NotCompilable(f"{label} has validators")
    return _text_input(uuid.UUID, uuid.UUID, backend), None


def _date(field: Any, label: str, backend: str) -> tuple[Any, None]:
    if field.validators:
        raise NotCompilable(f"{label} has validators")
    # As DRF reads it: an empty list given to the field reads no text.
    formats = getattr(field, "input_formats", api_settings.DATE_INPUT_FORMATS)
    if list(formats) != [ISO_8601]:
        raise NotCompilable(f"{label} does not only read ISO 8601 dates")
    return _text_input(datetime.date, datetime.date.fromisoformat, backend), None


def _text_input(python_type: type, parse: Callable[[str], Any], backend: str) -> Any:
    """Accept text for these scalar types without enabling general coercion."""
    if backend != "pydantic":
        return python_type
    from pydantic import BeforeValidator

    def convert(value: Any) -> Any:
        return parse(value) if type(value) is str else value

    return typing.Annotated[python_type, BeforeValidator(convert)]


def _time(field: Any, label: str, backend: str) -> tuple[Any, Callable[[Any], Any]]:
    if field.validators:
        raise NotCompilable(f"{label} has validators")
    # As DRF reads it: an empty list given to the field reads no text.
    formats = getattr(field, "input_formats", api_settings.TIME_INPUT_FORMATS)
    if list(formats) != [ISO_8601]:
        raise NotCompilable(f"{label} does not only read ISO 8601 times")

    def finish(value: datetime.time) -> Any:
        # Django discards offsets in time strings, but preserves time objects.
        # Keep that distinction on DRF instead of guessing after conversion.
        if value.tzinfo is not None:
            return NOT_RECOGNIZED
        if type(value) is _MsgspecTime:
            return datetime.time(
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
                fold=value.fold,
            )
        return value

    if backend == "msgspec":
        return _MsgspecTime, finish
    return _text_input(datetime.time, datetime.time.fromisoformat, backend), finish


class _MsgspecTime(datetime.time):
    """
    A time that msgspec reads through :func:`_msgspec_dec_hook`: its own
    parser rounds digits beyond microseconds, where Django's ``parse_time``
    (``time.fromisoformat``) truncates them.
    """

    __slots__ = ()


def _msgspec_dec_hook(type_: Any, value: Any) -> Any:
    if type_ is _MsgspecTime:
        if type(value) is str:
            value = datetime.time.fromisoformat(value)
        if type(value) is datetime.time:
            return _MsgspecTime(
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
                value.tzinfo,
                fold=value.fold,
            )
    raise TypeError(f"{type(value).__name__} is not a time")


def _collection(
    field: Any, label: str, backend: str
) -> tuple[Any, Callable[[Any], Any]]:
    child_type, finish_child = _field_type(field.child, f"{label}.child", backend)
    arguments = _constraints(field, label, numeric=False)
    if not field.allow_empty:
        arguments["min_length"] = max(arguments.get("min_length", 0), 1)
    is_list = type(field) is fields.ListField
    container = list[child_type] if is_list else dict[str, child_type]  # type: ignore[valid-type]  # built at runtime

    # One function per shape: these run for every item of every request.
    def finish_list(items: Any) -> Any:
        result = []
        for item in items:
            value = item
            if item is not None and finish_child is not None:
                value = finish_child(item)
                if value is NOT_RECOGNIZED:
                    return NOT_RECOGNIZED
            result.append(value)
        return result

    def finish_dict(items: Any) -> Any:
        result = {}
        for key, item in items.items():
            value = item
            if item is not None and finish_child is not None:
                value = finish_child(item)
                if value is NOT_RECOGNIZED:
                    return NOT_RECOGNIZED
            result[key] = value
        return result

    finish = finish_list if is_list else finish_dict
    return _constrained(container, arguments, backend), finish


# Exact classes: a subclass may convert or validate differently.
_FIELD_TYPES = {
    fields.BooleanField: _boolean,
    fields.IntegerField: _integer,
    fields.FloatField: _float,
    fields.CharField: _char,
    fields.ChoiceField: _choice,
    fields.UUIDField: _uuid,
    fields.DateField: _date,
    fields.TimeField: _time,
    fields.ListField: _collection,
    fields.DictField: _collection,
    fields.HStoreField: _collection,
}
if BigIntegerField is not None:
    # DRF 3.17: ModelSerializer's field for the big integer model fields,
    # whose input is IntegerField's.
    _FIELD_TYPES[BigIntegerField] = _integer


# -- The compiled class -------------------------------------------------------------


def model_for(spec: "InputSpec", backend: str) -> Any:
    """A backend model; unknown keys are ignored, as in DRF."""
    if backend == "pydantic":
        from pydantic import ConfigDict, Field, create_model

        # Internal names cannot collide with BaseModel methods or private
        # attributes. Aliases keep DRF's input names unchanged.
        definitions: dict[str, Any] = {
            f"field_{index}": (
                field.type,
                Field(
                    default=fields.empty if field.optional else ..., alias=field.name
                ),
            )
            for index, field in enumerate(spec.fields)
        }
        return create_model(
            spec.name, __config__=ConfigDict(strict=True), **definitions
        )

    import msgspec

    struct_fields: list[tuple[str, Any] | tuple[str, Any, Any]] = []
    for field in spec.fields:
        if not field.name.isidentifier() or keyword.iskeyword(field.name):
            # A Struct's fields are attributes: DRF validates the others
            # (``display-name``).
            raise NotCompilable(f"{field.name!r} is not a Python name")
        if field.optional:
            struct_fields.append(
                (field.name, field.type | msgspec.UnsetType, msgspec.UNSET)
            )
        else:
            struct_fields.append((field.name, field.type))
    return msgspec.defstruct(spec.name, struct_fields, kw_only=True)


def _finish_with(spec: InputSpec, backend: str) -> Callable[[Any], Any]:
    """Return ``finish(struct)`` building DRF's ``validated_data`` for ``spec``."""
    set_value = serializers.Serializer.set_value
    unset: Any
    if backend == "pydantic":
        unset = fields.empty
    else:
        from msgspec import UNSET

        unset = UNSET

    # Resolved once: ``finish`` runs for every row of every request.
    # ``skip`` is ``Field.validate_empty_values``: absent input is skipped in
    # partial updates and without a default.
    bindings = tuple(
        (
            f"field_{index}" if backend == "pydantic" else field.name,
            spec.partial or field.default is fields.empty,
            field.default,
            field.finish,
            field.source_attrs[0] if len(field.source_attrs) == 1 else None,
            field.source_attrs,
        )
        for index, field in enumerate(spec.fields)
    )

    def finish(obj: Any) -> Any:
        if obj is None:
            return None
        ret = {}
        for name, skip, default, finish_value, key, source_attrs in bindings:
            value = getattr(obj, name)
            if value is unset:
                if skip:
                    continue
                value = default
            elif value is not None and finish_value is not None:
                value = finish_value(value)
                if value is NOT_RECOGNIZED:
                    return NOT_RECOGNIZED
            if key is not None:
                ret[key] = value
            else:
                # DRF's ``set_value`` does not use ``self``.
                set_value(None, ret, source_attrs, value)  # type: ignore[arg-type]
        return ret

    return finish


def _canonical_types(annotation: Any) -> Any:
    """A rejection-only shortcut for primitive strict backend inputs."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _canonical_types(typing.get_args(annotation)[0])
    if origin is types.UnionType:
        parts = [_canonical_types(item) for item in typing.get_args(annotation)]
        if all(part is not None for part in parts):
            return tuple(kind for part in parts for kind in part)
        return None
    if annotation is float:
        return (int, float)
    if annotation in (type(None), bool, int, str):
        return (annotation,)
    return None


class Recognizer:
    __slots__ = ("convert", "early_types", "finish", "many", "schema")

    def __init__(self, spec: InputSpec, *, many: bool, backend: str) -> None:
        struct = model_for(spec, backend)
        self.schema = list[struct] if many else struct  # type: ignore[valid-type]
        self.finish = _finish_with(spec, backend)
        self.many = many
        self.early_types = tuple(
            (field.name, accepted)
            for field in spec.fields
            if (accepted := _canonical_types(field.type)) is not None
        )
        if backend == "pydantic":
            from pydantic import TypeAdapter

            self.convert = TypeAdapter(self.schema).validate_python
        else:
            from functools import partial

            import msgspec

            self.convert = partial(
                msgspec.convert, type=self.schema, dec_hook=_msgspec_dec_hook
            )

    def __call__(self, data: Any) -> Any:
        # Exactly ``dict`` / ``list``: a QueryDict is form input, which DRF
        # reads differently.
        if type(data) is not (list if self.many else dict):
            return NOT_RECOGNIZED
        # A coercion-heavy batch often fails at its first row. Decline before
        # walking the whole batch, without creating backend exceptions. This
        # never accepts input or caches a decision about subsequent requests.
        first = data[0] if self.many and data else data
        if type(first) is dict:
            if any(type(key) is not str for key in first):
                return NOT_RECOGNIZED
            for name, accepted in self.early_types:
                value = first.get(name, fields.empty)
                if value is not fields.empty and type(value) not in accepted:
                    return NOT_RECOGNIZED
        try:
            if not _plain_input(data):
                return NOT_RECOGNIZED
            converted = self.convert(data, strict=True)
        except Exception:  # noqa: BLE001 -- backend errors, overflow and recursive input decline recognition
            return NOT_RECOGNIZED
        if not self.many:
            return self.finish(converted)
        values = []
        for item in converted:
            value = self.finish(item)
            if value is NOT_RECOGNIZED:
                return NOT_RECOGNIZED
            values.append(value)
        return values


def _plain_input(value: Any) -> bool:
    # Backends can accept their own model instances inside a dict; a DRF
    # nested serializer requires a mapping. Do not let a backend's richer
    # Python-object protocol widen the accepted input or call user code.
    kind = type(value)
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                return False
            # Leaves are checked here: a call per value would cost more
            # than the check.
            if type(item) not in _PLAIN_INPUT_TYPES and not _plain_input(item):
                return False
        return True
    if kind is list:
        for item in value:
            if type(item) not in _PLAIN_INPUT_TYPES and not _plain_input(item):
                return False
        return True
    return kind in _PLAIN_INPUT_TYPES


# -- Cache --------------------------------------------------------------------------

# _recognizer_for(): class -> {signature: Recognizer or None}. Both the
# number of classes and each class's variants are bounded.
_recognizers = depends_on_classification(_SerializerCache())
_lock = threading.Lock()


def recognize(
    serializer: serializers.BaseSerializer, *, backend: str = "msgspec"
) -> Any:
    """
    Return DRF's ``validated_data`` for ``serializer.initial_data``, or
    :data:`NOT_RECOGNIZED` when DRF has to validate it.
    """
    recognizer = _recognizer_for(serializer, backend)
    if recognizer is None:
        return NOT_RECOGNIZED
    return recognizer(serializer.initial_data)


def _recognizer_for(serializer: Any, backend: str) -> Recognizer | None:
    if backend not in ("msgspec", "pydantic"):
        raise ValueError(f"Unknown input backend: {backend!r}")
    many = isinstance(serializer, serializers.ListSerializer)
    target = serializer.child if many else serializer
    if many and not _stock_list(serializer):
        return None
    if not isinstance(target, serializers.Serializer):
        # A ``BaseSerializer`` validates with its own ``to_internal_value``.
        return None
    variants = _recognizers.get_or_create(type(target))
    if is_static(target):
        # Its fields are a function of its class: answered once per class,
        # without building them for each instance.
        key = (backend, many, _instance_signature(target))
        try:
            return variants[key]
        except KeyError:
            pass
        if _requires_drf(target):
            return _publish(variants, key, None)
    else:
        if _requires_drf(target):
            return None
        key = (backend, many, _signature(target))
        try:
            return variants[key]
        except KeyError:
            pass
    if len(variants) >= MAX_VARIANTS:
        # Full: do not build a recognizer that could not be kept.
        return None
    try:
        recognizer = Recognizer(
            analyze_input(target, backend=backend), many=many, backend=backend
        )
    except NotCompilable:
        recognizer = None
    return _publish(variants, key, recognizer)


def _publish(
    variants: dict[Any, Any], key: tuple[Any, ...], recognizer: Recognizer | None
) -> Recognizer | None:
    # Built outside the lock; the bound on variants is enforced inside it
    # (``?fields=`` style serializers let clients choose the field set).
    with _lock:
        if key not in variants and len(variants) >= MAX_VARIANTS:
            return None
        return variants.setdefault(key, recognizer)


# Methods DRF's validation calls on a serializer, a list serializer and their
# fields: the hooks refused on their classes, assigned to an instance.
_INSTANCE_HOOKS = (
    *_LIST_HOOKS,
    *(name for pair in _ASYNC_PAIRS for name in pair),
    "get_default",
)


def _requires_drf(serializer: Any) -> bool:
    """Decline instance hooks and unsupported fields before building a cache key."""
    if not vars(serializer).keys().isdisjoint(_INSTANCE_HOOKS):
        return True
    if _dynamic_read_only_default(serializer) is not None:
        return True
    return any(_field_requires_drf(field) for field in serializer._writable_fields)


def _field_requires_drf(field: Any) -> bool:
    if not vars(field).keys().isdisjoint(_INSTANCE_HOOKS):
        return True
    if type(field) in (fields.ListField, fields.DictField, fields.HStoreField):
        return _field_requires_drf(field.child)
    if type(field) in _FIELD_TYPES:
        return False
    nested = field.child if isinstance(field, serializers.ListSerializer) else field
    if isinstance(nested, serializers.BaseSerializer):
        return _requires_drf(nested)
    # Inspect each instance: a dynamic serializer may replace this field.
    return True


def _stock_list(serializer: Any) -> bool:
    return not (
        user_defines(type(serializer), *_LIST_HOOKS)
        or not vars(serializer).keys().isdisjoint(_LIST_HOOKS)
        or serializer.validators
        or not serializer.allow_empty
        or getattr(serializer, "max_length", None) is not None
        or getattr(serializer, "min_length", None) is not None
    )


def _signature(serializer: Any) -> tuple[Any, ...]:
    """
    What :func:`analyze_input` reads from a serializer *instance*. What it
    reads from the class (its methods, class-level ``validate_<field>``
    hooks) is covered by the cache being per class.
    """
    return (
        *_instance_signature(serializer),
        tuple(_field_signature(field) for field in serializer._writable_fields),
    )


def _instance_signature(serializer: Any) -> tuple[bool, bool, bool]:
    """The instance state :func:`analyze_input` reads besides the fields."""
    return (
        bool(serializer.root.partial),
        bool(serializer.validators),
        # Hooks assigned to the instance.
        any(key.startswith(("validate_", "avalidate_")) for key in vars(serializer)),
    )


def _typed(constant: Any) -> object:
    return (
        (type(constant), constant)
        if type(constant) in _CONSTANTS
        else _UNSUPPORTED_CONSTANT
    )


def _field_signature(field: Any) -> tuple[Any, ...]:
    cls = type(field)
    default = field.default
    common = (
        field.field_name,
        cls,
        field.source,
        field.required,
        field.allow_null,
        # With their types: ``True == 1 == 1.0`` as dictionary keys, and DRF
        # returns a default as it is, unconverted.
        fields.empty if default is fields.empty else _typed(default),
        tuple(
            (type(v), _typed(getattr(v, "limit_value", None))) for v in field.validators
        ),
    )
    if cls is fields.CharField:
        return (*common, field.allow_blank, field.trim_whitespace)
    if cls is fields.ChoiceField:
        return (
            *common,
            field.allow_blank,
            tuple((type(key), key) for key in field.choices),
        )
    if cls in (fields.DateField, fields.TimeField):
        # Not given (the setting's) is not given empty.
        formats = getattr(field, "input_formats", None)
        return (*common, None if formats is None else tuple(formats))
    if cls in (fields.ListField, fields.DictField, fields.HStoreField):
        return (*common, field.allow_empty, _field_signature(field.child))
    if isinstance(field, serializers.ListSerializer):
        options = (
            field.allow_empty,
            getattr(field, "min_length", None),
            getattr(field, "max_length", None),
        )
        return (*common, options, _signature(field.child))
    if isinstance(field, serializers.Serializer):
        return (*common, _signature(field))
    return common


def report_input(
    serializer: serializers.BaseSerializer, *, backend: str = "msgspec"
) -> str | None:
    """Return ``None`` if input is recognized for ``serializer``, else the reason."""
    return report_input_details(serializer, backend=backend).reason


def report_input_details(
    serializer: serializers.BaseSerializer, *, backend: str = "msgspec"
) -> Eligibility:
    """Structural input eligibility, with the same stable codes as output."""
    target = serializer
    if isinstance(serializer, serializers.ListSerializer):
        if not _stock_list(serializer):
            return Eligibility(
                "custom_list",
                f"{type(serializer).__qualname__} customizes the validation of the list",
            )
        target = cast(serializers.BaseSerializer, serializer.child)
    try:
        analyze_input(target, backend=backend)
    except NotCompilable as exc:
        return Eligibility(exc.code, str(exc))
    return Eligibility()


def clear_recognizers(*, setting: str, **kwargs: Any) -> None:
    # ``analyze_input`` reads DRF's input formats.
    if setting == "REST_FRAMEWORK":
        _recognizers.clear()


setting_changed.connect(clear_recognizers)
