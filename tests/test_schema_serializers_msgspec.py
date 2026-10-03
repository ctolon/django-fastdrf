"""``MsgspecSerializer``: msgspec's rules for validation and representation."""

import datetime
import decimal
import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, NamedTuple, NewType, TypedDict

import msgspec
import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import QueryDict
from django.test import override_settings
from rest_framework.renderers import JSONRenderer

from fastdrf.msgspec.serializers import MsgspecSerializer, serializer_for
from fastdrf.typed import adapt, schema_serializer
from tests.models import Author
from tests.test_inputs import exact

SECRET = "private-token"


@pytest.mark.django_db
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("update", [False, True])
def test_unset_fields_are_not_written_to_the_model(partial, update):
    class OptionalName(msgspec.Struct):
        name: str | msgspec.UnsetType = msgspec.UNSET

    serializer = serializer_class(OptionalName, model=Author)
    instance = Author.objects.create(name="Ada") if update else None
    loaded = serializer(instance, data={}, partial=partial)
    assert loaded.is_valid(), loaded.errors
    assert loaded.validated_data == {}
    saved = loaded.save()
    saved.refresh_from_db()
    assert saved.name == ("Ada" if update else "")

    given = serializer(saved, data={"name": "Grace"}, partial=partial)
    assert given.is_valid(), given.errors
    assert given.save().name == "Grace"


def serializer_class(schema, **meta):
    return type(
        "Serializer",
        (MsgspecSerializer,),
        {"Meta": type("Meta", (), {"schema": schema, **meta})},
    )


def rendered(serializer):
    data = serializer.data
    assert SECRET.encode() not in JSONRenderer().render(data)
    return data


# -- Validation -----------------------------------------------------------------------


class Positive(msgspec.Struct):
    value: int
    note: str = ""

    def __post_init__(self):
        if self.value < 0:
            raise ValueError("positive required")


class PositivePatch(msgspec.Struct):
    value: int | msgspec.UnsetType = msgspec.UNSET
    note: str | msgspec.UnsetType = msgspec.UNSET

    def __post_init__(self):
        if self.value is not msgspec.UNSET and self.value < 0:
            raise ValueError("positive required")


def test_errors_are_drfs_with_msgspecs_first_message():
    serializer = serializer_class(Positive)(data={"value": "a"})
    assert not serializer.is_valid()
    assert serializer.errors == {"value": ["Expected `int`, got `str`"]}
    assert serializer.errors["value"][0].code == "invalid"
    serializer = serializer_class(Positive)(data={})
    assert not serializer.is_valid()
    assert serializer.errors == {"value": ["This field is required."]}
    assert serializer.errors["value"][0].code == "required"
    serializer = serializer_class(Positive)(data={"value": -1})
    assert not serializer.is_valid()
    assert serializer.errors == {"non_field_errors": ["positive required"]}


def test_strict_input_is_the_default_and_can_be_relaxed():
    assert not serializer_class(Positive)(data={"value": "1"}).is_valid()
    lenient = serializer_class(Positive, strict=False)(data={"value": "1"})
    assert lenient.is_valid(), lenient.errors
    assert lenient.validated_data == {"value": 1, "note": ""}


def test_partial_validation_never_drops_schema_invariants():
    # A derived partial Struct would not run ``__post_init__``.
    assert not serializer_class(Positive)(data={"value": -1}).is_valid()
    with pytest.raises(ImproperlyConfigured, match="partial_schema"):
        serializer_class(Positive)(data={"value": -1}, partial=True).is_valid()

    with_patch = serializer_class(Positive, partial_schema=PositivePatch)
    assert not with_patch(data={"value": -1}, partial=True).is_valid()
    serializer = with_patch(data={"note": "x"}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"note": "x"}


class Plain(msgspec.Struct):
    value: int
    note: str = ""


def test_partial_schema_is_derived_when_nothing_can_be_lost():
    serializer = serializer_class(Plain)(data={"note": "x"}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"note": "x"}


class Tagged(msgspec.Struct):
    name: str
    tags: list[str] = []


