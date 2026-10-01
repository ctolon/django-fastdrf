"""``PydanticSerializer``: pydantic's rules for validation and representation."""

import datetime
import decimal
import enum
import gc
import ipaddress
import uuid
import weakref
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, NamedTuple, NewType

import pydantic
import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import QueryDict
from django.test import override_settings
from pydantic.alias_generators import to_camel
from rest_framework.renderers import JSONRenderer
from rest_framework.settings import api_settings
from typing_extensions import TypedDict

from fastdrf.pydantic.serializers import PydanticSerializer, serializer_for
from fastdrf.typed import adapt, schema_serializer
from tests.models import Author
from tests.test_inputs import exact

SECRET = "private-token"


def serializer_class(schema, **meta):
    return type(
        "Serializer",
        (PydanticSerializer,),
        {"Meta": type("Meta", (), {"schema": schema, **meta})},
    )


def rendered(serializer):
    data = serializer.data
    assert SECRET.encode() not in JSONRenderer().render(data)
    return data


# -- Validation -----------------------------------------------------------------------


class Checked(pydantic.BaseModel):
    value: int

    @pydantic.field_validator("value")
    @classmethod
    def positive(cls, value):
        if value < 0:
            raise ValueError("positive required")
        return value


def test_errors_are_drfs_with_every_pydantic_message():
    serializer = serializer_class(Checked)(data={"value": -1})
    assert not serializer.is_valid()
    assert serializer.errors == {"value": ["Value error, positive required"]}
    assert serializer.errors["value"][0].code == "value_error"
    serializer = serializer_class(Checked)(data={})
    assert not serializer.is_valid()
    assert serializer.errors["value"][0].code == "required"


def test_lax_input_is_the_default_and_strict_can_be_chosen():
    lax = serializer_class(Checked)(data={"value": "1"})
    assert lax.is_valid(), lax.errors
    assert lax.validated_data == {"value": 1}
    assert not serializer_class(Checked, strict=True)(data={"value": "1"}).is_valid()


def test_models_with_validators_need_a_partial_schema():
    with pytest.raises(ImproperlyConfigured, match="partial_schema"):
        serializer_class(Checked)(data={}, partial=True).is_valid()


class CountedDefault(pydantic.BaseModel):
    count: int = pydantic.Field(default=0, validate_default=True)
    note: str = ""


def test_fields_validating_defaults_need_a_partial_schema():
    with pytest.raises(ImproperlyConfigured, match="validates defaults"):
        serializer_class(CountedDefault)(data={"note": "x"}, partial=True).is_valid()


def test_root_models_need_a_partial_schema():
    serializer = adapt(pydantic.RootModel[list[int]])(data=[1], partial=True)
    with pytest.raises(ImproperlyConfigured, match="partial_schema"):
        serializer.is_valid()


class Choices(pydantic.BaseModel):
    x: int | bool
    d: dict[int, int] = {}
    items: list[int | bool] = []


def test_errors_are_keyed_by_the_inputs_fields_and_indexes():
    serializer = adapt(Choices)(data={"x": "abc", "d": {"k": 1}, "items": [1, "abc"]})
    assert not serializer.is_valid()
    errors = serializer.errors
    assert set(errors) == {"x", "d", "items"}
    assert isinstance(errors["x"], list)
    assert all(isinstance(message, str) for message in errors["x"])
    assert set(errors["d"]) == {"k"}
    assert isinstance(errors["d"]["k"], list)
    item_errors = errors["items"][1]
    assert isinstance(item_errors, list)
    if getattr(api_settings, "LIST_SERIALIZER_ERRORS_AS_DICT", False):
        assert errors["items"] == {1: item_errors}
    else:
        assert errors["items"] == [{}, item_errors]


class Item(pydantic.BaseModel):
    name: str


class Numbers(pydantic.RootModel[list[int]]):
    pass


class Items(pydantic.RootModel[list[Item]]):
    pass


