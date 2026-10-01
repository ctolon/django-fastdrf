"""Serializers validated and represented by a pydantic ``BaseModel``."""

import datetime
import types
import typing
import uuid
from collections.abc import Iterator
from typing import Any

import pydantic
from django.db.models import Model
from django.utils.functional import cached_property
from pydantic.fields import FieldInfo

from fastdrf.typed import (
    REQUIRED_MESSAGE,
    SCHEMA_CACHE_SIZE,
    BoundedCache,
    FieldSpec,
    SchemaSerializer,
    build_serializer,
    raise_validation_error,
)

__all__ = ["PydanticBackend", "PydanticSerializer", "serializer_for"]

_SIMPLE_TYPES = (
    str,
    int,
    float,
    bool,
    datetime.datetime,
    datetime.date,
    datetime.time,
    uuid.UUID,
)


def parse_errors(
    exc: pydantic.ValidationError, data: Any = None, schema: Any = None
) -> Iterator[tuple[tuple[Any, ...], Any, str]]:
    """
    Turn a ``pydantic.ValidationError`` into ``(path, message, code)`` triples.

    pydantic's location also names what it tried (a union's member,
    ``[key]`` for a mapping's key); DRF's error keys are the input's own. The
    path keeps the segments found in ``data``; a missing field keeps its name.
    A missing field of ``schema`` read through an ``AliasPath`` is reported
    where the input stops: at ``payload`` when ``payload`` itself is absent.
    """
    alias_paths = _alias_paths(schema)
    for error in exc.errors(include_url=False, include_input=False):
        if error["type"] == "missing":
            loc = tuple(error["loc"])
            yield (
                _missing_path(loc, data)
                if loc in alias_paths
                else _input_path(loc, data, keep_last=True),
                REQUIRED_MESSAGE,
                "required",
            )
        else:
            yield _input_path(error["loc"], data), error["msg"], error["type"]


def _alias_paths(schema: Any) -> set[tuple[Any, ...]]:
    """The input paths of the fields of ``schema`` read through an ``AliasPath``."""
    paths: set[tuple[Any, ...]] = set()
    for field in getattr(schema, "model_fields", {}).values():
        alias = field.validation_alias
        choices = alias.choices if isinstance(alias, pydantic.AliasChoices) else [alias]
        paths.update(
            tuple(choice.path)
            for choice in choices
            if isinstance(choice, pydantic.AliasPath) and len(choice.path) > 1
        )
    return paths


def _missing_path(loc: tuple[Any, ...], data: Any) -> tuple[Any, ...]:
    current = data
    for index, segment in enumerate(loc):
        found, current = _child(current, segment)
        if not found:
            return loc[: index + 1]
    return loc


def _input_path(
    loc: tuple[Any, ...], data: Any, keep_last: bool = False
) -> tuple[Any, ...]:
    path = []
    current = data
    for index, segment in enumerate(loc):
        found, current = _child(current, segment)
        if found or (keep_last and index == len(loc) - 1 and isinstance(segment, str)):
            path.append(segment)
    return tuple(path)


def _child(value: Any, segment: Any) -> tuple[bool, Any]:
    if isinstance(value, dict) and segment in value:
        return True, value[segment]
    if (
        isinstance(value, dict)
        and isinstance(segment, str)
        and hasattr(value, "getlist")
    ):
        return segment in value, value.get(segment)
    if (
        isinstance(value, (list, tuple))
        and isinstance(segment, int)
        and -len(value) <= segment < len(value)
    ):
        return True, value[segment]
    return False, value