def test_form_input_keeps_repeated_values():
    serializer = serializer_class(Tagged)(data=QueryDict("name=a&tags=x&tags=y"))
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"name": "a", "tags": ["x", "y"]}


@pytest.mark.parametrize("partial", [False, True])
def test_form_fixed_tuple_keeps_all_values(partial):
    class Payload(msgspec.Struct):
        coordinates: tuple[int, int]

    serializer = adapt(Payload)(
        data=QueryDict("coordinates=1&coordinates=2"), partial=partial
    )
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"coordinates": (1, 2)}


@pytest.mark.parametrize("partial", [False, True])
def test_nested_collection_constraints_survive_adaptation(partial):
    class Payload(msgspec.Struct):
        values: list[Annotated[str, msgspec.Meta(min_length=5)]] | None

    invalid = adapt(Payload)(data={"values": ["no"]}, partial=partial)
    assert not invalid.is_valid()
    assert invalid.errors["values"][0][0].code == "invalid"
    for value in (["valid"], None):
        valid = adapt(Payload)(data={"values": value}, partial=partial)
        assert valid.is_valid(), valid.errors
        assert valid.validated_data == {"values": value}


class Point(msgspec.Struct, array_like=True):
    x: int
    y: int = 0


@pytest.mark.parametrize("as_dict", [False, True])
@pytest.mark.parametrize(
    ("data", "errors"),
    [
        (["a"], {0: ["Expected `int`, got `str`"]}),
        ([1, "a"], {1: ["Expected `int`, got `str`"]}),
    ],
)
def test_errors_of_an_array_like_struct_are_keyed_by_position(data, errors, as_dict):
    # A serializer's errors are a dict on every DRF version, so the top
    # level of an array input keys its items as DRF's ListField does.
    rest_framework = {
        **settings.REST_FRAMEWORK,
        "LIST_SERIALIZER_ERRORS_AS_DICT": as_dict,
    }
    with override_settings(REST_FRAMEWORK=rest_framework):
        serializer = adapt(Point)(data=data)
        assert not serializer.is_valid()
        assert serializer.errors == errors


class RenamedStruct(msgspec.Struct, rename="camel"):
    first_name: str
    page_count: int = 1


def test_the_data_of_validated_input_is_named_as_on_the_wire():
    body = {"firstName": "Ada", "pageCount": 3}
    serializer = adapt(RenamedStruct)(data=body)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"first_name": "Ada", "page_count": 3}
    assert serializer.data == body
    partial = adapt(RenamedStruct)(data={"pageCount": 4}, partial=True)
    assert partial.is_valid(), partial.errors
    assert partial.data == {"pageCount": 4}


# -- Partial input: the output Struct's names, the input Struct's configuration -------


class Owner(msgspec.Struct):
    name: str
    password: str


class PublicOwner(msgspec.Struct):
    name: str


class Document(msgspec.Struct):
    title: str
    owner: Owner


class PublicDocument(msgspec.Struct, rename="camel", tag=True):
    title: str
    owner: PublicOwner
    page_count: int = 0


@pytest.mark.parametrize("partial", [False, True])
def test_partial_data_is_the_output_schemas(partial):
    body = {"title": "Notes", "owner": {"name": "Ada", "password": SECRET}}
    serializer = schema_serializer(Document, PublicDocument)(data=body, partial=partial)
    assert serializer.is_valid(), serializer.errors
    expected = {"type": "PublicDocument", "title": "Notes", "owner": {"name": "Ada"}}
    if not partial:
        expected["pageCount"] = 0
    assert rendered(serializer) == expected


class Event(msgspec.Struct, tag="allowed"):
    value: int


class Numbered(
    msgspec.Struct, tag_field="kind", tag=3, rename="camel", forbid_unknown_fields=True
):
    page_count: int
    note: str = ""


class Named(msgspec.Struct, tag=True):
    value: int


class Row(msgspec.Struct, array_like=True):
    value: int
    note: str = ""


