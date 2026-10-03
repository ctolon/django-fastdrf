"""
Source code conversion between DRF serializers and msgspec / pydantic
schemas, for ``manage.py fastdrf_convert``.

The functions here return Python source; they write nothing. A serializer
is read from an instance (its ``fields``), a schema from its class. Both are
turned into a small description (:class:`Schema`, :class:`Spec`, :class:`T`)
that the emitters render, so each direction is one reader and one writer.

What cannot be expressed is never guessed: the field becomes ``Any`` (or
``serializers.JSONField()``) and a ``# TODO(convert): ...`` comment
says what was not converted. Validation hooks (``validate_<field>``,
pydantic validators, ``__post_init__``) are listed the same way. The output
is meant to be read and finished by a person, not used unread.

msgspec and pydantic are imported only to read a class of that library;
writing their source needs neither.
"""

import builtins
import copy
import datetime
import decimal
import enum
import keyword
import math
import re
import types
import typing
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from fastdrf.utils import is_framework_class

# The readers and writers. ``Schema``, ``Spec`` and ``T``, the description
# between them, are internal.
__all__ = [
    "from_msgspec",
    "from_pydantic",
    "from_serializer",
    "library_of",
    "to_drf",
    "to_msgspec",
    "to_pydantic",
]

TODO = "TODO(convert):"
LINE_LENGTH = 88
_NO_DEFAULT = object()


# -- The description both directions share -----------------------------------


@dataclass(frozen=True)
class T:
    """
    A type: ``kind`` is a scalar (``str``, ``int``, ``float``, ``bool``,
    ``decimal``, ``datetime``, ``date``, ``time``, ``uuid``, ``any``) or
    ``literal``, ``list``, ``dict``, ``ref``.
    """

    kind: str
    item: "T | None" = None
    values: tuple[Any, ...] = ()
    ref: "Schema | None" = None
    constraints: tuple[tuple[str, Any], ...] = ()
    nullable: bool = False


@dataclass(eq=False)
class Spec:
    """One field. ``required=False`` without a default means "may be absent"."""

    name: str
    type: T
    required: bool = True
    default: Any = _NO_DEFAULT
    source: str | None = None
    # Accepted, never represented (pydantic's ``exclude=True``).
    write_only: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(eq=False)
class Schema:
    name: str
    fields: list[Spec] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# The names a generated module refers to besides its classes: builtins,
# its imports and the typing names it uses.
_GENERATED_REFERENCES = frozenset(
    {
        *dir(builtins),
        "Annotated",
        "Any",
        "BaseModel",
        "ConfigDict",
        "Field",
        "Literal",
        "datetime",
        "decimal",
        "msgspec",
        "serializers",
        "uuid",
    }
)


class _Names:
    """Unique class names, in the order the classes must be emitted."""

    def __init__(self) -> None:
        self.schemas: list[Schema] = []
        # A class must not shadow what the generated module refers to.
        self.taken: set[str] = set(_GENERATED_REFERENCES)
        self.by_key: dict[Any, Schema] = {}
        # DRF serializers being read, by structure: a recursive one meets
        # itself (a cycle).
        self.reading: dict[Any, Schema] = {}
        self.recursive: set[int] = set()
        # Finished DRF serializers' classes, by serializer class and
        # direction: one class per contract (fields, types, constraints).
        self.read: dict[Any, list[Schema]] = {}

    def new(self, key: Any, name: str) -> Schema:
        unique, number = name, 2
        while unique in self.taken:
            unique, number = f"{name}{number}", number + 1
        self.taken.add(unique)
        schema = Schema(unique)
        self.by_key[key] = schema
        return schema

    def done(self, schema: Schema) -> None:
        self.schemas.append(schema)

    def release(self, schema: Schema) -> None:
        """Give up a class read again: its name is free."""
        self.taken.discard(schema.name)


# -- Reading a DRF serializer ------------------------------------------------

# Collected into one note per class.
_TRIMS = "trims whitespace"


def from_serializer(serializer: Any, name: str | None = None) -> list[Schema]:
    """
    Describe a DRF serializer instance. Returns the schemas in dependency
    order; the last one is the serializer itself, or its ``In`` and ``Out``
    pair when it has read-only or write-only fields (then the last two).
    """
    names = _Names()
    base = name or _base_name(type(serializer).__name__)
    if _splits(serializer):
        _read_serializer(serializer, "in", names, f"{base}In")
        _read_serializer(serializer, "out", names, f"{base}Out")
    else:
        _read_serializer(serializer, "both", names, base)
    return names.schemas


def _base_name(class_name: str) -> str:
    base = class_name.removesuffix("Serializer")
    return base or class_name


def _splits(serializer: Any, seen: set[int] | None = None) -> bool:
    """Whether it or a nested serializer has read-only or write-only fields."""
    from rest_framework.fields import DictField, ListField

    seen = set() if seen is None else seen
    if id(serializer) in seen:
        return False
    seen.add(id(serializer))
    for drf_field in serializer.fields.values():
        if drf_field.read_only or drf_field.write_only:
            return True
        while isinstance(drf_field, ListField | DictField):
            drf_field = drf_field.child
        nested = _nested(drf_field)
        if nested is not None and _splits(nested, seen):
            return True
    return False


def _nested(drf_field: Any) -> Any:
    from rest_framework.serializers import BaseSerializer, ListSerializer

    if isinstance(drf_field, ListSerializer):
        return drf_field.child
    if isinstance(drf_field, BaseSerializer):
        return drf_field
    return None


def _read_serializer(
    serializer: Any, direction: str, names: _Names, name: str
) -> Schema:
    # One class per contract: instances of one serializer class whose fields
    # differ in an option (a max_length) get classes of their own. A
    # serializer that nests itself meets its own reading: the cycle.
    key = (
        type(serializer),
        direction,
        tuple((key, type(value)) for key, value in serializer.fields.items()),
    )
    if key in names.reading:
        names.recursive.add(id(names.reading[key]))
        return names.reading[key]
    schema = names.new(key, name)
    names.reading[key] = schema
    try:
        _read_fields(serializer, direction, names, schema)
    finally:
        del names.reading[key]
    same = (type(serializer), direction)
    if id(schema) not in names.recursive:
        for other in names.read.get(same, ()):
            if _contract(other) == _contract(schema):
                names.release(schema)
                return other
    names.read.setdefault(same, []).append(schema)
    names.done(schema)
    return schema


def _contract(schema: Schema) -> tuple[Any, ...]:
    """What a written class says: its fields as written, and its notes."""
    return (
        [
            (
                spec.name,
                spec.type,
                spec.required,
                spec.default,
                spec.source,
                spec.write_only,
                spec.notes,
            )
            for spec in schema.fields
        ],
        schema.notes,
    )


