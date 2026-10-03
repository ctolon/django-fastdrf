"""Serializers validated and represented by a msgspec ``Struct``."""

import copy
import datetime
import decimal
import re
import types
import typing
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import msgspec
from django.db.models import Model
from django.utils.functional import cached_property

from fastdrf.registry import _decoder, _describer, _encoder
from fastdrf.typed import (
    REQUIRED_MESSAGE,
    SCHEMA_CACHE_SIZE,
    BoundedCache,
    FieldSpec,
    SchemaSerializer,
    build_serializer,
    raise_validation_error,
)

__all__ = ["MsgspecBackend", "MsgspecSerializer", "serializer_for"]

# The location runs to the last backtick: a wire name may hold one.
_LOCATION = re.compile(r"^(?P<message>.*?)(?: - at `\$(?P<path>.*)`)?$", re.DOTALL)
_INDEX = re.compile(r"\[(?:(\d+)|\.\.\.)\]")
_NAME = re.compile(r"[^.\[]*")
_MISSING = re.compile(r"^Object missing required field `(?P<name>.*)`$", re.DOTALL)

# Collections of one item type; a fixed tuple (``TupleType``) types each item.
_SEQUENCES = (
    msgspec.inspect.ListType,
    msgspec.inspect.SetType,
    msgspec.inspect.FrozenSetType,
    msgspec.inspect.VarTupleType,
)


def parse_error(
    exc: msgspec.ValidationError, data: Any = None
) -> tuple[tuple[Any, ...], Any, str]:
    """
    Turn a ``msgspec.ValidationError`` into ``(path, message, code)``.

    msgspec stops at the first error and reports it as one string such as
    ``Expected `int`, got `str` - at `$.items[0].pages```. A key may hold
    ``.`` or ``[`` itself: the keys of ``data``, the input, tell which.
    """
    match = _LOCATION.match(str(exc))
    assert match is not None  # the pattern matches any text
    message = match["message"]
    path = _path(match["path"] or "", data)
    missing = _MISSING.match(message)
    if missing:
        return (*path, missing["name"]), REQUIRED_MESSAGE, "required"
    return tuple(path), message, "invalid"


def _path(text: str, data: Any) -> list[Any]:
    path: list[Any] = []
    current = data
    position = 0
    while position < len(text):
        if text[position] == "[":
            index = _INDEX.match(text, position)
            if index is None:
                break
            position = index.end()
            if index[1] is not None:
                path.append(int(index[1]))
                current = (
                    current[int(index[1])]
                    if isinstance(current, (list, tuple))
                    and int(index[1]) < len(current)
                    else None
                )
            continue
        # A ``.``: the longest key of the input the rest starts with, ended
        # where the location ends or goes on; or up to the next separator.
        rest = text[position + 1 :]
        keys = [
            key
            for key in (current if isinstance(current, Mapping) else ())
            if isinstance(key, str)
            and rest.startswith(key)
            and rest[len(key) : len(key) + 1] in ("", ".", "[")
        ]
        key = max(keys, key=len) if keys else _NAME.match(rest)[0]  # type: ignore[index]
        path.append(key)
        current = current.get(key) if isinstance(current, Mapping) else None
        position += 1 + len(key)
    return path


