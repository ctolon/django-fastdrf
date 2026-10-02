"""The msgspec serializer backend: compiled output as ``msgspec.Struct`` classes."""

import collections
import operator
import typing
import weakref
from collections.abc import Sequence

import msgspec

from fastdrf.compiler import (
    Encoder,
    OutputField,
    OutputSpec,
    UnreadableValue,
    _exact_string,
    related_items,
)

__all__ = ["build", "struct_for"]


class _Deferred(typing.NamedTuple):
    """
    A field to finish after conversion: ``"many"`` converts the items of a
    related manager to ``schema`` (a list type), ``"one"`` converts the value
    read to ``schema``, and ``"done"`` is a value msgspec already converted
    whose ``plan`` remains.
    """

    attribute: str
    kind: str
    schema: typing.Any
    plan: "_Plan | None"


class _Plan(typing.NamedTuple):
    """
    What a Struct's values still need once msgspec converted them: the
    ``(field, attribute read, convert)`` of each field converted by Python
    code, then the fields to finish, depth first.

    Nothing runs inside msgspec's conversion (a ``__post_init__``): a
    conversion started there crashes msgspec before 0.21, and before 0.21
    too, an object the garbage collector tracks (a list, a model instance)
    assigned there aborts the interpreter.
    """

    converters: tuple[tuple[str, str, typing.Any], ...]
    later: tuple[_Deferred, ...]
    # Readers of the ``CharField`` values, which msgspec leaves as they are
    # when they are a subclass of ``str`` (an enum member): DRF outputs
    # ``str(value)`` for them, so only an exact ``str`` is msgspec's output.
    strings: tuple[typing.Any, ...] = ()


# Struct class -> immutable plan. Plans retain child schemas, not their own
# key, so dynamically compiled roots remain collectible.
_PLANS: weakref.WeakKeyDictionary[type, _Plan] = weakref.WeakKeyDictionary()


def _unchanged[T](value: T) -> T:
    return value


def _any_values(values: list[typing.Any]) -> bool:
    return True


_unchanged.accepts = _any_values  # type: ignore[attr-defined]
_unchanged.column = list  # type: ignore[attr-defined]


def struct_for(spec: OutputSpec) -> typing.Any:
    """A ``Struct`` reading ``spec``'s attributes and emitting DRF's keys."""
    fields: list[tuple[typing.Any, ...]] = []
    rename = {}
    # Attributes read as they are and converted after: ``from_attributes``
    # has no hook for a value it can read but not convert (a manager), or
    # converts differently (a datetime, which DRF moves to a time zone).
    converters = []
    later: list[_Deferred] = []
    strings = []
    counts = collections.Counter(field.attribute for field in spec.fields)
    read: set[str] = set()
    for index, field in enumerate(spec.fields):
        if counts[field.attribute] > 1:
            # ``from_attributes`` reads each attribute into one Struct field.
            # The others sharing it have a name no instance has, hold None
            # once read, and are given the value the first one read; each is
            # then converted as it would be alone.
            if field.attribute in read:
                name = f"_fastdrf_{index}"
                fields.append((name, typing.Any, None))
            else:
                name = field.attribute
                read.add(name)
                fields.append((name, typing.Any))
            converters.append((name, field.attribute, field.convert or _unchanged))
            if field.convert is None:
                later.append(_deferred(name, field))
            if name != field.key:
                rename[name] = field.key
            continue
        field_type: typing.Any
        if field.many:
            field_type = typing.Any
            later.append(_deferred(field.attribute, field))
        elif field.convert is _exact_string:
            # Checked for the whole output at once (``_complete``), without a
            # call per value.
            field_type = str | None
            strings.append(operator.attrgetter(field.attribute))
        elif field.convert is not None:
            field_type = typing.Any
            converters.append((field.attribute, field.attribute, field.convert))
        else:
            field_type = (
                struct_for(field.type)
                if isinstance(field.type, OutputSpec)
                else field.type
            )
            if field_type in _PLANS:
                later.append(
                    _Deferred(field.attribute, "done", None, _PLANS[field_type])
                )
            if field.nullable:
                field_type = field_type | None
        fields.append((field.attribute, field_type))
        if field.attribute != field.key:
            rename[field.attribute] = field.key
    struct = msgspec.defstruct(
        spec.name,
        fields,
        rename=rename or None,
        # Fields with a default among others: the order stays the output's.
        kw_only=any(count > 1 for count in counts.values()),
    )
    if converters or later or strings:
        _PLANS[struct] = _Plan(tuple(converters), tuple(later), tuple(strings))
    return struct