def _read_fields(
    serializer: Any, direction: str, names: _Names, schema: Schema
) -> None:
    from rest_framework.fields import HiddenField, SerializerMethodField

    trimmed = []
    for field_name, drf_field in serializer.fields.items():
        if direction == "in" and drf_field.read_only:
            continue
        if direction == "out" and drf_field.write_only:
            continue
        if isinstance(drf_field, HiddenField):
            schema.notes.append(
                f"{TODO} HiddenField {field_name!r} is set by the view, not sent."
            )
            continue
        notes: list[str] = []
        type_ = _read_drf_type(
            drf_field, serializer, direction, names, notes, field_name
        )
        if _TRIMS in notes:
            trimmed.append(field_name)
            notes = [note for note in notes if note != _TRIMS]
        spec = Spec(field_name, type_, notes=notes)
        if drf_field.source not in (None, field_name) and not isinstance(
            drf_field, SerializerMethodField
        ):
            source = drf_field.source
            if source.isidentifier() and not keyword.iskeyword(source):
                spec.source = source
            else:
                notes.append(f"{TODO} source={source!r} is not converted.")
        if direction != "out":
            _read_drf_presence(drf_field, spec)
        _note_validators(drf_field, spec)
        schema.fields.append(spec)
    for hook in _serializer_hooks(serializer):
        where = (
            f"{type(serializer).__name__}'s {hook}"
            if hook.startswith("validator ")
            else f"{type(serializer).__name__}.{hook}"
        )
        schema.notes.append(f"{TODO} {where} is not converted.")
    if trimmed and direction != "out":
        schema.notes.append(
            f"DRF strips surrounding whitespace from {', '.join(trimmed)} before validating;"
            " these fields do not."
        )


def _read_drf_presence(drf_field: Any, spec: Spec) -> None:
    from rest_framework.fields import empty

    if drf_field.default is not empty:
        if callable(drf_field.default):
            spec.notes.append(f"{TODO} the callable default is not converted.")
            spec.required = False
        else:
            spec.required = False
            spec.default = drf_field.default
    elif not drf_field.required:
        spec.required = False


def _note_validators(drf_field: Any, spec: Spec) -> None:
    built = _built_validators(drf_field)
    for validator in getattr(drf_field, "validators", ()):
        match = next((b for b in built if _same_validator(validator, b)), None)
        if match is not None:
            # DRF built it from an option (``max_length``, ``EmailField``...),
            # which the reader above converts or marks.
            built.remove(match)
            continue
        name = type(validator).__name__
        if name == "function":
            name = getattr(validator, "__name__", repr(validator))
        spec.notes.append(f"{TODO} validator {name} is not converted.")


def _built_validators(drf_field: Any) -> list[Any]:
    """The validators the field builds from its own options, without ``validators=``."""
    from rest_framework.fields import Field
    from rest_framework.serializers import BaseSerializer

    if isinstance(drf_field, BaseSerializer):
        return []
    kwargs = {
        # A bound ``child`` cannot be given again; DRF's copy is unbound.
        key: copy.deepcopy(value) if isinstance(value, Field) else value
        for key, value in getattr(drf_field, "_kwargs", {}).items()
        if key != "validators"
    }
    try:
        bare = type(drf_field)(*getattr(drf_field, "_args", ()), **kwargs)
    except Exception:  # noqa: BLE001 -- then every validator is marked
        return []
    return list(bare.validators)


def _same_validator(validator: Any, built: Any) -> bool:
    if type(validator) is not type(built):
        return False
    # Django's validators compare their messages too, which DRF builds anew.
    for attribute in ("limit_value", "inverse_match", "flags", "schemes"):
        if getattr(validator, attribute, None) != getattr(built, attribute, None):
            return False
    regex = getattr(validator, "regex", None)
    return getattr(regex, "pattern", regex) == getattr(
        getattr(built, "regex", None), "pattern", getattr(built, "regex", None)
    )


def _serializer_hooks(serializer: Any) -> list[str]:
    hooks: list[str] = []
    for klass in type(serializer).__mro__:
        # DRF's classes, and those of the packages built on it (fastdrf's
        # serializers, or another package's registered bases).
        if is_framework_class(klass):
            continue
        hooks.extend(
            f"{attr}()"
            for attr in sorted(vars(klass))
            if attr in ("validate", "to_internal_value", "run_validation")
            or attr.startswith("validate_")
        )
    # ``Meta.validators``, or those ModelSerializer derives from the model's
    # unique constraints.
    hooks.extend(
        f"validator {type(validator).__name__}" for validator in serializer.validators
    )
    return list(dict.fromkeys(hooks))


def _read_drf_type(
    drf_field: Any,
    serializer: Any,
    direction: str,
    names: _Names,
    notes: list[str],
    field_name: str,
) -> T:
    from rest_framework import fields as f
    from rest_framework import relations

    nullable = bool(getattr(drf_field, "allow_null", False))
    nested = _nested(drf_field)
    if nested is not None:
        many = nested is not drf_field
        variant = direction if _splits(nested) else "both"
        suffix = {"in": "In", "out": "Out", "both": ""}[variant]
        schema = _read_serializer(
            nested, variant, names, f"{_base_name(type(nested).__name__)}{suffix}"
        )
        if not many:
            return T("ref", ref=schema, nullable=nullable)
        # The list and its items allow null apart (``many=True`` gives both).
        ref = T("ref", ref=schema, nullable=bool(getattr(nested, "allow_null", False)))
        constraints: Any = _length(
            drf_field, allow_empty=getattr(drf_field, "allow_empty", True)
        )
        return T("list", item=ref, constraints=constraints, nullable=nullable)

    kind = type(drf_field)
    if kind in (f.CharField, f.EmailField, f.URLField, f.SlugField, f.RegexField):
        return _read_text(drf_field, notes, nullable)
    if kind in (f.IntegerField, f.FloatField):
        scalar = "int" if kind is f.IntegerField else "float"
        return T(scalar, constraints=_bounds(drf_field), nullable=nullable)
    if kind is f.DecimalField:
        notes.append(
            f"{TODO} DRF counts trailing decimal zeros in precision checks; "
            "pydantic does not. Decimal output quantization is not converted."
        )
        constraints = dict(_bounds(drf_field))
        for option in ("max_digits", "decimal_places"):
            if getattr(drf_field, option) is not None:
                constraints[option] = getattr(drf_field, option)
        return T("decimal", constraints=tuple(constraints.items()), nullable=nullable)
    simple = {
        f.BooleanField: "bool",
        f.DateTimeField: "datetime",
        f.DateField: "date",
        f.TimeField: "time",
        f.UUIDField: "uuid",
        f.JSONField: "any",
    }
    if kind in simple:
        if kind in (f.DateField, f.TimeField, f.DateTimeField):
            _note_temporal_formats(drf_field, direction, notes)
        elif kind is f.JSONField and not nullable:
            notes.append(f"{TODO} Any accepts null; this JSONField does not.")
        return T(simple[kind], nullable=nullable)
    if kind is f.ChoiceField:
        values = tuple(drf_field.choices)
        if drf_field.allow_blank and "" not in values:
            values = (*values, "")
        return T("literal", values=values, nullable=nullable)
    if kind is f.MultipleChoiceField:
        choices = tuple(drf_field.choices)
        if drf_field.allow_blank and "" not in choices:
            choices = (*choices, "")
        item = T("literal", values=choices)
        constraints = () if drf_field.allow_empty else (("min_length", 1),)
        notes.append("DRF returns a set; this is a list.")
        return T("list", item=item, constraints=constraints, nullable=nullable)
    if kind in (f.ListField, f.DictField):
        item = _read_drf_type(
            drf_field.child, serializer, direction, names, notes, field_name
        )
        if kind is f.ListField:
            constraints = _length(drf_field, allow_empty=drf_field.allow_empty)
            return T("list", item=item, constraints=constraints, nullable=nullable)
        constraints = () if drf_field.allow_empty else (("min_length", 1),)
        return T("dict", item=item, constraints=constraints, nullable=nullable)
    if kind is relations.PrimaryKeyRelatedField:
        return _pk_type(drf_field, serializer, field_name, notes, nullable)
    if kind is relations.ManyRelatedField and type(drf_field.child_relation) is (
        relations.PrimaryKeyRelatedField
    ):
        item = _pk_type(drf_field.child_relation, serializer, field_name, notes, False)
        constraints = () if drf_field.allow_empty else (("min_length", 1),)
        return T("list", item=item, constraints=constraints, nullable=nullable)
    notes.append(f"{TODO} {kind.__name__} is not converted.")
    return T("any")