class RowPatch(msgspec.Struct, array_like=True):
    value: int | msgspec.UnsetType = msgspec.UNSET
    note: str | msgspec.UnsetType = msgspec.UNSET


class EventPatch(msgspec.Struct, tag="allowed", tag_field="type"):
    value: int | msgspec.UnsetType = msgspec.UNSET


def errors(schema, data, partial, partial_schema=None):
    meta = {} if partial_schema is None else {"partial_schema": partial_schema}
    serializer = serializer_class(schema, **meta)(data=data, partial=partial)
    return None if serializer.is_valid() else serializer.errors


@pytest.mark.parametrize(
    ("schema", "valid", "invalid"),
    [
        (Event, {"type": "allowed", "value": 1}, [{"type": "wrong", "value": 1}]),
        (Numbered, {"kind": 3, "pageCount": 1}, [{"kind": 4}, {"kind": "3"}]),
        (Named, {"type": "Named", "value": 1}, [{"type": "Event", "value": 1}]),
    ],
)
def test_a_partial_update_checks_the_tag_as_a_full_one_does(schema, valid, invalid):
    # msgspec accepts a Struct's tag left out, and checks one that is given.
    untagged = {
        key: value for key, value in valid.items() if key not in ("type", "kind")
    }
    for data in (valid, untagged):
        assert errors(schema, data, False) is None
        assert errors(schema, data, True) is None
    for data in invalid:
        full = errors(schema, {**valid, **data}, False)
        assert full is not None
        assert errors(schema, data, True) == full


def test_an_explicit_partial_schema_checks_its_own_tag():
    assert errors(Event, {"type": "allowed"}, True, EventPatch) is None
    assert errors(Event, {"type": "wrong"}, True, EventPatch) is not None


def test_a_derived_partial_update_keeps_names_and_unknown_field_policy():
    assert errors(Numbered, {"kind": 3, "note": "x"}, True) is None
    assert errors(Numbered, {"kind": 3, "page_count": 1}, True) is not None
    serializer = serializer_class(Numbered)(data={"kind": 3, "note": "x"}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"note": "x"}
    assert serializer.data == {"kind": 3, "note": "x"}


def test_an_array_like_struct_needs_an_explicit_partial_schema():
    # A shorter array cannot say which of its fields it leaves out.
    with pytest.raises(ImproperlyConfigured, match="partial_schema"):
        errors(Row, [1], True)
    assert errors(Row, [1], True, RowPatch) is None
    assert errors(Row, {"value": 1}, True, RowPatch) is not None


def test_an_array_like_output_cannot_represent_partial_input():
    serializer = serializer_class(Row, partial_schema=RowPatch)(data=[1], partial=True)
    assert serializer.is_valid(), serializer.errors
    with pytest.raises(TypeError, match="array_like"):
        serializer.data  # noqa: B018


@pytest.mark.parametrize("many", [False, True])
def test_array_like_output_keeps_its_shape(many):
    serializer = adapt(Point)(data=[[3]] if many else [3], many=many)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == (
        [{"x": 3, "y": 0}] if many else {"x": 3, "y": 0}
    )
    assert serializer.data == ([[3, 0]] if many else [3, 0])


# -- Custom types and the projection of nested output ---------------------------------


class Reference:
    def __init__(self, value):
        self.value = value


class CustomStruct(msgspec.Struct):
    reference: Reference


def decode_reference(target, value):
    if target is Reference and isinstance(value, str):
        return Reference(value)
    raise ValueError("Expected a reference string")


def encode_reference(value):
    if isinstance(value, Reference):
        return value.value
    raise NotImplementedError(type(value).__name__)


class CustomSerializer(MsgspecSerializer):
    class Meta:
        schema = CustomStruct
        dec_hook = decode_reference
        enc_hook = encode_reference


@pytest.mark.parametrize("many", [False, True])
def test_custom_types_round_trip(many):
    payload = {"reference": "book:1"}
    serializer = CustomSerializer(data=[payload] if many else payload, many=many)
    assert serializer.is_valid(), serializer.errors
    assert serializer.data == ([payload] if many else payload)


