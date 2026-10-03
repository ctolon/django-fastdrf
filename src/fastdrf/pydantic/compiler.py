"""The pydantic serializer backend: compiled output as pydantic models."""

import typing
from collections.abc import Callable

import pydantic

from fastdrf.compiler import Encoder, OutputSpec, UnreadableValue, related_items

__all__ = ["build", "model_for"]


def model_for(spec: OutputSpec) -> type[pydantic.BaseModel]:
    """A model reading ``spec``'s attributes and dumping DRF's keys."""
    fields = {}
    for index, field in enumerate(spec.fields):
        field_type = (
            model_for(field.type) if isinstance(field.type, OutputSpec) else field.type
        )
        if field.many:
            field_type = list[field_type]  # type: ignore[valid-type]  # built at runtime
        if field.nullable:
            field_type = typing.Optional[field_type]  # noqa: UP045 -- works for typing.Any
        convert = _items if field.many else field.convert
        if convert is not None:
            field_type = typing.Annotated[
                field_type, pydantic.BeforeValidator(_unless_none(convert))
            ]
        # Named by position: a DRF key may not be a pydantic field name
        # (``_label``, ``model_config``); it is the serialization alias.
        fields[f"field_{index}"] = (
            field_type,
            pydantic.Field(
                validation_alias=field.attribute, serialization_alias=field.key
            ),
        )
    # Fields known only at runtime: none of create_model's overloads apply.
    return pydantic.create_model(  # type: ignore[call-overload]
        spec.name,
        __config__=pydantic.ConfigDict(
            # The types the msgspec backend accepts, without coercion: a
            # value of another type is DRF's (``unreadable_source``).
            from_attributes=True,
            strict=True,
        ),
        **fields,
    )


def _items(value: typing.Any) -> list[typing.Any]:
    return list(related_items(value))


class _Raised(Exception):  # noqa: N818 -- carries another exception
    """
    An error of a representation, carried through pydantic, which would make
    a ``ValueError`` or ``AssertionError`` a ``ValidationError``: the source
    would then look unreadable and DRF would represent it again, running the
    representation twice.
    """

    def __init__(self, error: Exception) -> None:
        super().__init__(error)
        self.error = error


def _unless_none(
    convert: Callable[[typing.Any], typing.Any],
) -> Callable[[typing.Any], typing.Any]:
    # DRF represents None as None without asking the field.
    def validate(value: typing.Any) -> typing.Any:
        if value is None:
            return None
        try:
            return convert(value)
        except UnreadableValue:
            raise
        except (ValueError, AssertionError) as exc:
            raise _Raised(exc) from None

    return validate


def _unwrapped(dump: Callable[[typing.Any], typing.Any]) -> Callable[..., typing.Any]:
    def run(source: typing.Any) -> typing.Any:
        try:
            return dump(source)
        except _Raised as carried:
            raise carried.error from None

    return run


def build(spec: OutputSpec) -> Encoder:
    model = model_for(spec)
    adapter = pydantic.TypeAdapter(list[model])  # type: ignore[valid-type]  # built at runtime

    def dump(instance: typing.Any) -> dict[str, typing.Any]:
        return model.model_validate(instance, from_attributes=True).model_dump(
            mode="json", by_alias=True
        )

    def dump_many(instances: typing.Any) -> typing.Any:
        return adapter.dump_python(
            adapter.validate_python(instances, from_attributes=True),
            mode="json",
            by_alias=True,
        )

    return Encoder(
        model, _unwrapped(dump), _unwrapped(dump_many), pydantic.ValidationError
    )