def _note_temporal_formats(field: Any, direction: str, notes: list[str]) -> None:
    from rest_framework import ISO_8601, fields
    from rest_framework.settings import api_settings

    prefix = {
        fields.DateField: "DATE",
        fields.TimeField: "TIME",
        fields.DateTimeField: "DATETIME",
    }[type(field)]
    if direction != "out":
        formats = getattr(
            field, "input_formats", getattr(api_settings, prefix + "_INPUT_FORMATS")
        )
        if list(formats) != [ISO_8601]:
            notes.append(f"{TODO} input_formats={formats!r} is not converted.")
    if direction != "in":
        format_ = getattr(field, "format", getattr(api_settings, prefix + "_FORMAT"))
        if format_ != ISO_8601:
            notes.append(f"{TODO} format={format_!r} is not converted.")


def _read_text(drf_field: Any, notes: list[str], nullable: bool) -> T:
    from rest_framework import fields as f

    kind = type(drf_field)
    constraints = dict(_length(drf_field, allow_empty=drf_field.allow_blank))
    if kind in (f.SlugField, f.RegexField):
        # DRF's own regex (a Unicode slug's too), with its flags.
        pattern = _regex_of(drf_field)
        if pattern is not None:
            constraints["pattern"] = pattern
    elif kind in (f.EmailField, f.URLField):
        notes.append(f"{TODO} {kind.__name__} format validation is not converted.")
    if drf_field.allow_blank:
        _allow_blank(constraints, notes)
    if drf_field.trim_whitespace:
        notes.append(_TRIMS)
    return T("str", constraints=tuple(constraints.items()), nullable=nullable)


def _allow_blank(constraints: dict[str, Any], notes: list[str]) -> None:
    # DRF returns "" before it runs any validator.
    min_length = constraints.pop("min_length", None)
    if min_length:
        notes.append(
            f"{TODO} min_length={min_length} applies to non-blank values only; "
            "it is not converted."
        )
    pattern = constraints.get("pattern")
    if pattern is not None:
        # Inline flags must lead the pattern: the blank alternative follows.
        flags = _INLINE_FLAGS.match(pattern)
        lead = flags[0] if flags else ""
        constraints["pattern"] = f"{lead}^$|{pattern[len(lead) :]}"


# ``re`` flags as inline flags; ``re.UNICODE`` is a str pattern's default.
_FLAG_LETTERS = (
    (re.ASCII, "a"),
    (re.IGNORECASE, "i"),
    (re.MULTILINE, "m"),
    (re.DOTALL, "s"),
    (re.VERBOSE, "x"),
)
_INLINE_FLAGS = re.compile(r"\(\?[aimsx]+\)")


def _regex_of(drf_field: Any) -> str | None:
    """The pattern of the field's regex validator, its flags inline."""
    for validator in drf_field.validators:
        regex = getattr(validator, "regex", None)
        if regex is None:
            continue
        if isinstance(regex, str):
            return regex
        letters = "".join(
            letter for flag, letter in _FLAG_LETTERS if regex.flags & flag
        )
        return f"(?{letters}){regex.pattern}" if letters else regex.pattern
    return None


def _length(drf_field: Any, *, allow_empty: bool = True) -> tuple[tuple[str, Any], ...]:
    constraints = {}
    min_length = getattr(drf_field, "min_length", None)
    if not allow_empty:
        min_length = max(min_length or 0, 1)
    if min_length:
        constraints["min_length"] = min_length
    max_length = getattr(drf_field, "max_length", None)
    if max_length is not None:
        constraints["max_length"] = max_length
    return tuple(constraints.items())


def _bounds(drf_field: Any) -> tuple[tuple[str, Any], ...]:
    constraints = []
    if getattr(drf_field, "min_value", None) is not None:
        constraints.append(("ge", drf_field.min_value))
    if getattr(drf_field, "max_value", None) is not None:
        constraints.append(("le", drf_field.max_value))
    return tuple(constraints)


_PK_TYPES = {
    "AutoField": "int",
    "BigAutoField": "int",
    "SmallAutoField": "int",
    "IntegerField": "int",
    "BigIntegerField": "int",
    "SmallIntegerField": "int",
    "PositiveIntegerField": "int",
    "PositiveBigIntegerField": "int",
    "PositiveSmallIntegerField": "int",
    "UUIDField": "uuid",
    "CharField": "str",
    "SlugField": "str",
}


def _pk_type(
    drf_field: Any, serializer: Any, field_name: str, notes: list[str], nullable: bool
) -> T:
    model = None
    if drf_field.queryset is not None:
        model = drf_field.queryset.model
    else:
        parent_model = getattr(getattr(serializer, "Meta", None), "model", None)
        source = drf_field.source or field_name
        if parent_model is not None and "." not in source:
            try:
                model = parent_model._meta.get_field(source).related_model
            except Exception:  # noqa: BLE001 -- any lookup failure is reported as a TODO
                model = None
    if model is not None:
        kind = _PK_TYPES.get(type(model._meta.pk).__name__)
        if kind is not None:
            return T(kind, nullable=nullable)
    notes.append(f"{TODO} the type of this primary key is not known.")
    return T("any", nullable=nullable)


# -- Reading a pydantic model or a msgspec Struct ----------------------------


def from_pydantic(model: Any, name: str | None = None) -> list[Schema]:
    """Describe a pydantic model; returns schemas in dependency order."""
    names = _Names()
    _read_pydantic(model, names, name or model.__name__)
    return names.schemas