class MsgspecBackend:
    def __init__(
        self,
        *,
        dec_hook: Callable[[type, Any], Any] | None = None,
        enc_hook: Callable[[Any], Any] | None = None,
        schema_hook: Callable[[type], dict[str, Any]] | None = None,
    ) -> None:
        # Each asks the given hook first, then the types registered with
        # ``fastdrf.registry.register_msgspec_type`` (none: msgspec's own).
        self.dec_hook = _decoder(dec_hook)
        self.enc_hook = _encoder(enc_hook)
        self.schema_hook = _describer(schema_hook)

    def validate(
        self, schema: Any, data: Any, *, partial: bool, strict: bool | None
    ) -> Any:
        """``validated_data`` of ``data``: the field values of a loaded ``schema``."""
        return self.values(self.load(schema, data, strict=strict), partial=partial)

    def load(self, schema: Any, data: Any, *, strict: bool | None) -> Any:
        try:
            return msgspec.convert(
                data, schema, strict=strict is not False, dec_hook=self.dec_hook
            )
        except msgspec.ValidationError as exc:
            raise_validation_error([parse_error(exc, data)])

    def values(self, obj: Any, *, partial: bool) -> dict[str, Any]:
        # asdict() is shallow and uses attribute names, not wire aliases.
        # Reading values needs neither type resolution nor recursive conversion.
        values = msgspec.structs.asdict(obj)
        # UNSET is an absent field, also in a full request. Never pass it
        # to create()/update() as a value to write to the model.
        return {
            key: value for key, value in values.items() if value is not msgspec.UNSET
        }

    def from_values(self, schema: Any, values: dict[str, Any]) -> Any:
        return schema(**values)

    def with_values(self, obj: Any, changes: dict[str, Any]) -> Any:
        # A copy, without running __post_init__ again (structs.replace would).
        obj = copy.copy(obj)
        for name, value in changes.items():
            msgspec.structs.force_setattr(obj, name, value)
        return obj

    # ``to_builtins`` encodes a Struct by its runtime class: a subclass
    # instance would bring the subclass's fields. The output is projected on
    # the schema first; exact instances of a schema without nested Structs
    # need nothing.

    def dump(self, schema: Any, instance: Any) -> Any:
        if not isinstance(instance, schema):
            instance = msgspec.convert(
                instance,
                schema,
                from_attributes=not isinstance(instance, dict),
                dec_hook=self.dec_hook,
            )
        if _nests_structs(schema):
            if not (_has_callbacks(schema) and _exact(_struct_info(schema), instance)):
                instance = _project(
                    schema, instance, enc_hook=self.enc_hook, dec_hook=self.dec_hook
                )
        elif type(instance) is not schema:
            instance = msgspec.convert(
                instance, schema, from_attributes=True, dec_hook=self.dec_hook
            )
        return msgspec.to_builtins(instance, enc_hook=self.enc_hook)

    def dump_many(self, schema: type, instances: list[Any]) -> list[Any]:
        if not all(isinstance(instance, schema) for instance in instances):
            from_attributes = not all(
                isinstance(instance, dict) for instance in instances
            )
            instances = msgspec.convert(
                instances,
                list[schema],  # type: ignore[valid-type]  # built at runtime
                from_attributes=from_attributes,
                dec_hook=self.dec_hook,
            )
        if _nests_structs(schema):
            info = _struct_info(schema)
            if not (
                _has_callbacks(schema)
                and all(_exact(info, instance) for instance in instances)
            ):
                instances = _project(
                    list[schema],  # type: ignore[valid-type]  # built at runtime
                    instances,
                    enc_hook=self.enc_hook,
                    dec_hook=self.dec_hook,
                )
        elif any(type(instance) is not schema for instance in instances):
            instances = [
                instance
                if type(instance) is schema
                else msgspec.convert(
                    instance, schema, from_attributes=True, dec_hook=self.dec_hook
                )
                for instance in instances
            ]
        return msgspec.to_builtins(instances, enc_hook=self.enc_hook)

    def dump_partial(self, schema: Any, values: dict[str, Any]) -> Any:
        # The partial copy leaves out what was not given (``UNSET``) and keeps
        # the names and tag; ``dump`` converts nested values to the output's.
        if schema.__struct_config__.array_like:
            raise TypeError(
                f"Cannot represent partial input as {schema.__qualname__}: an "
                "array_like Struct cannot leave fields out. Represent the saved instance."
            )
        return self.dump(_partial(schema), types.SimpleNamespace(**values))

    def lossy_partial(self, schema: Any) -> str | None:
        if getattr(schema, "__post_init__", None) is not None:
            return "defines __post_init__(), which a derived schema would not run"
        if schema.__struct_config__.array_like:
            return (
                "is array_like: a shorter array cannot say which fields it leaves out"
            )
        return None

    def partial_schema(self, schema: Any) -> type:
        return _partial(schema)

    def collection_fields(self, schema: Any) -> set[str]:
        return {
            field.encode_name
            for field in _struct_info(schema).fields
            if _takes_list(field.type)
        }

    def json_schema(
        self, schema: Any, *, ref_prefix: str, direction: str
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """
        The JSON Schema of ``schema`` and of the schemas it refers to, which
        refer to each other through ``ref_prefix`` (an OpenAPI generator's
        components). A Struct is described alike for requests and responses.
        """
        (ref,), components = msgspec.json.schema_components(
            (schema,), ref_template=ref_prefix + "{name}", schema_hook=self.schema_hook
        )
        name = ref["$ref"].rsplit("/", 1)[-1]
        body = components.pop(name)
        return body, components

    def field_specs(self, schema: Any) -> list[FieldSpec]:
        return [_field_spec(field) for field in _struct_info(schema).fields]

    def field_schemas(self, schema: Any) -> dict[str, type | None]:
        """
        Each attribute validation reads (by field name), with the Struct it
        holds: itself, or the items of a collection.
        """
        read: dict[str, type | None] = {}
        for field in _struct_info(schema).fields:
            type_info = _unwrap(field.type)
            if isinstance(type_info, _SEQUENCES):
                type_info = _unwrap(type_info.item_type)
            read[field.name] = (
                type_info.cls
                if isinstance(type_info, msgspec.inspect.StructType)
                else None
            )
        return read


# Kept as they are by the projection's round trip, which ``convert`` reads
# back as themselves.
_NATIVE = (
    datetime.datetime,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    uuid.UUID,
    decimal.Decimal,
    bytes,
    bytearray,
    memoryview,
)


def _project(
    output_type: Any,
    value: Any,
    *,
    enc_hook: Callable[[Any], Any] | None = None,
    dec_hook: Callable[[type, Any], Any] | None = None,
) -> Any:
    """
    ``value`` rebuilt from the fields ``output_type`` declares, at every depth.

    A schema that forbids unknown fields rejects a subclass value that has
    more: that output cannot be projected, and says so.
    """
    try:
        return msgspec.convert(
            msgspec.to_builtins(value, builtin_types=_NATIVE, enc_hook=enc_hook),
            output_type,
            dec_hook=dec_hook,
        )
    except msgspec.ValidationError as exc:
        raise TypeError(
            f"Cannot represent the value as {output_type!r}, its output schema: {exc}"
        ) from exc


def _exact(type_info: msgspec.inspect.Type, value: Any) -> bool:
    """
    Whether every Struct in ``value`` is of the class ``type_info`` declares
    for it: then its output is the schema's without a projection, which
    would build each Struct again and run its ``__post_init__``.
    """
    inspect = msgspec.inspect
    if isinstance(type_info, inspect.Metadata):
        type_info = type_info.type
    if isinstance(value, msgspec.Struct):
        if isinstance(type_info, inspect.UnionType):
            type_info = next(
                (
                    member
                    for member in type_info.types
                    if isinstance(member, inspect.StructType)
                    and member.cls is type(value)
                ),
                type_info,
            )
        if not (
            isinstance(type_info, inspect.StructType) and type(value) is type_info.cls
        ):
            return False
        # Only the fields that may hold a Struct are looked at.
        return all(
            _exact(field_type, getattr(value, name))
            for name, field_type in _nesting_fields(type_info)
        )
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        if isinstance(type_info, inspect.UnionType):
            members = [
                member for member in type_info.types if isinstance(member, _CONTAINERS)
            ]
            if len(members) != 1:
                return False
            type_info = members[0]
        if isinstance(type_info, inspect.DictType) and isinstance(value, dict):
            items: Any = value.values()
            item_type = type_info.value_type
        elif isinstance(type_info, inspect.TupleType) and not isinstance(value, dict):
            return len(value) == len(type_info.item_types) and all(
                _exact(item_type, item)
                for item_type, item in zip(type_info.item_types, value, strict=True)
            )
        else:
            item_type = getattr(type_info, "item_type", None)
            if item_type is None or isinstance(value, dict):
                return isinstance(type_info, inspect.AnyType)
            items = value
        if isinstance(item_type, inspect.StructType) and not _nesting_fields(item_type):
            # Flat Structs: their class is all there is to check.
            cls = item_type.cls
            return all(type(item) is cls for item in items)
        return all(_exact(item_type, item) for item in items)
    # A dataclass or TypedDict value is projected as before.
    return not isinstance(type_info, (inspect.DataclassType, inspect.TypedDictType))


def _nesting_fields(
    type_info: msgspec.inspect.StructType,
) -> tuple[tuple[str, msgspec.inspect.Type], ...]:
    """The ``(name, type)`` of the fields of a Struct that may hold a Struct."""
    return _nesting.get(
        type_info.cls,
        lambda: tuple(
            (field.name, field.type)
            for field in type_info.fields
            if _is_or_holds_struct(field.type)
        ),
    )


_CONTAINERS = (*_SEQUENCES, msgspec.inspect.TupleType, msgspec.inspect.DictType)

_schema_info = BoundedCache(SCHEMA_CACHE_SIZE)
_nesting = BoundedCache(SCHEMA_CACHE_SIZE)


def _nests_structs(schema: type) -> bool:
    return _schema_info.get(schema, lambda: _inspect_schema(schema))[1]


def _has_callbacks(schema: type) -> bool:
    """
    Whether a Struct ``schema`` reaches defines ``__post_init__``, which a
    projection runs again; without one, projecting (in C) is cheaper than
    checking whether it is needed.
    """
    return _schema_info.get(schema, lambda: _inspect_schema(schema))[2]


def _struct_classes(type_info: Any, found: set[type]) -> set[type]:
    inspect = msgspec.inspect
    if isinstance(type_info, inspect.StructType):
        if type_info.cls in found:
            return found
        found.add(type_info.cls)
        for field in type_info.fields:
            _struct_classes(field.type, found)
    elif isinstance(type_info, inspect.Metadata):
        _struct_classes(type_info.type, found)
    elif isinstance(type_info, inspect.UnionType):
        for member in type_info.types:
            _struct_classes(member, found)
    elif isinstance(type_info, inspect.TupleType):
        for item in type_info.item_types:
            _struct_classes(item, found)
    elif isinstance(type_info, inspect.DictType):
        _struct_classes(type_info.value_type, found)
    elif getattr(type_info, "item_type", None) is not None:
        _struct_classes(type_info.item_type, found)
    return found


def _holds_struct(fields: Iterable[msgspec.inspect.Field]) -> bool:
    return any(_is_or_holds_struct(field.type) for field in fields)


def _is_or_holds_struct(type_info: msgspec.inspect.Type) -> bool:
    inspect = msgspec.inspect
    if isinstance(
        type_info, inspect.StructType | inspect.DataclassType | inspect.TypedDictType
    ):
        return True
    if isinstance(type_info, inspect.Metadata):
        return _is_or_holds_struct(type_info.type)
    if isinstance(type_info, inspect.UnionType):
        return any(_is_or_holds_struct(member) for member in type_info.types)
    if isinstance(type_info, inspect.TupleType):
        return any(_is_or_holds_struct(item) for item in type_info.item_types)
    if isinstance(type_info, inspect.DictType):
        return _is_or_holds_struct(type_info.key_type) or _is_or_holds_struct(
            type_info.value_type
        )
    item = getattr(type_info, "item_type", None)
    return item is not None and _is_or_holds_struct(item)


def _struct_info(schema: type) -> msgspec.inspect.StructType:
    return _schema_info.get(schema, lambda: _inspect_schema(schema))[0]


def _inspect_schema(
    schema: type,
) -> tuple[msgspec.inspect.StructType, bool, bool]:
    # Struct field definitions are fixed at class creation. Cache only their
    # metadata, never DRF field instances, hooks or request values.
    info = typing.cast(msgspec.inspect.StructType, msgspec.inspect.type_info(schema))
    callbacks = any(
        getattr(cls, "__post_init__", None) is not None
        for cls in _struct_classes(info, set())
    )
    return info, _holds_struct(info.fields), callbacks


_PYTHON_TYPES: dict[type, type] = {
    msgspec.inspect.StrType: str,
    msgspec.inspect.IntType: int,
    msgspec.inspect.FloatType: float,
    msgspec.inspect.BoolType: bool,
    msgspec.inspect.DateTimeType: datetime.datetime,
    msgspec.inspect.DateType: datetime.date,
    msgspec.inspect.TimeType: datetime.time,
    msgspec.inspect.UUIDType: uuid.UUID,
}


def _takes_list(type_info: msgspec.inspect.Type) -> bool:
    """Whether the type accepts a list: a collection, or a union with one."""
    type_info = _unwrap(type_info)
    if isinstance(type_info, msgspec.inspect.UnionType):
        return any(_takes_list(member) for member in type_info.types)
    return isinstance(type_info, (*_SEQUENCES, msgspec.inspect.TupleType))


def _unwrap(type_info: msgspec.inspect.Type) -> msgspec.inspect.Type:
    """The type behind ``Annotated[...]`` metadata and ``T | None``."""
    if isinstance(type_info, msgspec.inspect.Metadata):
        type_info = type_info.type
    if isinstance(type_info, msgspec.inspect.UnionType):
        # (msgspec leaves ``UNSET`` out of the union it reports.)
        members = [
            t for t in type_info.types if not isinstance(t, msgspec.inspect.NoneType)
        ]
        if len(members) == 1:
            return _unwrap(members[0])
    return type_info


def _field_spec(field: msgspec.inspect.Field) -> FieldSpec:
    type_info = field.type
    schema = {}
    if isinstance(type_info, msgspec.inspect.Metadata):
        schema = type_info.extra_json_schema or {}
    type_info = _unwrap(type_info)
    return FieldSpec(
        field.encode_name,
        _PYTHON_TYPES.get(type(type_info)),
        title=schema.get("title"),
        description=schema.get("description"),
        attribute=field.name,
    )


_partial_schemas = BoundedCache(SCHEMA_CACHE_SIZE)


def _partial(schema: type) -> type:
    """A copy of ``schema`` where every field defaults to ``UNSET`` (for PATCH)."""
    return _partial_schemas.get(schema, lambda: _build_partial(schema))


def _build_partial(schema: Any) -> type:
    # The configuration that decides what input is valid, besides the
    # fields: names, unknown fields and the tag, which is no field. The rest
    # is about Python objects or defaults; ``array_like`` is refused.
    config = schema.__struct_config__
    fields = [
        (field.name, field.type, msgspec.UNSET)
        for field in msgspec.structs.fields(schema)
    ]
    return msgspec.defstruct(
        f"Patched{schema.__name__}",
        fields,
        kw_only=True,
        rename={f.name: f.encode_name for f in msgspec.structs.fields(schema)},
        forbid_unknown_fields=config.forbid_unknown_fields,
        tag_field=config.tag_field,
        tag=config.tag,
        module=schema.__module__,
    )


class MsgspecSerializer(SchemaSerializer):
    """
    A serializer whose validation and representation are done by a msgspec
    ``Struct`` given as ``Meta.schema`` (or ``Meta.input_schema`` /
    ``Meta.output_schema``).

    ``validated_data`` is a dict of the Struct's fields, so ``save()`` and
    ``create(validated_data)`` work as with DRF serializers. Set
    ``Meta.strict = False`` to accept strings for numbers and booleans in
    JSON input, like DRF does.

    ``Meta.dec_hook``, ``Meta.enc_hook`` and ``Meta.schema_hook`` handle
    custom types for validation, representation and the JSON Schema
    (``backend.json_schema``) respectively.
    """

    schema_library = "msgspec"

    @cached_property
    def backend(self) -> MsgspecBackend:
        meta = getattr(self, "Meta", None)
        return MsgspecBackend(
            dec_hook=getattr(meta, "dec_hook", None),
            enc_hook=getattr(meta, "enc_hook", None),
            schema_hook=getattr(meta, "schema_hook", None),
        )


def serializer_for(
    schema: object,
    output_schema: type | None = None,
    model: type[Model] | None = None,
    *,
    base: type | None = None,
) -> type | None:
    """A ``MsgspecSerializer`` for a Struct (or an input and an output Struct)."""
    if not (isinstance(schema, type) and issubclass(schema, msgspec.Struct)):
        return None
    # ``base``: a package's subclass of MsgspecSerializer (aiodrf's is asynchronous).
    return build_serializer(base or MsgspecSerializer, schema, output_schema, model)