def test_custom_type_errors_remain_drf_errors():
    serializer = CustomSerializer(data={"reference": 1})
    assert not serializer.is_valid()
    assert serializer.errors["reference"][0].code == "invalid"


def test_nested_hooks_do_not_disclose_subclass_fields():
    class Public(msgspec.Struct):
        reference: Reference

    class Internal(Public):
        secret: str

    class Envelope(msgspec.Struct):
        item: Public

    class Output(CustomSerializer):
        class Meta(CustomSerializer.Meta):
            schema = Envelope

    assert Output(Envelope(Internal(Reference("book:1"), "hidden"))).data == {
        "item": {"reference": "book:1"}
    }


def test_values_do_not_reinspect_field_types(monkeypatch):
    class Nested(msgspec.Struct):
        value: int

    class Payload(msgspec.Struct, rename="camel"):
        nested_value: Nested

    obj = Payload(Nested(1))
    backend = adapt(Payload)().backend
    monkeypatch.setattr(
        msgspec.structs, "fields", lambda *args: pytest.fail("field type inspection")
    )
    assert backend.values(obj, partial=False) == {"nested_value": obj.nested_value}


def test_field_metadata_is_reused_without_sharing_drf_fields(monkeypatch):
    class Payload(msgspec.Struct):
        value: int

    payloads = adapt(Payload)
    first = payloads().fields
    monkeypatch.setattr(
        msgspec.inspect, "type_info", lambda *args: pytest.fail("schema rebuilt")
    )
    second = payloads().fields
    assert first["value"] is not second["value"]


def test_model_instances_are_read_by_attribute():
    class AuthorOut(msgspec.Struct):
        id: int
        name: str

    authors = [Author(pk=1, name="a"), Author(pk=2, name="b")]
    assert adapt(AuthorOut)(authors[0]).data == {"id": 1, "name": "a"}
    assert adapt(AuthorOut)(authors, many=True).data == [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
    ]


# -- Datetimes are msgspec's, not Django's --------------------------------------------


class Stamp(msgspec.Struct):
    when: datetime.datetime


@pytest.mark.parametrize(
    "value",
    [
        datetime.datetime(2026, 1, 2, 3, 4, 5),
        datetime.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=datetime.UTC),
        datetime.datetime(
            2026,
            1,
            2,
            3,
            4,
            5,
            tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=45)),
        ),
        datetime.datetime.min,
        datetime.datetime.max.replace(tzinfo=datetime.UTC),
    ],
)
def test_datetime_validation_and_output_match_msgspec(value):
    native = msgspec.convert({"when": value}, Stamp, strict=True)
    expected = msgspec.to_builtins(native)
    serializer = adapt(Stamp)(data={"when": value})
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["when"] == native.when
    assert adapt(Stamp)(native).data == expected
    assert adapt(Stamp)([native], many=True).data == [expected]


@pytest.mark.parametrize("value", ["not-a-date", "2026-02-30T12:00:00Z"])
def test_invalid_datetimes_report_field_errors(value):
    serializer = adapt(Stamp)(data={"when": value})
    assert not serializer.is_valid()
    assert "when" in serializer.errors


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("value", ["2026-01-02T03:04:05Z", "2026-01-02T03:04:05+05:45"])
def test_iso_input_keeps_msgspecs_strictness(strict, value):
    try:
        native = msgspec.convert({"when": value}, Stamp, strict=strict)
    except msgspec.ValidationError:
        native = None
    serializer = serializer_class(Stamp, strict=strict)(data={"when": value})
    assert serializer.is_valid() is (native is not None)
    if native is not None:
        assert serializer.validated_data["when"] == native.when


# -- Every type msgspec supports -------------------------------------------------------


class State(enum.Enum):
    ready = "ready"


class Number(enum.IntEnum):
    one = 1


class Label(enum.StrEnum):
    ready = "ready"


class Flags(enum.IntFlag):
    read = 1
    write = 2


class Position(NamedTuple):
    x: int
    y: int