def _read_pydantic(model: Any, names: _Names, name: str) -> Schema:
    from pydantic_core import PydanticUndefined

    if model in names.by_key:
        return names.by_key[model]
    schema = names.new(model, name)
    for attr, info in model.model_fields.items():
        notes: list[str] = []
        type_ = _read_annotation(
            info.annotation, info.metadata, names, notes, "pydantic"
        )
        type_ = _with_model_text_limits(type_, model.model_config)
        spec = Spec(attr, type_, notes=notes)
        validates_default = (
            info.validate_default
            if info.validate_default is not None
            else model.model_config.get("validate_default", False)
        )
        if not info.is_required() and validates_default:
            notes.append(
                f"{TODO} validate_default=True is not converted; DRF does not validate defaults."
            )
        alias = (
            info.validation_alias
            if isinstance(info.validation_alias, str)
            else info.alias
        )
        if info.validation_alias is not None and not isinstance(
            info.validation_alias, str
        ):
            notes.append(
                f"{TODO} the validation alias {info.validation_alias!r} is not converted."
            )
        _rename(spec, attr, alias)
        _read_pydantic_output(info, spec, notes, alias or attr)
        if info.default_factory is not None:
            _factory_default(spec, info.default_factory)
        elif info.default is not PydanticUndefined:
            spec.required = False
            spec.default = info.default
        schema.fields.append(spec)
    decorators = model.__pydantic_decorators__
    for kind in (
        "field_validators",
        "model_validators",
        "root_validators",
        "validators",
        "field_serializers",
        "model_serializers",
    ):
        for decorator_name in sorted(getattr(decorators, kind, {})):
            schema.notes.append(
                f"{TODO} {model.__name__}.{decorator_name}() is not converted."
            )
    for computed in sorted(decorators.computed_fields):
        schema.notes.append(f"{TODO} the computed field {computed!r} is not converted.")
    hook = _project_hook(model, "model_post_init")
    if hook is not None:
        schema.notes.append(f"{TODO} {hook}.model_post_init() is not converted.")
    for option in ("str_strip_whitespace", "str_to_lower", "str_to_upper"):
        if model.model_config.get(option):
            schema.notes.append(f"{TODO} the model's {option} is not converted.")
    if model.model_config.get("extra") == "forbid":
        schema.notes.append(
            f"{TODO} unknown fields are refused by pydantic; DRF ignores them."
        )
    elif model.model_config.get("extra") == "allow":
        schema.notes.append(
            f"{TODO} extra='allow' is not converted; DRF discards extra fields."
        )
    names.done(schema)
    return schema


def _read_pydantic_output(info: Any, spec: Spec, notes: list[str], name: str) -> None:
    # What the model leaves out of its output, or renames there, must not
    # become an ordinary readable field of the serializer.
    if info.exclude is True:
        spec.write_only = True
    elif info.exclude:
        notes.append(f"{TODO} exclude={info.exclude!r} is not converted.")
    if getattr(info, "exclude_if", None) is not None:
        notes.append(
            f"{TODO} exclude_if is not converted; the field is always represented."
        )
    if info.serialization_alias is not None and info.serialization_alias != name:
        notes.append(
            f"{TODO} the output name {info.serialization_alias!r} is not converted; "
            f"DRF represents it as {name!r}."
        )


def from_msgspec(struct: Any, name: str | None = None) -> list[Schema]:
    """Describe a msgspec Struct; returns schemas in dependency order."""
    names = _Names()
    _read_msgspec(struct, names, name or struct.__name__)
    return names.schemas


def _read_msgspec(struct: Any, names: _Names, name: str) -> Schema:
    import msgspec

    if struct in names.by_key:
        return names.by_key[struct]
    schema = names.new(struct, name)
    hints = typing.get_type_hints(struct, include_extras=True)
    for info in msgspec.structs.fields(struct):
        notes: list[str] = []
        type_ = _read_annotation(
            hints.get(info.name, info.type), (), names, notes, "msgspec"
        )
        spec = Spec(info.name, type_, notes=notes)
        _rename(spec, info.name, info.encode_name)
        if info.default_factory is not msgspec.NODEFAULT:
            _factory_default(spec, info.default_factory)
        elif info.default is not msgspec.NODEFAULT:
            spec.required = False
            if info.default is not msgspec.UNSET:
                spec.default = info.default
        if _is_unset(type_):
            spec.required = False
        schema.fields.append(spec)
    hook = _project_hook(struct, "__post_init__")
    if hook is not None:
        schema.notes.append(f"{TODO} {hook}.__post_init__() is not converted.")
    config = struct.__struct_config__
    if config.omit_defaults:
        schema.notes.append(
            f"{TODO} omit_defaults=True is not converted; DRF represents default values."
        )
    if config.tag is not None:
        schema.notes.append(
            f"{TODO} the tag {config.tag_field}={config.tag!r} is not converted: "
            "msgspec checks it in input and writes it in output."
        )
    if config.array_like:
        schema.notes.append(
            f"{TODO} array_like is not converted: msgspec reads and writes "
            f"{struct.__name__} as an array of its fields."
        )
    if config.forbid_unknown_fields:
        schema.notes.append(
            f"{TODO} unknown fields are refused by msgspec; DRF ignores them."
        )
    names.done(schema)
    return schema


def _project_hook(cls: type, name: str) -> str | None:
    """
    The name of the class that defines the hook ``name`` that ``cls`` runs,
    inherited too, when it is not the library's (pydantic sets its own
    ``model_post_init`` on a model with private attributes).
    """
    for klass in cls.__mro__:
        if name in vars(klass):
            function = vars(klass)[name]
            module = getattr(function, "__module__", "") or ""
            if module.split(".", 1)[0] in ("pydantic", "msgspec"):
                return None
            return klass.__name__
    return None


def _with_model_text_limits(type_: T, config: Any) -> T:
    """
    ``type_`` with the model's ``str_min_length`` and ``str_max_length``
    on each string it holds that does not set its own.
    """
    limits = {
        constraint: config.get(option)
        for constraint, option in (
            ("min_length", "str_min_length"),
            ("max_length", "str_max_length"),
        )
        if config.get(option) is not None
    }
    if not limits:
        return type_
    if type_.kind == "str":
        own = dict(type_.constraints)
        return _with(type_, constraints=tuple({**limits, **own}.items()))
    if type_.item is not None:
        return _with(type_, item=_with_model_text_limits(type_.item, config))
    return type_


def _factory_default(spec: Spec, factory: Callable[[], Any]) -> None:
    spec.required = False
    if factory in (list, dict):
        spec.default = factory()  # an empty collection, written as one
    else:
        spec.notes.append(
            f"{TODO} default_factory {_type_name(factory)} is not converted."
        )


def _rename(spec: Spec, attr: str, wire_name: str | None) -> None:
    if wire_name is None or wire_name == attr:
        return
    if wire_name.isidentifier() and not keyword.iskeyword(wire_name):
        spec.name, spec.source = wire_name, attr
    else:
        spec.notes.append(f"{TODO} the wire name {wire_name!r} is not a Python name.")