class _Unset:
    """Default of every field of a partial model; never validated."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


class PydanticBackend:
    def __init__(self, *, context: Any = None) -> None:
        self.context = context

    def load(self, schema: Any, data: Any, *, strict: bool) -> Any:
        try:
            return schema.model_validate(
                data, strict=strict or None, context=self.context
            )
        except pydantic.ValidationError as exc:
            raise_validation_error(list(parse_errors(exc, data, schema)))

    def values(self, obj: Any, *, partial: bool) -> dict[str, Any]:
        if partial:
            return {name: getattr(obj, name) for name in obj.model_fields_set}
        values = {name: getattr(obj, name) for name in type(obj).model_fields}
        if obj.model_extra:
            values.update(obj.model_extra)
        return values

    def from_values(self, schema: Any, values: dict[str, Any]) -> Any:
        return schema.model_construct(**values)

    # Output uses serialization aliases: the names the model's serialization
    # schema (``model_json_schema(mode="serialization")``) documents.

    def dump(self, schema: Any, instance: Any) -> Any:
        if not isinstance(instance, schema):
            instance = schema.model_validate(
                instance, from_attributes=True, context=self.context
            )
        # The schema's serializer, not the instance's model_dump(): a subclass
        # instance is dumped with the schema's fields only, as in dump_many.
        return schema.__pydantic_serializer__.to_python(
            instance, mode="json", by_alias=True, context=self.context
        )

    def dump_many(self, schema: Any, instances: list[Any]) -> list[Any]:
        adapter = _list_adapter(schema)
        # Through the adapter's SchemaSerializer, which accepts ``context`` on
        # every supported pydantic version.
        return adapter.serializer.to_python(
            adapter.validate_python(
                instances, from_attributes=True, context=self.context
            ),
            mode="json",
            by_alias=True,
            context=self.context,
        )

    def dump_partial(self, schema: Any, values: dict[str, Any]) -> Any:
        # The given fields only, through the schema's own serializer, so its
        # field serializers, aliases and exclusions apply. A model serializer
        # is written for a whole object, which partial input is not.
        if schema.__pydantic_decorators__.model_serializers:
            raise TypeError(
                f"Cannot represent partial input as {schema.__qualname__}: its "
                "model_serializer represents whole objects. Represent the saved instance."
            )
        given = {
            name: value
            for name, value in values.items()
            if name in schema.model_fields
            or schema.model_config.get("extra") == "allow"
        }
        return schema.__pydantic_serializer__.to_python(
            schema.model_construct(**given),
            mode="json",
            by_alias=True,
            include=set(given),
            context=self.context,
        )

    def lossy_partial(self, schema: Any) -> str | None:
        if schema.__pydantic_root_model__:
            return "is a RootModel, which has no field-level partial update contract"
        decorators = schema.__pydantic_decorators__
        if (
            decorators.field_validators
            or decorators.model_validators
            or decorators.validators
        ):
            reason = "has validators"
        elif decorators.root_validators:
            reason = "has root validators"
        elif schema.model_post_init is not pydantic.BaseModel.model_post_init:
            reason = "defines model_post_init()"
        elif schema.model_config.get("validate_default") or any(
            field.validate_default for field in schema.model_fields.values()
        ):
            reason = "validates defaults"
        else:
            return None
        return f"{reason}, which a derived schema would not run"

    def partial_schema(self, schema: Any) -> type:
        return _partial(schema)

    def collection_fields(self, schema: Any) -> set[Any]:
        names = set()
        for name, field in schema.model_fields.items():
            annotation = _non_optional(field.annotation)
            if typing.get_origin(annotation) in (list, set, frozenset, tuple):
                alias = field.validation_alias or field.alias or name
                choices = (
                    alias.choices
                    if isinstance(alias, pydantic.AliasChoices)
                    else [alias]
                )
                for choice in choices:
                    if isinstance(choice, pydantic.AliasPath) and len(choice.path) == 1:
                        names.add(choice.path[0])
                    elif isinstance(choice, str):
                        names.add(choice)
                if schema.model_config.get(
                    "validate_by_name",
                    schema.model_config.get("populate_by_name", False),
                ):
                    names.add(name)
        return names

    def field_specs(self, schema: Any) -> list[FieldSpec]:
        return [
            FieldSpec(
                field.serialization_alias or field.alias or name,
                _simple_type(field.annotation),
                title=field.title,
                description=field.description,
                attribute=name,
            )
            for name, field in schema.model_fields.items()
            # Not in the output, so not a field of it.
            if field.exclude is not True
        ]


def _non_optional(annotation: Any) -> Any:
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        members = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        if len(members) == 1:
            return members[0]
    return annotation


def _simple_type(annotation: Any) -> type | None:
    annotation = _non_optional(annotation)
    return annotation if annotation in _SIMPLE_TYPES else None


_list_adapters = BoundedCache(SCHEMA_CACHE_SIZE)
_partial_schemas = BoundedCache(SCHEMA_CACHE_SIZE)


def _list_adapter(schema: type) -> Any:
    return _list_adapters.get(
        schema,
        lambda: pydantic.TypeAdapter(list[schema]),  # type: ignore[valid-type]  # built at runtime
    )


def _partial(schema: type) -> type:
    """A subclass of ``schema`` where every field defaults to ``UNSET`` (for PATCH)."""
    return _partial_schemas.get(schema, lambda: _build_partial(schema))


def _build_partial(schema: Any) -> type:
    fields = {}
    for name, field in schema.model_fields.items():
        partial_field = FieldInfo.merge_field_infos(field, default=UNSET)
        fields[name] = (field.annotation, partial_field)
    # Fields known only at runtime: none of create_model's overloads apply.
    return pydantic.create_model(  # type: ignore[call-overload]
        f"Patched{schema.__name__}", __base__=schema, **fields
    )


class PydanticSerializer(SchemaSerializer):
    """
    A serializer whose validation and representation are done by a pydantic
    model given as ``Meta.schema`` (or ``Meta.input_schema`` /
    ``Meta.output_schema``).

    ``validated_data`` is a dict of the model's fields. Representation reads
    attributes (``from_attributes``), so model instances can be serialized
    directly. ``Meta.strict = True`` turns on pydantic's strict mode; by
    default pydantic's lax mode applies. Its coercion rules are Pydantic's,
    not those of DRF fields.

    DRF serializer context is passed to the model's validation and serialization
    callbacks. RootModel and custom model_serializer outputs keep their shape.
    """

    schema_library = "pydantic"

    @cached_property
    def backend(self) -> PydanticBackend:
        # Bound after DRF assigns the parent, so nested serializers inherit
        # the root context. Never cache request context alongside schema classes.
        return PydanticBackend(context=self.context)

    def _strict(self) -> bool:
        return getattr(getattr(self, "Meta", None), "strict", False)


def serializer_for(
    schema: object,
    output_schema: type | None = None,
    model: type[Model] | None = None,
) -> type | None:
    """A ``PydanticSerializer`` for a model (or an input and an output model)."""
    if not (isinstance(schema, type) and issubclass(schema, pydantic.BaseModel)):
        return None
    return build_serializer(PydanticSerializer, schema, output_schema, model)