class Coordinates(TypedDict):
    x: int
    y: int


@dataclass
class Pair:
    x: int
    y: int


TYPES = {
    "none": (type(None), None),
    "bool": (bool, True),
    "int": (int, 12),
    "float": (float, 1.25),
    "str": (str, "text"),
    "bytes": (bytes, b"text"),
    "bytearray": (bytearray, bytearray(b"text")),
    "list": (list[int], [1, 2]),
    "dict": (dict[str, int], {"one": 1}),
    "tuple": (tuple[int, str], (1, "one")),
    "variable_tuple": (tuple[int, ...], (1, 2)),
    "set": (set[int], {1}),
    "frozenset": (frozenset[int], frozenset({1})),
    "datetime": (
        datetime.datetime,
        datetime.datetime(2026, 9, 27, tzinfo=datetime.UTC),
    ),
    "date": (datetime.date, datetime.date(2026, 9, 27)),
    "time": (datetime.time, datetime.time(12, 30)),
    "duration": (datetime.timedelta, datetime.timedelta(seconds=90)),
    "uuid": (uuid.UUID, uuid.UUID(int=1)),
    "decimal": (decimal.Decimal, decimal.Decimal("1.250")),
    "enum": (State, State.ready),
    "int_enum": (Number, Number.one),
    "str_enum": (Label, Label.ready),
    "int_flag": (Flags, Flags.read | Flags.write),
    "dataclass": (Pair, Pair(1, 2)),
    "named_tuple": (Position, Position(1, 2)),
    "typed_dict": (Coordinates, {"x": 1, "y": 2}),
    "any": (Any, {"x": [1, True, None]}),
    "optional": (int | None, None),
    "union": (int | str, "one"),
    "literal": (Literal["one", "two"], "one"),
    "new_type": (NewType("Identifier", int), 1),
    "sequence": (Sequence[int], [1, 2]),
    "mapping": (Mapping[str, int], {"one": 1}),
    "constrained": (Annotated[int, msgspec.Meta(ge=0)], 1),
    "nested_struct": (msgspec.defstruct("Child", [("x", int)]), {"x": 1}),
}


@pytest.mark.parametrize("name", TYPES)
def test_type_matrix(name):
    annotation, value = TYPES[name]
    schema = msgspec.defstruct("Matrix", [("value", annotation)])
    data = {"value": value}
    reference = msgspec.convert(data, schema, strict=True)
    matrix = serializer_for(schema)
    serializer = matrix(data=data)
    assert serializer.is_valid(), serializer.errors
    assert exact(serializer.validated_data["value"]) == exact(reference.value)
    assert matrix(reference).data == msgspec.to_builtins(reference)
    assert matrix([reference], many=True).data == msgspec.to_builtins([reference])


def test_serializer_for_takes_structs_only():
    assert serializer_for(dict) is None
    assert serializer_for(Plain(value=1)) is None
    assert issubclass(serializer_for(Plain), MsgspecSerializer)


def _renamed(name):
    return msgspec.defstruct("Wire", [("value", int)], rename={"value": name})


@pytest.mark.parametrize("name", ["first.last", "value[0]", "value`end", "a b"])
@pytest.mark.parametrize("given", [True, False])
def test_an_error_is_keyed_by_the_wire_name_as_given(name, given):
    serializer = serializer_for(_renamed(name))(data={name: "bad"} if given else {})
    assert not serializer.is_valid()
    assert list(serializer.errors) == [name]
    (error,) = serializer.errors[name]
    assert error.code == ("invalid" if given else "required")
    assert "- at" not in str(error)


def test_a_nested_wire_name_with_a_dot_keeps_its_place():
    Item = msgspec.defstruct("Item", [("value", int)], rename={"value": "x.y"})
    Holder = msgspec.defstruct("Holder", [("items", list[Item])])
    serializer = serializer_for(Holder)(data={"items": [{"x.y": 1}, {"x.y": "b"}]})
    assert not serializer.is_valid()
    assert list(serializer.errors["items"][1]) == ["x.y"]