_UNSET_MARK = ("unset", True)
_SCALARS = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    decimal.Decimal: "decimal",
    datetime.datetime: "datetime",
    datetime.date: "date",
    datetime.time: "time",
    uuid.UUID: "uuid",
    Any: "any",
}
_CONSTRAINT_NAMES = (
    "min_length",
    "max_length",
    "ge",
    "le",
    "gt",
    "lt",
    "multiple_of",
    "pattern",
    "max_digits",
    "decimal_places",
)


# What metadata may carry besides constraints without changing validation.
_DOCUMENTATION = ("title", "description", "examples", "extra_json_schema", "extra")


def _constraints_of(metadata: Iterable[Any], notes: list[str]) -> dict[str, Any]:
    found = {}
    for item in metadata:
        nested = getattr(item, "metadata", None)
        if isinstance(nested, list):  # a pydantic FieldInfo inside Annotated
            found.update(_constraints_of(nested, notes))
            continue
        options = {
            option: getattr(item, option)
            for option in _CONSTRAINT_NAMES
            if getattr(item, option, None) is not None
        }
        found.update(options)
        if type(item).__module__.split(".", 1)[0] == "msgspec":
            # ``msgspec.Meta``: constraints, documentation, and ``tz``.
            settings = {
                name: getattr(item, name)
                for name in dir(item)
                if not name.startswith("_")
                and name not in (*_CONSTRAINT_NAMES, *_DOCUMENTATION)
                and getattr(item, name) is not None
            }
            if settings:
                given = ", ".join(f"{key}={value!r}" for key, value in settings.items())
                notes.append(f"{TODO} msgspec.Meta({given}) is not converted.")
        elif not options:
            # A validator, ``Strict()``, anything that is not a known constraint.
            notes.append(f"{TODO} {_type_name(type(item))} is not converted.")
        else:
            # Constraints beside other settings (``StringConstraints``).
            settings = {
                name: getattr(item, name)
                for name in getattr(item, "__dataclass_fields__", ())
                if name not in (*_CONSTRAINT_NAMES, *_DOCUMENTATION)
                and getattr(item, name) is not None
            }
            if settings:
                given = ", ".join(f"{key}={value!r}" for key, value in settings.items())
                notes.append(
                    f"{TODO} {_type_name(type(item))}({given}) is not converted."
                )
    return found


def _is_unset(type_: T) -> bool:
    return dict(type_.constraints).get("unset", False)


def _read_annotation(
    annotation: Any,
    metadata: Iterable[Any],
    names: _Names,
    notes: list[str],
    library: str,
) -> T:
    constraints = _constraints_of(metadata, notes)
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        base, *extra = typing.get_args(annotation)
        inner = _read_annotation(base, extra, names, notes, library)
        merged = {**dict(inner.constraints), **constraints}
        return _with(inner, constraints=tuple(merged.items()))
    if origin in (typing.Union, types.UnionType):
        members = list(typing.get_args(annotation))
        nullable = type(None) in members
        unset = False
        rest = []
        for member in members:
            if member is type(None):
                continue
            if (
                getattr(member, "__name__", "") == "UnsetType"
                and member.__module__ == "msgspec"
            ):
                unset = True
                continue
            rest.append(member)
        if len(rest) != 1:
            notes.append(f"{TODO} the union {annotation!r} is not converted.")
            return T("any", nullable=nullable)
        inner = _read_annotation(rest[0], metadata, names, notes, library)
        unset_mark = (_UNSET_MARK,) if unset else ()
        return _with(
            inner,
            nullable=inner.nullable or nullable,
            constraints=inner.constraints + unset_mark,
        )
    if annotation in _SCALARS:
        return T(
            _SCALARS[annotation],
            constraints=tuple(constraints.items()),
            nullable=annotation is Any,
        )
    if origin is typing.Literal:
        values = typing.get_args(annotation)
        # ``None`` among the members is null, which DRF allows apart.
        return T(
            "literal",
            values=tuple(value for value in values if value is not None),
            nullable=None in values,
            constraints=tuple(constraints.items()),
        )
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        notes.append(
            f"{annotation.__name__} members by value; DRF returns the raw value."
        )
        return T(
            "literal",
            values=tuple(member.value for member in annotation),
            constraints=tuple(constraints.items()),
        )
    if origin is list:
        (item,) = typing.get_args(annotation) or (Any,)
        return T(
            "list",
            item=_read_annotation(item, (), names, notes, library),
            constraints=tuple(constraints.items()),
        )
    if origin is dict:
        key, value = typing.get_args(annotation) or (str, Any)
        if key is str:
            return T(
                "dict",
                item=_read_annotation(value, (), names, notes, library),
                constraints=tuple(constraints.items()),
            )
    if isinstance(annotation, type) and library_of(annotation) == library:
        reader = _read_pydantic if library == "pydantic" else _read_msgspec
        return T("ref", ref=reader(annotation, names, annotation.__name__))
    notes.append(f"{TODO} {_type_name(annotation)} is not converted.")
    return T("any")


def library_of(cls: type) -> str | None:
    for klass in cls.__mro__:
        root = klass.__module__.split(".", 1)[0]
        if (root, klass.__name__) in (("pydantic", "BaseModel"), ("msgspec", "Struct")):
            return root
    return None


def _type_name(annotation: Any) -> str:
    if typing.get_origin(annotation) is None and hasattr(annotation, "__name__"):
        return annotation.__name__
    return repr(annotation)


def _with(type_: T, **changes: Any) -> T:
    values: dict[str, Any] = {
        "kind": type_.kind,
        "item": type_.item,
        "values": type_.values,
        "ref": type_.ref,
        "constraints": type_.constraints,
        "nullable": type_.nullable,
    }
    values.update(changes)
    return T(**values)


# -- Writing source ----------------------------------------------------------


class _Imports:
    def __init__(self) -> None:
        self.modules: set[str] = set()
        self.typing: set[str] = set()
        self.third_party: list[str] = []

    def lines(self) -> str:
        stdlib = [f"import {module}" for module in sorted(self.modules)]
        if self.typing:
            stdlib.append(f"from typing import {', '.join(sorted(self.typing))}")
        blocks = [block for block in (stdlib, self.third_party) if block]
        return "\n\n".join("\n".join(block) for block in blocks)


def _literal(value: Any, imports: _Imports) -> str | None:
    """Python source for a constant, or None when it has none."""
    if isinstance(value, enum.Enum):  # before int: an IntEnum is an int
        return _literal(value.value, imports)
    if isinstance(value, float) and not math.isfinite(value):
        return f'float("{value}")'
    if value is None or isinstance(value, bool | int | float):
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, decimal.Decimal):
        imports.modules.add("decimal")
        return f"decimal.Decimal({_string(str(value))})"
    if isinstance(value, list | tuple):
        items = [_literal(item, imports) for item in value]
        if None in items:
            return None
        body = ", ".join(typing.cast(list[str], items))
        return (
            f"[{body}]"
            if isinstance(value, list)
            else f"({body}{',' if len(items) == 1 else ''})"
        )
    if isinstance(value, dict):
        pairs = [
            (_literal(key, imports), _literal(item, imports))
            for key, item in value.items()
        ]
        if any(key is None or item is None for key, item in pairs):
            return None
        return "{" + ", ".join(f"{key}: {item}" for key, item in pairs) + "}"
    return None