@pytest.mark.parametrize("as_dict", [False, True])
@pytest.mark.parametrize(
    ("schema", "data", "errors"),
    [
        (
            Numbers,
            [1, "a"],
            {
                1: [
                    "Input should be a valid integer, unable to parse string as an integer"
                ]
            },
        ),
        (Items, [{"name": "a"}, {}], {1: {"name": ["This field is required."]}}),
    ],
)
def test_errors_of_a_list_root_model_are_keyed_by_position(
    schema, data, errors, as_dict
):
    rest_framework = {
        **settings.REST_FRAMEWORK,
        "LIST_SERIALIZER_ERRORS_AS_DICT": as_dict,
    }
    with override_settings(REST_FRAMEWORK=rest_framework):
        serializer = adapt(schema)(data=data)
        assert not serializer.is_valid()
        assert serializer.errors == errors


class AliasedModel(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(alias_generator=to_camel)
    first_name: str
    page_count: int = 1


def test_the_data_of_validated_input_is_named_as_on_the_wire():
    body = {"firstName": "Ada", "pageCount": 3}
    serializer = adapt(AliasedModel)(data=body)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"first_name": "Ada", "page_count": 3}
    assert serializer.data == body
    partial = adapt(AliasedModel)(data={"pageCount": 4}, partial=True)
    assert partial.is_valid(), partial.errors
    assert partial.data == {"pageCount": 4}


class Aliased(pydantic.BaseModel):
    value: int = pydantic.Field(serialization_alias="public_value")


def test_output_uses_the_serialization_aliases():
    aliased = serializer_class(Aliased)
    assert aliased(Aliased(value=1)).data == {"public_value": 1}
    assert aliased([Aliased(value=1)], many=True).data == [{"public_value": 1}]
    assert list(aliased().fields) == ["public_value"]


# -- Form input and aliases ------------------------------------------------------------


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("key", ["tags", "labels"])
def test_form_alias_choices_keep_repeated_values(key, partial):
    class Payload(pydantic.BaseModel):
        tags: list[str] = pydantic.Field(
            validation_alias=pydantic.AliasChoices("tags", "labels")
        )

    serializer = adapt(Payload)(data=QueryDict(f"{key}=one&{key}=two"), partial=partial)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"tags": ["one", "two"]}


@pytest.mark.parametrize("partial", [False, True])
def test_a_form_alias_path_is_a_validation_error(partial):
    class Payload(pydantic.BaseModel):
        tags: list[str] = pydantic.Field(
            validation_alias=pydantic.AliasPath("body", "tags")
        )

    # Flat form input cannot express this nested path: a 400, not a 500.
    serializer = adapt(Payload)(data=QueryDict("body=one&body=two"), partial=partial)
    if partial:
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data == {}
    else:
        assert not serializer.is_valid()
        assert serializer.errors["body"]["tags"][0].code == "required"


def test_a_missing_alias_path_is_reported_where_the_input_stops():
    class Payload(pydantic.BaseModel):
        value: int = pydantic.Field(
            validation_alias=pydantic.AliasPath("payload", "value")
        )

    serializer = adapt(Payload)(data={})
    assert not serializer.is_valid()
    assert list(serializer.errors) == ["payload"]
    assert serializer.errors["payload"][0].code == "required"
    serializer = adapt(Payload)(data={"payload": {}})
    assert not serializer.is_valid()
    assert serializer.errors["payload"]["value"][0].code == "required"


@pytest.mark.parametrize("partial", [False, True])
def test_form_populate_by_name_keeps_the_collection(partial):
    class Payload(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(populate_by_name=True)
        tags: list[str] = pydantic.Field(alias="labels")

    serializer = adapt(Payload)(data=QueryDict("tags=one&tags=two"), partial=partial)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"tags": ["one", "two"]}


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("choice", [False, True])
def test_a_single_segment_alias_path_keeps_the_collection(partial, choice):
    alias = pydantic.AliasPath("labels")
    if choice:
        alias = pydantic.AliasChoices("other", alias)

    class Payload(pydantic.BaseModel):
        tags: list[str] = pydantic.Field(validation_alias=alias)

    serializer = adapt(Payload)(
        data=QueryDict("labels=one&labels=two"), partial=partial
    )
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"tags": ["one", "two"]}


