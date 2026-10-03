"""
The ``"python"`` serializer backend: compiled output without msgspec or
pydantic.

The compiler's analysis decides which fields DRF represents in a way that
can be repeated without DRF's per-field machinery (:mod:`fastdrf.compiler`);
this backend turns its :class:`~fastdrf.compiler.OutputSpec` into one
reader per field and a dict per instance. It reads what the msgspec backend
reads and accepts what it accepts: a value of another type than the field's,
or a missing attribute, raises :class:`UnreadableSource`, and DRF represents
that source (:func:`~fastdrf.compiler.unreadable_source`). Input is
validated by DRF.
"""

import datetime
import decimal
import typing
import uuid
from collections.abc import Callable
from typing import Any

from fastdrf.compiler import (
    Encoder,
    OutputField,
    OutputSpec,
    UnreadableValue,
    _exact_string,
    related_items,
)

__all__ = ["UnreadableSource", "build"]

type _Reader = Callable[[Any], Any]


class UnreadableSource(UnreadableValue):
    """The source lacks what the compiled output reads, or holds another type."""


def build(spec: OutputSpec) -> Encoder:
    dump = _dumper(spec)

    def dump_many(instances: Any) -> list[Any]:
        return [dump(instance) for instance in instances]

    return Encoder(spec, dump, dump_many, UnreadableValue)


def _dumper(spec: OutputSpec) -> _Reader:
    readers = tuple((field.key, _reader(field)) for field in spec.fields)

    def dump(instance: Any) -> dict[str, Any]:
        return {key: read(instance) for key, read in readers}

    return dump


def _read(instance: Any, attribute: str) -> Any:
    try:
        return getattr(instance, attribute)
    except Exception as exc:
        # As the other backends: DRF reads the source again, and outputs
        # None, a default or nothing (a missing row, an unsaved instance's
        # related objects), or raises the exception itself.
        raise UnreadableSource(f"{attribute}: {exc!r}") from exc


def _reader(field: OutputField) -> _Reader:
    attribute = field.attribute
    if field.many:
        child = _dumper(field.type)

        def read_many(instance: Any) -> Any:
            value = _read(instance, attribute)
            if value is None:
                return None
            return [child(item) for item in related_items(value)]

        return read_many
    if isinstance(field.type, OutputSpec):
        nested = _dumper(field.type)

        def read_nested(instance: Any) -> Any:
            value = _read(instance, attribute)
            return None if value is None else nested(value)

        return read_nested
    if field.convert is _exact_string:
        # A ``CharField``: an exact ``str`` is DRF's ``str(value)``.
        def read_string(instance: Any) -> Any:
            value = _read(instance, attribute)
            if value is None or type(value) is str:
                return value
            raise UnreadableSource(f"{attribute}: {value!r} is not a str")

        return read_string
    convert = field.convert or _TYPED.get(field.type)
    if convert is None:
        raise ValueError(f"The python backend has no reader for {field.type!r}")

    def read(instance: Any) -> Any:
        value = _read(instance, attribute)
        if value is None:
            return None
        value = convert(value)
        # The compiler's datetime representation leaves the formatting to the
        # backend when it is DRF's (a whole-minute offset).
        return _iso(value) if type(value) is datetime.datetime else value

    return read


def _iso(value: datetime.datetime) -> str:
    # DRF's ISO 8601: ``Z`` for a zero offset.
    text = value.isoformat()
    return text[:-6] + "Z" if text.endswith("+00:00") else text


# Values the msgspec backend accepts for a typed field (``strict`` conversion)
# and outputs for it; anything else is the source's to DRF.
def _string(value: Any) -> str:
    # A ``CharField`` converts with ``str()`` (``compiler._exact_string``);
    # for a choice or a read-only value DRF outputs the string as it is.
    if isinstance(value, str):
        return value
    raise UnreadableSource(f"{value!r} is not a string")


def _integer(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    raise UnreadableSource(f"{value!r} is not an integer")


def _float(value: Any) -> float:
    if type(value) is float:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise UnreadableSource(f"{value!r} is not a float")


def _boolean(value: Any) -> bool:
    if type(value) is bool:
        return value
    raise UnreadableSource(f"{value!r} is not a boolean")


def _uuid(value: Any) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    raise UnreadableSource(f"{value!r} is not a UUID")


def _date(value: Any) -> str:
    if type(value) is datetime.date:
        return value.isoformat()
    raise UnreadableSource(f"{value!r} is not a date")


def _time(value: Any) -> str:
    if type(value) is datetime.time:
        return value.isoformat()
    raise UnreadableSource(f"{value!r} is not a time")


def _decimal(value: Any) -> str:
    if isinstance(value, decimal.Decimal):
        return str(value)
    raise UnreadableSource(f"{value!r} is not a decimal")


def _unchanged(value: Any) -> Any:
    return value


_TYPED: dict[Any, _Reader] = {
    str: _string,
    int: _integer,
    float: _float,
    bool: _boolean,
    uuid.UUID: _uuid,
    datetime.date: _date,
    datetime.time: _time,
    decimal.Decimal: _decimal,
    typing.Any: _unchanged,
}