def _string(value: str) -> str:
    if not value.isprintable():
        return repr(value)
    if '"' in value and "'" not in value:
        return "'" + value.replace("\\", "\\\\") + "'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _wrap(indent: str, text: str) -> list[str]:
    """Lines of ``text`` as ruff format breaks them (brackets at the end only)."""
    if len(indent) + len(text) <= LINE_LENGTH or text[-1:] not in ")]":
        return [indent + text]
    opening = _matching_open(text)
    head, body, close = text[: opening + 1], text[opening + 1 : -1], text[-1]
    inner = indent + "    "
    if not body:
        return [indent + text]
    if len(inner) + len(body) <= LINE_LENGTH:
        return [indent + head, inner + body, indent + close]
    parts = _split_commas(body)
    if len(parts) == 1:
        return [indent + head, *_wrap(inner, body), indent + close]
    lines = [indent + head]
    for part in parts:
        wrapped = (
            _wrap(inner, part + ",") if part[-1:] not in ")]" else _wrap(inner, part)
        )
        if part[-1:] in ")]":
            wrapped[-1] += ","
        lines.extend(wrapped)
    lines.append(indent + close)
    return lines


def _scan(text: str) -> Iterator[tuple[int, str, int]]:
    """Yield ``(index, char, depth)`` outside string literals."""
    depth, quote, escaped = 0, None, False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        yield index, char, depth


def _matching_open(text: str) -> int:
    stack = []
    for index, char, _depth in _scan(text):
        if char in "([{":
            stack.append(index)
        elif char in ")]}":
            opening = stack.pop()
            if index == len(text) - 1:
                return opening
    raise ValueError(text)