def test_form_input_does_not_enable_population_by_name():
    class Payload(pydantic.BaseModel):
        tags: list[str] = pydantic.Field(alias="labels")

    serializer = adapt(Payload)(data=QueryDict("tags=one&tags=two"))
    assert not serializer.is_valid()
    assert serializer.errors["labels"][0].code == "required"


@pytest.mark.parametrize("partial", [False, True])
def test_nested_collection_constraints_survive_adaptation(partial):
    class Payload(pydantic.BaseModel):
        values: list[Annotated[str, pydantic.Field(min_length=5)]] | None

    invalid = adapt(Payload)(data={"values": ["no"]}, partial=partial)
    assert not invalid.is_valid()
    assert invalid.errors["values"][0][0].code == "string_too_short"
    for value in (["valid"], None):
        valid = adapt(Payload)(data={"values": value}, partial=partial)
        assert valid.is_valid(), valid.errors
        assert valid.validated_data == {"values": value}


# -- Partial input: the output model's serializers, aliases and exclusions ------------


class Redacted(pydantic.BaseModel):
    secret: str
    note: str | None = None

    @pydantic.field_serializer("secret")
    def hide(self, value):
        return "[redacted]"


class WholeRedacted(pydantic.BaseModel):
    secret: str

    @pydantic.model_serializer
    def hide(self):
        return {"secret": "[redacted]"}


class Account(pydantic.BaseModel):
    name: str
    secret: str
    token: str = ""


class PublicAccount(pydantic.BaseModel):
    name: str = pydantic.Field(serialization_alias="displayName")
    secret: str | None = None
    token: str = pydantic.Field("", exclude=True)

    @pydantic.field_serializer("secret")
    def hide(self, value):
        return None if value is None else "[redacted]"

    @pydantic.computed_field
    @property
    def initial(self) -> str:
        return self.name[0]


@pytest.mark.parametrize("partial", [False, True])
def test_partial_data_uses_the_field_serializers(partial):
    serializer = adapt(Redacted)(data={"secret": SECRET}, partial=partial)
    assert serializer.is_valid(), serializer.errors
    expected = (
        {"secret": "[redacted]"} if partial else {"secret": "[redacted]", "note": None}
    )
    assert rendered(serializer) == expected


@pytest.mark.parametrize("partial", [False, True])
def test_partial_data_keeps_null_and_leaves_out_what_was_not_given(partial):
    serializer = adapt(Redacted)(data={"note": None}, partial=partial)
    assert serializer.is_valid() is partial
    if partial:
        assert rendered(serializer) == {"note": None}


def test_a_model_serializer_cannot_represent_partial_input():
    serializer = adapt(WholeRedacted)(data={"secret": SECRET})
    assert serializer.is_valid(), serializer.errors
    assert rendered(serializer) == {"secret": "[redacted]"}

    serializer = adapt(WholeRedacted)(data={"secret": SECRET}, partial=True)
    assert serializer.is_valid(), serializer.errors
    with pytest.raises(TypeError, match="model_serializer"):
        serializer.data  # noqa: B018


@pytest.mark.parametrize("partial", [False, True])
def test_partial_data_is_the_output_schemas(partial):
    body = {"name": "Ada", "secret": SECRET, "token": SECRET}
    serializer = schema_serializer(Account, PublicAccount)(data=body, partial=partial)
    assert serializer.is_valid(), serializer.errors
    expected = {"displayName": "Ada", "secret": "[redacted]"}
    # The given fields only: a computed field could read one left out.
    assert rendered(serializer) == (
        expected if partial else {**expected, "initial": "A"}
    )


