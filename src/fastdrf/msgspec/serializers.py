"""Serializers validated and represented by a msgspec ``Struct``."""

import datetime
import decimal
import re
import types
import typing
import uuid
from collections.abc import Callable, Iterable
from typing import Any

import msgspec
from django.db.models import Model
from django.utils.functional import cached_property

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

_LOCATION = re.compile(r"^(?P<message>.*?)(?: - at `\$(?P<path>[^`]*)`)?$", re.DOTALL)
_PATH_TOKEN = re.compile(r"\.([^.\[]+)|\[(\d+)\]|\[\.\.\.\]")
_MISSING = re.compile(r"^Object missing required field `(?P<name>[^`]+)`$")


def parse_error(exc: msgspec.ValidationError) -> tuple[tuple[Any, ...], Any, str]:
    """
    Turn a ``msgspec.ValidationError`` into ``(path, message, code)``.

    msgspec stops at the first error and reports it as one string such as
    ``Expected `int`, got `str` - at `$.items[0].pages```.
    """
    match = _LOCATION.match(str(exc))
    assert match is not None  # the pattern matches any text
    message = match["message"]
    path: list[Any] = []
    for name, index in _PATH_TOKEN.findall(match["path"] or ""):
        if name:
            path.append(name)
        elif index:
            path.append(int(index))
    missing = _MISSING.match(message)
    if missing:
        return (*path, missing["name"]), REQUIRED_MESSAGE, "required"
    return tuple(path), message, "invalid"


class MsgspecBackend:
    def __init__(
        self,
        *,
        dec_hook: Callable[[type, Any], Any] | None = None,
        enc_hook: Callable[[Any], Any] | None = None,
    ) -> None:
        self.dec_hook = dec_hook
        self.enc_hook = enc_hook

    def load(self, schema: Any, data: Any, *, strict: bool) -> Any:
        try:
            return msgspec.convert(data, schema, strict=strict, dec_hook=self.dec_hook)
        except msgspec.ValidationError as exc:
            raise_validation_error([parse_error(exc)])

    def values(self, obj: Any, *, partial: bool) -> dict[str, Any]:
        # asdict() is shallow and uses attribute names, not wire aliases.
        # Reading values needs neither type resolution nor recursive conversion.
        values = msgspec.structs.asdict(obj)
        if partial:
            values = {
                key: value
                for key, value in values.items()
                if value is not msgspec.UNSET
            }
        return values

    def from_values(self, schema: Any, values: dict[str, Any]) -> Any:
        return schema(**values)

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
        collections = (
            msgspec.inspect.ListType,
            msgspec.inspect.SetType,
            msgspec.inspect.FrozenSetType,
            msgspec.inspect.VarTupleType,
            msgspec.inspect.TupleType,
        )
        return {
            field.encode_name
            for field in _struct_info(schema).fields
            if isinstance(_unwrap(field.type), collections)
        }

    def field_specs(self, schema: Any) -> list[FieldSpec]:
        return [_field_spec(field) for field in _struct_info(schema).fields]


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


_schema_info = BoundedCache(SCHEMA_CACHE_SIZE)


def _nests_structs(schema: type) -> bool:
    return _schema_info.get(schema, lambda: _inspect_schema(schema))[1]


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


def _inspect_schema(schema: type) -> tuple[msgspec.inspect.StructType, bool]:
    # Struct field definitions are fixed at class creation. Cache only their
    # metadata, never DRF field instances, hooks or request values.
    info = typing.cast(msgspec.inspect.StructType, msgspec.inspect.type_info(schema))
    return info, _holds_struct(info.fields)


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

    ``Meta.dec_hook`` and ``Meta.enc_hook`` convert custom types for
    validation and representation.
    """

    schema_library = "msgspec"

    @cached_property
    def backend(self) -> MsgspecBackend:
        meta = getattr(self, "Meta", None)
        return MsgspecBackend(
            dec_hook=getattr(meta, "dec_hook", None),
            enc_hook=getattr(meta, "enc_hook", None),
        )


def serializer_for(
    schema: object,
    output_schema: type | None = None,
    model: type[Model] | None = None,
) -> type | None:
    """A ``MsgspecSerializer`` for a Struct (or an input and an output Struct)."""
    if not (isinstance(schema, type) and issubclass(schema, msgspec.Struct)):
        return None
    return build_serializer(MsgspecSerializer, schema, output_schema, model)