def _deferred(name: str, field: OutputField) -> _Deferred:
    if field.many:
        child = struct_for(field.type)
        child_list = list[child]  # type: ignore[valid-type]  # generated Struct
        return _Deferred(name, "many", child_list, _PLANS.get(child))
    if isinstance(field.type, OutputSpec):
        child = struct_for(field.type)
        return _Deferred(name, "one", child, _PLANS.get(child))
    return _Deferred(name, "one", field.type, None)


_STRINGS = frozenset({str, type(None)})


def _check_strings(
    values: Sequence[typing.Any], strings: tuple[typing.Any, ...]
) -> None:
    if len(values) == 1:
        (value,) = values
        if {type(read_string(value)) for read_string in strings} <= _STRINGS:
            return
    else:
        for read_string in strings:
            # The types of the whole output's values, gathered in C.
            if not set(map(type, map(read_string, values))) <= _STRINGS:
                break
        else:
            return
    raise msgspec.ValidationError("a CharField holds a subclass of str")


def _complete(values: Sequence[typing.Any], plan: _Plan) -> None:
    """Finish what ``values`` still hold (:class:`_Plan`), depth first."""
    converters, later, strings = plan
    if strings:
        _check_strings(values, strings)
    if not (converters or later):
        return
    if converters:
        if len(values) < _COLUMN_ROWS:
            _complete_rows(values, converters, None)
        else:
            # Every value is read before any is converted: several fields
            # may share a value.
            columns = [
                [getattr(value, source) for value in values]
                for _, source, _ in converters
            ]
            if not _complete_columns(values, converters, columns):
                _complete_rows(values, converters, zip(*columns, strict=True))
    for value in values:
        for attribute, kind, schema, child_plan in later:
            item = getattr(value, attribute)
            if item is None:
                continue
            if kind == "many":
                items = msgspec.convert(
                    list(related_items(item)), schema, from_attributes=True
                )
                setattr(value, attribute, items)
                if child_plan:
                    _complete(items, child_plan)
                continue
            if kind == "one":
                item = msgspec.convert(item, schema, from_attributes=True)
                setattr(value, attribute, item)
            if child_plan:
                _complete((item,), child_plan)


# Below this many rows, setting up the columns costs more than converting
# row by row saves (measured: 5 rows slower, 8 even, 12 and more faster).
_COLUMN_ROWS = 8


def _complete_rows(
    values: Sequence[typing.Any],
    converters: tuple[tuple[str, str, typing.Any], ...],
    rows: typing.Any,
) -> None:
    """Convert row by row, in DRF's order, from ``rows`` if already read."""
    for value in values:
        # Read before any is converted: several fields may share a value.
        read = (
            [getattr(value, source) for _, source, _ in converters]
            if rows is None
            else next(rows)
        )
        try:
            for (name, _, convert), item in zip(converters, read, strict=True):
                # DRF represents None as None without asking the field.
                setattr(value, name, None if item is None else convert(item))
        except UnreadableValue as exc:
            raise msgspec.ValidationError(str(exc)) from exc


def _complete_columns(
    values: Sequence[typing.Any],
    converters: tuple[tuple[str, str, typing.Any], ...],
    columns: list[list[typing.Any]],
) -> bool:
    """
    Convert column by column when every converter accepts its column (see
    ``accepts`` in the compiler), and tell whether it did. Nothing is
    converted unless every column is accepted, and nothing is written unless
    every column is converted: otherwise the caller converts row by row, in
    DRF's order, which raises DRF's first error.
    """
    if not all(hasattr(convert, "column") for _, _, convert in converters):
        return False
    pairs = list(zip(converters, columns, strict=True))
    if not all(convert.accepts(column) for (_, _, convert), column in pairs):
        return False
    try:
        converted = [convert.column(column) for (_, _, convert), column in pairs]
    except Exception:  # noqa: BLE001 -- the row-by-row conversion raises DRF's error
        return False
    for (name, _, _), column in zip(converters, converted, strict=True):
        for value, item in zip(values, column, strict=True):
            setattr(value, name, item)
    return True


def build(spec: OutputSpec) -> Encoder:
    schema = struct_for(spec)
    many = list[schema]  # type: ignore[valid-type]  # built at runtime
    plan = _PLANS.get(schema)

    def dump(instance: typing.Any) -> typing.Any:
        value = msgspec.convert(instance, schema, from_attributes=True)
        if plan:
            _complete((value,), plan)
        return msgspec.to_builtins(value)

    def dump_many(instances: typing.Any) -> typing.Any:
        values = msgspec.convert(instances, many, from_attributes=True)
        if plan:
            _complete(values, plan)
        return msgspec.to_builtins(values)

    return Encoder(schema, dump, dump_many, msgspec.ValidationError)