def test_partial_data_leaves_out_the_fields_not_given():
    accounts = schema_serializer(Account, PublicAccount)
    serializer = accounts(data={"secret": SECRET}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert rendered(serializer) == {"secret": "[redacted]"}
    serializer = accounts(data={}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert rendered(serializer) == {}


# -- Context, root values and extra values --------------------------------------------


class ContextModel(pydantic.BaseModel):
    value: int

    @pydantic.field_validator("value")
    @classmethod
    def add_offset(cls, value, info):
        return value + info.context["offset"]

    @pydantic.field_serializer("value")
    def display(self, value, info):
        return value * info.context["scale"]


@pytest.mark.parametrize("many", [False, True])
def test_the_context_reaches_validation_and_output(many):
    payload = [{"value": 1}] if many else {"value": 1}
    context = {"offset": 2, "scale": 10}
    serializer = adapt(ContextModel)(data=payload, many=many, context=context)
    assert serializer.is_valid(), serializer.errors
    expected = {"value": 30}
    assert serializer.data == ([expected] if many else expected)
    # Output of a mapping validates it first, with the same context.
    serializer = adapt(ContextModel)(payload, many=many, context=context)
    assert serializer.data == ([expected] if many else expected)


def test_the_cached_list_adapter_does_not_retain_the_context():
    class Context:
        pass

    context = Context()
    reference = weakref.ref(context)
    serializer = adapt(ContextModel)(
        [{"value": 1}],
        many=True,
        context={"offset": 1, "scale": 10, "request": context},
    )
    assert serializer.data == [{"value": 20}]
    del serializer, context
    gc.collect()
    assert reference() is None


class ScalarModel(pydantic.BaseModel):
    value: int

    @pydantic.model_serializer
    def scalar(self) -> int:
        return self.value


@pytest.mark.parametrize(
    ("schema", "payload", "expected"),
    [
        (pydantic.RootModel[list[int]], [1, 2], [1, 2]),
        (pydantic.RootModel[int], 3, 3),
        (pydantic.RootModel[str], "value", "value"),
        (pydantic.RootModel[bool], False, False),
        (ScalarModel, {"value": 3}, 3),
    ],
)
@pytest.mark.parametrize("many", [False, True])
def test_output_keeps_the_shape_of_the_schema(schema, payload, expected, many):
    serializer = adapt(schema)(data=[payload] if many else payload, many=many)
    assert serializer.is_valid(), serializer.errors
    assert serializer.data == ([expected] if many else expected)


@pytest.mark.parametrize("many", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_allowed_extra_values_are_not_lost(many, partial):
    class OpenModel(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="allow")
        value: int

    payload = {"value": 1, "extra": "retained"}
    serializer = adapt(OpenModel)(
        data=[payload] if many else payload, many=many, partial=partial
    )
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == ([payload] if many else payload)
    assert serializer.data == ([payload] if many else payload)


def test_model_instances_are_read_by_attribute():
    class AuthorOut(pydantic.BaseModel):
        id: int
        name: str

    authors = [Author(pk=1, name="a"), Author(pk=2, name="b")]
    assert adapt(AuthorOut)(authors[0]).data == {"id": 1, "name": "a"}
    assert adapt(AuthorOut)(authors, many=True).data == [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
    ]


# -- Datetimes are pydantic's, not Django's --------------------------------------------


class Stamp(pydantic.BaseModel):
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
def test_datetime_validation_and_output_match_pydantic(value):
    native = Stamp.model_validate({"when": value}, strict=True)
    expected = native.model_dump(mode="json", by_alias=True)
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
def test_iso_input_keeps_pydantics_strictness(strict, value):
    try:
        native = Stamp.model_validate({"when": value}, strict=strict or None)
    except pydantic.ValidationError:
        native = None
    serializer = serializer_class(Stamp, strict=strict)(data={"when": value})
    assert serializer.is_valid() is (native is not None)
    if native is not None:
        assert serializer.validated_data["when"] == native.when


# -- Every type pydantic supports -------------------------------------------------------


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


# name: (annotation, value, strict)
TYPES = {
    "none": (type(None), None, True),
    "bool": (bool, True, True),
    "int": (int, 12, True),
    "float": (float, 1.25, True),
    "str": (str, "text", True),
    "bytes": (bytes, b"text", True),
    "list": (list[int], [1, 2], True),
    "dict": (dict[str, int], {"one": 1}, True),
    "tuple": (tuple[int, str], (1, "one"), True),
    "variable_tuple": (tuple[int, ...], (1, 2), True),
    "set": (set[int], {1}, True),
    "frozenset": (frozenset[int], frozenset({1}), True),
    "datetime": (
        datetime.datetime,
        datetime.datetime(2026, 9, 27, tzinfo=datetime.UTC),
        True,
    ),
    "date": (datetime.date, datetime.date(2026, 9, 27), True),
    "time": (datetime.time, datetime.time(12, 30), True),
    "duration": (datetime.timedelta, datetime.timedelta(seconds=90), True),
    "uuid": (uuid.UUID, uuid.UUID(int=1), True),
    "decimal": (decimal.Decimal, decimal.Decimal("1.250"), True),
    "enum": (State, State.ready, True),
    "int_enum": (Number, Number.one, True),
    "str_enum": (Label, Label.ready, True),
    "int_flag": (Flags, Flags.read | Flags.write, True),
    "dataclass": (Pair, Pair(1, 2), True),
    "named_tuple": (Position, Position(1, 2), True),
    "typed_dict": (Coordinates, {"x": 1, "y": 2}, True),
    "any": (Any, {"x": [1, True, None]}, True),
    "optional": (int | None, None, True),
    "union": (int | str, "one", True),
    "literal": (Literal["one", "two"], "one", True),
    "new_type": (NewType("Identifier", int), 1, True),
    "sequence": (Sequence[int], [1, 2], True),
    "mapping": (Mapping[str, int], {"one": 1}, True),
    "path": (Path, Path("example.txt"), True),
    "ipv4": (ipaddress.IPv4Address, ipaddress.IPv4Address("127.0.0.1"), True),
    "ipv6": (ipaddress.IPv6Address, ipaddress.IPv6Address("::1"), True),
    "network": (ipaddress.IPv4Network, ipaddress.IPv4Network("192.0.2.0/24"), True),
    "url": (pydantic.AnyUrl, pydantic.AnyUrl("https://example.com/"), True),
    "secret": (pydantic.SecretStr, pydantic.SecretStr("example"), True),
    "deque": (deque[int], [1, 2], False),
    "constrained": (Annotated[int, pydantic.Field(ge=0)], 1, True),
    "nested_model": (pydantic.create_model("Child", x=(int, ...)), {"x": 1}, True),
}


@pytest.mark.parametrize("name", TYPES)
def test_type_matrix(name):
    annotation, value, strict = TYPES[name]
    schema = pydantic.create_model("Matrix", value=(annotation, ...))
    data = {"value": value}
    reference = schema.model_validate(data, strict=strict)
    matrix = serializer_for(schema)
    matrix.Meta.strict = strict
    serializer = matrix(data=data)
    assert serializer.is_valid(), serializer.errors
    assert exact(serializer.validated_data["value"]) == exact(reference.value)
    assert matrix(reference).data == reference.model_dump(mode="json")
    assert matrix([reference], many=True).data == [reference.model_dump(mode="json")]


def test_serializer_for_takes_models_only():
    assert serializer_for(dict) is None
    assert serializer_for(Item(name="a")) is None
    assert issubclass(serializer_for(Item), PydanticSerializer)
