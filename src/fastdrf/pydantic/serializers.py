"""Serializers validated and represented by a pydantic ``BaseModel``."""

import collections
import collections.abc
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
    A field located by its attribute name (``loc_by_alias=False``) is keyed
    by its input name. A missing field read through an ``AliasPath``, at any
    depth of ``schema``, is reported where the input stops: at ``payload``
    when ``payload`` itself is absent.
    """
    alias_paths, model_names = _input_names(schema)
    for error in exc.errors(include_url=False, include_input=False):
        loc = tuple(error["loc"])
        input_names = _location_names(schema, loc, model_names)
        if error["type"] == "missing":
            yield (
                _missing_path(loc, data, alias_paths, input_names),
                REQUIRED_MESSAGE,
                "required",
            )
        else:
            path, _ = _walk(loc, data, input_names)
            yield tuple(path), error["msg"], error["type"]


def _input_names(
    schema: Any,
) -> tuple[tuple[tuple[Any, ...], ...], dict[type, dict[str, str]]]:
    """
    Of the models ``schema`` holds, itself included: the ``AliasPath``s
    their fields are read through (longest first), and the input name of
    each field a model locates by attribute name (``loc_by_alias=False``).
    """
    if not (isinstance(schema, type) and issubclass(schema, pydantic.BaseModel)):
        return (), {}
    return _error_names.get(schema, lambda: _read_input_names(schema))


def _read_input_names(
    schema: type[pydantic.BaseModel],
) -> tuple[tuple[tuple[Any, ...], ...], dict[type, dict[str, str]]]:
    paths: set[tuple[Any, ...]] = set()
    names: dict[type, dict[str, str]] = {}
    seen: set[type] = set()
    pending: list[Any] = [schema]
    while pending:
        annotation = pending.pop()
        if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
            if annotation in seen:
                continue
            seen.add(annotation)
            by_alias = annotation.model_config.get("loc_by_alias", True)
            for name, field in annotation.model_fields.items():
                alias = field.validation_alias
                choices = (
                    alias.choices
                    if isinstance(alias, pydantic.AliasChoices)
                    else [alias]
                )
                paths.update(
                    tuple(choice.path)
                    for choice in choices
                    if isinstance(choice, pydantic.AliasPath) and len(choice.path) > 1
                )
                given = alias if isinstance(alias, str) else field.alias
                if not by_alias and given and given != name:
                    names.setdefault(annotation, {})[name] = given
                pending.append(field.annotation)
        else:
            pending.extend(typing.get_args(annotation))
    return tuple(sorted(paths, key=len, reverse=True)), names


_error_names = BoundedCache(SCHEMA_CACHE_SIZE)


def _location_names(
    schema: Any,
    loc: tuple[Any, ...],
    model_names: dict[type, dict[str, str]],
) -> dict[int, str]:
    """Resolve aliases at their model's location, including container items."""
    if not model_names:
        return {}
    names = {}
    for index, segment in enumerate(loc):
        schema, alias = _location_step(schema, segment, model_names)
        if alias is not None:
            names[index] = alias
    return names


def _location_step(
    annotation: Any, segment: Any, model_names: dict[type, dict[str, str]]
) -> tuple[Any, str | None]:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Annotated:
        return _location_step(args[0], segment, model_names)
    if origin in (typing.Union, types.UnionType):
        # Non-discriminated unions include the model name in error locations.
        for member in args:
            if isinstance(member, type) and member.__name__ == segment:
                return member, None
        for member in args:
            child, alias = _location_step(member, segment, model_names)
            if child is not member:
                return child, alias
        return annotation, None
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        for name, field in annotation.model_fields.items():
            if (
                segment == name
                or segment == field.alias
                or segment == field.validation_alias
            ):
                return field.annotation, model_names.get(annotation, {}).get(name)
        return annotation, None
    if origin is not None and args:
        if isinstance(origin, type) and issubclass(origin, collections.abc.Mapping):
            return args[-1], None
        if isinstance(segment, int):
            if origin is tuple and len(args) > 1 and args[-1] is not Ellipsis:
                return args[segment] if 0 <= segment < len(args) else Any, None
            return args[0], None
    return annotation, None