def _split_commas(body: str) -> list[str]:
    parts, start = [], 0
    for index, char, depth in _scan(body):
        if char == "," and depth == 0:
            parts.append(body[start:index].strip())
            start = index + 1
    tail = body[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _module(source_path: str, imports: _Imports, blocks: list[str]) -> str:
    header = f'"""Generated by the serializer converter from ``{source_path}``."""'
    # One blank line after the docstring, and after the imports when an
    # assignment follows them; two before and between top-level blocks.
    after_imports = "\n\n\n" if blocks[0].startswith("class ") else "\n\n"
    return (
        f"{header}\n\n{imports.lines()}{after_imports}" + "\n\n\n".join(blocks) + "\n"
    )


_SCHEMA_SCALARS = {
    "str": "str",
    "int": "int",
    "float": "float",
    "bool": "bool",
    "decimal": "decimal.Decimal",
    "datetime": "datetime.datetime",
    "date": "datetime.date",
    "time": "datetime.time",
    "uuid": "uuid.UUID",
    "any": "Any",
}
_MSGSPEC_META = (
    "gt",
    "ge",
    "lt",
    "le",
    "multiple_of",
    "pattern",
    "min_length",
    "max_length",
)


def to_pydantic(schemas: Iterable[Schema], source_path: str) -> str:
    """pydantic source for the described schemas."""
    return _to_schema_source(schemas, source_path, "pydantic")


def to_msgspec(schemas: Iterable[Schema], source_path: str) -> str:
    """msgspec source for the described schemas."""
    return _to_schema_source(schemas, source_path, "msgspec")


def _to_schema_source(schemas: Iterable[Schema], source_path: str, library: str) -> str:
    imports = _Imports()
    imports.third_party.append(
        "from pydantic import BaseModel, Field"
        if library == "pydantic"
        else "import msgspec"
    )
    schemas = list(schemas)
    names = {*_GENERATED_REFERENCES, *(schema.name for schema in schemas)}
    references = {
        *(value.split(".")[0] for value in _SCHEMA_SCALARS.values()),
        *(schema.name for schema in schemas),
        "list",
        "dict",
        "Annotated",
        "Literal",
        "Field",
        "BaseModel",
        "ConfigDict",
        "msgspec",
    }
    blocks = []
    uses_field = uses_config = False
    for schema in schemas:
        aliases, body = [], [f"    # {note}" for note in schema.notes]
        kw_only = seen_default = False
        attributes: set[str] = set()
        taken = (
            {spec.name for spec in schema.fields}
            | {spec.source for spec in schema.fields if spec.source}
            | names
        )
        for spec in schema.fields:
            default, type_, notes = _schema_default(spec, imports, library)
            has_default = default is not None
            annotation = _annotation(type_, imports, library, notes)
            attribute = spec.source or spec.name
            if attribute in references or (
                library == "pydantic" and attribute.startswith(("_", "model_"))
            ):
                candidate = (attribute.lstrip("_") or "field") + "_"
                if candidate.startswith("model_"):
                    candidate = "field_" + candidate
                attribute = _unused(candidate, taken)
            if not attribute.isidentifier() or keyword.iskeyword(attribute):
                attribute = _unused("field_", taken)
            if attribute in attributes:
                notes.append(f"{TODO} shared source={spec.source!r} is not converted.")
                attribute = _unused(attribute + "_", taken)
            attributes.add(attribute)
            if attribute != spec.name:
                wire = _string(spec.name)
                if library == "pydantic":
                    given = f"{default}, " if default is not None else ""
                    default = f"Field({given}alias={wire})"
                elif default is not None and default.startswith("msgspec.field("):
                    default = default[:-1] + f", name={wire})"
                else:
                    given = f", default={default}" if default is not None else ""
                    default = f"msgspec.field(name={wire}{given})"
            uses_field = (
                uses_field or "Field(" in annotation or "Field(" in (default or "")
            )
            if not has_default and seen_default and library == "msgspec":
                kw_only = True
            seen_default = seen_default or has_default
            assigned = f" = {default}" if default is not None else ""
            line = f"{attribute}: {annotation}{assigned}"
            if len("    " + line) > LINE_LENGTH:
                core, rest = _split_annotation(annotation)
                if core.endswith("]"):
                    # The alias is read inside the class: neither existing nor
                    # later field attributes may shadow its module-level name.
                    alias = _unused(f"{schema.name}{_pascal(attribute)}", names | taken)
                    names.add(alias)
                    taken.add(alias)
                    aliases.extend(_wrap("", f"{alias} = {core}"))
                    line = f"{attribute}: {alias}{rest}{assigned}"
            body.extend(f"    # {note}" for note in [*spec.notes, *notes])
            body.append(f"    {line}")
        base = "BaseModel" if library == "pydantic" else "msgspec.Struct"
        if kw_only:
            base += ", kw_only=True"
        if library == "pydantic":
            options = []
            if any(_has_pattern(spec.type) for spec in schema.fields):
                options.append('regex_engine="python-re"')
            if any(spec.source for spec in schema.fields):
                # Attributes used by output need to be readable by name too.
                options.append("populate_by_name=True")
                body.insert(
                    0, "    # Input accepts attribute names as well as wire aliases."
                )
            if options:
                uses_config = True
                body.insert(0, f"    model_config = ConfigDict({', '.join(options)})")
        if not schema.fields:
            # Notes are comments: the class still needs a statement.
            body.append("    pass")
        block = f"class {schema.name}({base}):\n" + "\n".join(body)
        if aliases:
            block = "\n".join(aliases) + "\n\n\n" + block
        blocks.append(block)
    if library == "pydantic":
        used = ["BaseModel"]
        if uses_config:
            used.append("ConfigDict")
        if uses_field:
            used.append("Field")
        imports.third_party[0] = f"from pydantic import {', '.join(used)}"
    return _module(source_path, imports, blocks)


def _unused(name: str, names: set[str]) -> str:
    unique, number = name, 2
    while unique in names:
        unique, number = f"{name}{number}", number + 1
    names.add(unique)
    return unique


def _has_pattern(type_: T | None) -> bool:
    return type_ is not None and (
        any(name == "pattern" for name, _ in type_.constraints)
        or _has_pattern(type_.item)
    )


def _pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _split_annotation(annotation: str) -> tuple[str, str]:
    """``Core[...] | None`` -> (``Core[...]``, `` | None``)."""
    for suffix in (" | msgspec.UnsetType | None", " | msgspec.UnsetType", " | None"):
        if annotation.endswith(suffix):
            return annotation[: -len(suffix)], suffix
    return annotation, ""


def _annotation(type_: T, imports: _Imports, library: str, notes: list[str]) -> str:
    text = _core_annotation(type_, imports, library, notes)
    if _is_unset(type_):
        text += " | msgspec.UnsetType"
    if type_.nullable:
        text += " | None"
    return text


def _core_annotation(
    # A ``T``; its kind decides which of ``item`` and ``ref`` is set.
    type_: Any,
    imports: _Imports,
    library: str,
    notes: list[str],
) -> str:
    kind = type_.kind
    if kind in _SCHEMA_SCALARS:
        text = _SCHEMA_SCALARS[kind]
        module = text.split(".", 1)[0] if "." in text else None
        if module:
            imports.modules.add(module)
        if text == "Any":
            imports.typing.add("Any")
    elif kind == "literal":
        if library == "msgspec" and any(
            type(value) not in (str, int)
            or (type(value) is int and not -(2**63) <= value < 2**63)
            for value in type_.values
        ):
            # The supported msgspec versions share string/integer Literal
            # support (signed 64-bit integers), not float or boolean Literals.
            notes.append(
                f"{TODO} these choices are not supported by msgspec Literal: {type_.values!r}."
            )
            choices = None
        else:
            choices = _choices(type_, imports, notes)
        if choices is None:
            imports.typing.add("Any")
            text = "Any"
        else:
            imports.typing.add("Literal")
            text = f"Literal[{choices}]"
    elif kind == "list":
        text = f"list[{_item_annotation(type_.item, imports, library, notes)}]"
    elif kind == "dict":
        text = f"dict[str, {_item_annotation(type_.item, imports, library, notes)}]"
    else:
        text = type_.ref.name
    constraints = [(key, value) for key, value in type_.constraints if key != "unset"]
    if library == "msgspec":
        lost = [key for key, _ in constraints if key not in _MSGSPEC_META]
        if lost:
            notes.append(
                f"{TODO} {', '.join(lost)} cannot be expressed with msgspec.Meta."
            )
        constraints = [
            (key, value) for key, value in constraints if key in _MSGSPEC_META
        ]
    if constraints:
        imports.typing.add("Annotated")
        options = ", ".join(
            f"{key}={_literal(value, imports)}" for key, value in constraints
        )
        marker = "Field" if library == "pydantic" else "msgspec.Meta"
        text = f"Annotated[{text}, {marker}({options})]"
    return text


def _choices(type_: T, imports: _Imports, notes: list[str]) -> str | None:
    """The literal's members as source, or None (noted) when one has none."""
    if not type_.values:
        # Choices filled at run time are declared empty; nothing is guessed.
        notes.append(f"{TODO} the field has no choices; DRF refuses every value.")
        return None
    members = [_literal(value, imports) for value in type_.values]
    if None in members:
        notes.append(f"{TODO} the choices {type_.values!r} are not converted.")
        return None
    return ", ".join(typing.cast(list[str], members))


def _item_annotation(
    type_: T, imports: _Imports, library: str, notes: list[str]
) -> str:
    text = _core_annotation(type_, imports, library, notes)
    return text + " | None" if type_.nullable else text


def _schema_default(
    spec: Spec, imports: _Imports, library: str
) -> tuple[str | None, T, list[str]]:
    """``(default source or None, the type to write, notes)``."""
    type_ = spec.type
    if library == "pydantic" and _is_unset(type_):
        type_ = _with(
            type_, constraints=tuple(c for c in type_.constraints if c != _UNSET_MARK)
        )
    if spec.required:
        return None, type_, []
    if spec.default is not _NO_DEFAULT:
        value = _literal(spec.default, imports)
        if value is not None:
            if (
                library == "msgspec"
                and isinstance(spec.default, list | dict)
                and spec.default
            ):
                value = f"msgspec.field(default_factory=lambda: {value})"
            return value, type_, []
        notes = [f"{TODO} the default {spec.default!r} is not converted."]
    else:
        notes = []
    if library == "msgspec":
        if not _is_unset(type_):
            type_ = _with(type_, constraints=(*type_.constraints, _UNSET_MARK))
        return "msgspec.UNSET", type_, notes
    if not type_.nullable:
        type_ = _with(type_, nullable=True)
        notes.append("DRF: optional but not nullable; None stands for a missing value.")
    return "None", type_, notes


def to_drf(schemas: Iterable[Schema], source_path: str) -> str:
    """DRF serializer source for the described schemas."""
    imports = _Imports()
    imports.third_party.append("from rest_framework import serializers")
    blocks = []
    written: set[str] = set()
    for schema in schemas:
        body = [f"    # {note}" for note in schema.notes]
        for spec in schema.fields:
            notes = list(spec.notes)
            expression = _drf_field(spec.type, imports, notes, written, top=spec)
            body.extend(f"    # {note}" for note in notes)
            body.extend(_wrap("    ", f"{spec.name} = {expression}"))
        if not schema.fields:
            # Notes are comments: the class still needs a statement.
            body.append("    pass")
        blocks.append(
            f"class {schema.name}Serializer(serializers.Serializer):\n"
            + "\n".join(body)
        )
        written.add(schema.name)
    return _module(source_path, imports, blocks)


def _drf_field(
    # A ``T``; its kind decides which of ``item`` and ``ref`` is set.
    type_: Any,
    imports: _Imports,
    notes: list[str],
    written: set[str],
    top: Spec | None = None,
) -> str:
    options: list[tuple[str, str | None]] = []
    constraints = dict(type_.constraints)
    constraints.pop("unset", None)
    kind = type_.kind
    if _unwritten_ref(type_, notes, written):
        kind = "any"
    if kind == "ref":
        name = f"{type_.ref.name}Serializer"
    elif (
        kind == "list"
        and type_.item.kind == "ref"
        and type_.nullable
        and not type_.item.nullable
    ):
        # ``many=True`` would pass ``allow_null`` to the items too.
        name = "serializers.ListSerializer"
        options.append(("child", f"{type_.item.ref.name}Serializer()"))
        options.extend(_drf_lengths(constraints))
    elif kind == "list" and type_.item.kind == "ref":
        name = f"{type_.item.ref.name}Serializer"
        options.append(("many", "True"))
        options.extend(_drf_lengths(constraints))
        if type_.item.nullable and not type_.nullable:
            # DRF passes ``allow_null`` to the list and its child alike.
            notes.append(
                f"{TODO} null items are not converted; allow_null=True would "
                "also accept a null list."
            )
    elif kind == "str":
        name = (
            "serializers.RegexField"
            if "pattern" in constraints
            else "serializers.CharField"
        )
        blank = not constraints.get("min_length")
        if "pattern" in constraints:
            pattern = constraints.pop("pattern")
            try:
                accepts_blank = re.search(pattern, "") is not None
            except re.error:
                name = "serializers.CharField"
                notes.append(
                    f"{TODO} pattern={pattern!r} is not supported by Python re."
                )
            else:
                options.append(("regex", _string(pattern)))
                blank = blank and accepts_blank
        options.extend(_drf_lengths(constraints))
        if blank:
            options.append(("allow_blank", "True"))
        options.append(("trim_whitespace", "False"))
    elif kind in ("int", "float", "decimal"):
        name = {
            "int": "serializers.IntegerField",
            "float": "serializers.FloatField",
            "decimal": "serializers.DecimalField",
        }[kind]
        if kind == "decimal":
            if "max_digits" in constraints or "decimal_places" in constraints:
                notes.append(
                    f"{TODO} DRF counts trailing decimal zeros in precision checks; pydantic does not."
                )
            options.extend(
                (option, repr(constraints.pop(option, None)))
                for option in ("max_digits", "decimal_places")
            )
        options.extend(_drf_bounds(constraints, kind, imports))
    elif kind == "literal" and not type_.values and type_.nullable:
        # ``Literal[None]``: null only.
        name = "serializers.ChoiceField"
        options.append(("choices", "[]"))
    elif kind == "literal" and (
        choices := _choices(_chosen(type_, constraints), imports, notes)
    ):
        name = "serializers.ChoiceField"
        options.append(("choices", f"[{choices}]"))
    elif kind == "list":
        name = "serializers.ListField"
        options.append(("child", _drf_field(type_.item, imports, notes, written)))
        options.extend(_drf_lengths(constraints))
    elif kind == "dict":
        name = "serializers.DictField"
        options.append(("child", _drf_field(type_.item, imports, notes, written)))
        # ``allow_empty=False`` is a minimum of one item, no more.
        if constraints.get("min_length", 0) <= 1 and constraints.pop("min_length", 0):
            options.append(("allow_empty", "False"))
    else:
        name = {
            "bool": "serializers.BooleanField",
            "datetime": "serializers.DateTimeField",
            "date": "serializers.DateField",
            "time": "serializers.TimeField",
            "uuid": "serializers.UUIDField",
            "any": "serializers.JSONField",
            "literal": "serializers.JSONField",  # choices without source
        }[kind]
    notes.extend(
        f"{TODO} the constraint {leftover} is not converted."
        for leftover in constraints
    )
    if type_.nullable:
        options.append(("allow_null", "True"))
    if top is not None:
        options.extend(_drf_presence(top, imports, notes))
    return f"{name}({', '.join(f'{key}={value}' for key, value in options)})"


_CHOICE_CHECKS: dict[str, Callable[[Any, Any], bool]] = {
    "min_length": lambda value, limit: len(value) >= limit,
    "max_length": lambda value, limit: len(value) <= limit,
    "ge": lambda value, limit: value >= limit,
    "gt": lambda value, limit: value > limit,
    "le": lambda value, limit: value <= limit,
    "lt": lambda value, limit: value < limit,
    "multiple_of": lambda value, limit: value % limit == 0,
    "pattern": lambda value, limit: re.search(limit, value) is not None,
}


def _chosen(type_: T, constraints: dict[str, Any]) -> T:
    """
    The literal ``type_`` with the members its constraints allow: a choice
    either passes a constraint or not. The constraints checked are removed
    from ``constraints``; one a member cannot be checked against stays.
    """
    values = type_.values
    for name in [name for name in constraints if name in _CHOICE_CHECKS]:
        check, limit = _CHOICE_CHECKS[name], constraints[name]
        try:
            values = tuple(value for value in values if check(value, limit))
        except (TypeError, re.error):
            continue
        del constraints[name]
    return _with(type_, values=values)


def _unwritten_ref(type_: T, notes: list[str], written: set[str]) -> bool:
    """Whether a class body would name its own class or one written after it."""
    item = type_.item if type_.kind == "list" else None
    ref = item.ref if item is not None and item.kind == "ref" else type_.ref
    if ref is None or ref.name in written:
        return False
    notes.append(f"{TODO} the recursive reference to {ref.name} is not converted.")
    return True


def _drf_presence(
    spec: Spec, imports: _Imports, notes: list[str]
) -> list[tuple[str, str]]:
    options = []
    if spec.source:
        options.append(("source", _string(spec.source)))
    if spec.write_only:
        options.append(("write_only", "True"))
    if not spec.required:
        options.append(("required", "False"))
        if spec.default is not _NO_DEFAULT:
            value = _drf_default(spec.default, imports)
            if value is None:
                notes.append(f"{TODO} the default {spec.default!r} is not converted.")
            else:
                options.append(("default", value))
    return options


def _drf_lengths(constraints: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (option, repr(constraints.pop(option)))
        for option in ("max_length", "min_length")
        if option in constraints
    ]


def _drf_default(value: Any, imports: _Imports) -> str | None:
    # DRF hands the default itself to every request: a mutable one is shared.
    if isinstance(value, list | dict):
        source = _literal(value, imports)
        if source is None:
            return None
        return type(value).__name__ if not value else f"lambda: {source}"
    return _literal(value, imports)


def _drf_bounds(
    constraints: dict[str, Any], kind: str, imports: _Imports
) -> list[tuple[str, str | None]]:
    # Exclusive bounds are exact only for integers.
    options = []
    if "ge" in constraints:
        options.append(("min_value", _literal(constraints.pop("ge"), imports)))
    elif kind == "int" and "gt" in constraints:
        options.append(("min_value", repr(constraints.pop("gt") + 1)))
    if "le" in constraints:
        options.append(("max_value", _literal(constraints.pop("le"), imports)))
    elif kind == "int" and "lt" in constraints:
        options.append(("max_value", repr(constraints.pop("lt") - 1)))
    return options