def _missing_path(
    loc: tuple[Any, ...],
    data: Any,
    alias_paths: tuple[tuple[Any, ...], ...],
    input_names: dict[int, str],
) -> tuple[Any, ...]:
    for alias_path in alias_paths:
        if loc[len(loc) - len(alias_path) :] == alias_path:
            # Up to the field as found; then its path, to where the input stops.
            path, current = _walk(loc[: len(loc) - len(alias_path)], data, input_names)
            for segment in alias_path:
                found, segment, current = _child(current, segment)
                path.append(segment)
                if not found:
                    break
            return tuple(path)
    path, _ = _walk(loc, data, input_names, keep_last=True)
    return tuple(path)


def _walk(
    loc: tuple[Any, ...],
    data: Any,
    input_names: dict[int, str],
    keep_last: bool = False,
) -> tuple[list[Any], Any]:
    """The segments of ``loc`` found in ``data``, and the value reached."""
    path = []
    current = data
    for index, segment in enumerate(loc):
        found, key, child = _child(current, segment)
        if not found and index in input_names:
            # Located by attribute name: the input names it by its alias.
            found, key, child = _child(current, input_names[index])
            if not found and keep_last and index == len(loc) - 1:
                key = input_names[index]
        if found or (keep_last and index == len(loc) - 1 and isinstance(key, str)):
            path.append(key)
        if found:
            current = child
    return path, current


def _child(value: Any, segment: Any) -> tuple[bool, Any, Any]:
    """Whether ``value`` has ``segment``, the segment as its key, and the child."""
    if isinstance(value, dict) and segment in value:
        return True, segment, value[segment]
    if (
        isinstance(value, dict)
        and isinstance(segment, str)
        and hasattr(value, "getlist")
    ):
        return segment in value, segment, value.get(segment)
    if (
        isinstance(value, (list, tuple))
        and isinstance(segment, int)
        and -len(value) <= segment < len(value)
    ):
        # An ``AliasPath`` may count from the end; the error names the item.
        return True, segment % len(value), value[segment]
    return False, segment, value


class _Unset:
    """Default of every field of a partial model; never validated."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


class PydanticBackend:
    def __init__(self, *, context: Any = None) -> None:
        self.context = context

    def validate(
        self, schema: Any, data: Any, *, partial: bool, strict: bool | None
    ) -> Any:
        """``validated_data`` of ``data``: the field values of a loaded ``schema``."""
        return self.values(self.load(schema, data, strict=strict), partial=partial)

    def load(self, schema: Any, data: Any, *, strict: bool | None) -> Any:
        # None: the model's own strictness; False: lax whatever it says.
        try:
            return schema.model_validate(data, strict=strict, context=self.context)
        except pydantic.ValidationError as exc:
            raise_validation_error(list(parse_errors(exc, data, schema)))

    def values(self, obj: Any, *, partial: bool) -> dict[str, Any]:
        # A model keeps its fields' values in ``__dict__`` and its extra
        # fields in ``__pydantic_extra__``, which ``model_fields`` and
        # ``model_extra`` read through Python descriptors.
        fields = obj.__dict__
        extra = obj.__pydantic_extra__
        if partial:
            # An extra field is read from the extras: as an attribute its
            # name may be the model's own (``model_dump``, ``model_config``).
            extra = extra or {}
            return {
                name: extra[name] if name in extra else fields[name]
                for name in obj.__pydantic_fields_set__
            }
        values = dict(fields)
        # Cached properties also live in __dict__, but are not input fields.
        for name in fields.keys() - type(obj).model_fields.keys():
            del values[name]
        if extra:
            values.update(extra)
        return values

    def from_values(self, schema: Any, values: dict[str, Any]) -> Any:
        return schema.model_construct(**values)

    def with_values(self, obj: Any, changes: dict[str, Any]) -> Any:
        # A copy, without validating again.
        return obj.model_copy(update=changes)

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
        # The item types, gathered in C: an isinstance() per item goes
        # through the model class's ABC check.
        models = {
            kind for kind in set(map(type, instances)) if issubclass(kind, schema)
        }
        if models:
            # As in dump(): existing models are represented, not validated
            # again. Keep their callbacks and revalidate_instances policy
            # from changing the result merely because many=True was used.
            instances = [
                instance
                if type(instance) in models
                else schema.model_validate(
                    instance, from_attributes=True, context=self.context
                )
                for instance in instances
            ]
        else:
            instances = adapter.validate_python(
                instances, from_attributes=True, context=self.context
            )
        # Through the adapter's SchemaSerializer, which accepts ``context`` on
        # every supported pydantic version.
        return adapter.serializer.to_python(
            instances,
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
        fields = {
            name: value for name, value in values.items() if name in schema.model_fields
        }
        extra = (
            {name: value for name, value in values.items() if name not in fields}
            if schema.model_config.get("extra") == "allow"
            else {}
        )
        return schema.__pydantic_serializer__.to_python(
            _constructed(schema, fields, extra),
            mode="json",
            by_alias=True,
            include={*fields, *extra},
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
            if _takes_list(field.annotation):
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

    def json_schema(
        self, schema: Any, *, ref_prefix: str, direction: str
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """
        The JSON Schema of ``schema`` and of the models it refers to, which
        refer to each other through ``ref_prefix`` (an OpenAPI generator's
        components): its validation schema for a ``"request"``, its
        serialization schema (serialization aliases) for a ``"response"``.
        """
        mode = "validation" if direction == "request" else "serialization"
        body = schema.model_json_schema(ref_template=ref_prefix + "{model}", mode=mode)
        components = body.pop("$defs", {})
        return body, components

    def field_schemas(self, schema: Any) -> dict[str, type | None]:
        """
        Each attribute validation reads (by name or alias, excluded fields
        too), with the model it holds: itself, or the items of a collection.
        """
        read: dict[str, type | None] = {}
        for name, field in schema.model_fields.items():
            nested = _nested_model(field.annotation)
            for attribute in (name, field.alias, field.validation_alias):
                if isinstance(attribute, str):
                    read[attribute] = nested
        return read

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


def _constructed(schema: Any, fields: dict[str, Any], extra: dict[str, Any]) -> Any:
    """
    An instance of ``schema`` holding ``fields`` by attribute name and
    nothing else, set as ``model_construct()`` sets them. Not
    ``model_construct()`` itself: it reads its arguments as input (an alias
    that is another field's name takes its value), and builds the defaults
    of the fields not given and runs ``model_post_init()``, for output that
    leaves them out.
    """
    obj = schema.__new__(schema)
    object.__setattr__(obj, "__dict__", dict(fields))
    object.__setattr__(obj, "__pydantic_fields_set__", set(fields))
    allows_extra = schema.model_config.get("extra") == "allow"
    object.__setattr__(obj, "__pydantic_extra__", extra if allows_extra else None)
    object.__setattr__(obj, "__pydantic_private__", None)
    return obj


_COLLECTIONS = (
    list,
    set,
    frozenset,
    tuple,
    collections.deque,
    collections.abc.Sequence,
    collections.abc.MutableSequence,
    collections.abc.Set,
    collections.abc.MutableSet,
)


def _takes_list(annotation: Any) -> bool:
    """Whether ``annotation`` accepts a list: a collection, or a union with one."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _takes_list(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return any(_takes_list(member) for member in typing.get_args(annotation))
    return (origin or annotation) in _COLLECTIONS


def _nested_model(annotation: Any) -> type | None:
    """The model ``annotation`` holds: itself, or the items of a collection."""
    annotation = _bare(annotation)
    if typing.get_origin(annotation) in _COLLECTIONS:
        arguments = typing.get_args(annotation)
        annotation = _bare(arguments[0]) if arguments else None
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        return annotation
    return None


def _bare(annotation: Any) -> Any:
    """``annotation`` without ``Annotated`` metadata and ``| None``."""
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
        elif origin in (typing.Union, types.UnionType):
            members = [
                member
                for member in typing.get_args(annotation)
                if member is not type(None)
            ]
            if len(members) != 1:
                return annotation
            annotation = members[0]
        else:
            return annotation


def _simple_type(annotation: Any) -> type | None:
    annotation = _bare(annotation)
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
        # Not given: UNSET, also where the model builds a default.
        partial_field = FieldInfo.merge_field_infos(
            field, default=UNSET, default_factory=None
        )
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

    def _strict(self) -> bool | None:
        # Unset: the model's own ``strict`` decides.
        return getattr(getattr(self, "Meta", None), "strict", None)


def serializer_for(
    schema: object,
    output_schema: type | None = None,
    model: type[Model] | None = None,
    *,
    base: type | None = None,
) -> type | None:
    """A ``PydanticSerializer`` for a model (or an input and an output model)."""
    if not (isinstance(schema, type) and issubclass(schema, pydantic.BaseModel)):
        return None
    # ``base``: a package's subclass of PydanticSerializer (aiodrf's is asynchronous).
    return build_serializer(base or PydanticSerializer, schema, output_schema, model)
