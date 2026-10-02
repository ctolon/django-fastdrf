"""Rejection shortcuts cannot change validation or call arbitrary input code."""

import datetime
import decimal
import uuid
from unittest.mock import patch

import pytest
from django.test import override_settings

from fastdrf import inputs, serializers


class Scalars(serializers.Serializer):
    name = serializers.CharField(max_length=20)
    count = serializers.IntegerField(min_value=0, allow_null=True)
    price = serializers.FloatField()
    active = serializers.BooleanField()


CANONICAL = {"name": "item", "count": 1, "price": 2.5, "active": True}


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_first_row_coercion_declines_without_scanning_the_batch(backend):
    data = [{**CANONICAL, "count": "1"} for _ in range(100)]
    with patch.object(
        inputs, "_plain_input", side_effect=AssertionError("batch was traversed")
    ):
        assert (
            inputs.recognize(Scalars(data=data, many=True), backend=backend)
            is inputs.NOT_RECOGNIZED
        )


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize("position", [0, 99])
@pytest.mark.parametrize("value", ["1", "bad", None, True, 1, 1.0])
def test_early_and_late_declines_preserve_complete_drf_errors(backend, position, value):
    data = [dict(CANONICAL) for _ in range(100)]
    data[position]["count"] = value
    reference = Scalars(data=data, many=True)
    valid = reference.is_valid()
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        candidate = Scalars(data=data, many=True)
        assert candidate.is_valid() is valid
    assert candidate.errors == reference.errors
    assert candidate.validated_data == reference.validated_data
    for left, right in zip(
        candidate.validated_data, reference.validated_data, strict=True
    ):
        assert type(left["count"]) is type(right["count"])


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_rejection_is_not_learned_for_later_requests(backend):
    for data in ({**CANONICAL, "count": "1"}, CANONICAL, {**CANONICAL, "count": None}):
        result = inputs.recognize(Scalars(data=[data], many=True), backend=backend)
        if isinstance(data["count"], str):
            assert result is inputs.NOT_RECOGNIZED
        else:
            assert result == [data]


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_preflight_does_not_compare_non_string_dictionary_keys(backend):
    class Key:
        def __hash__(self):
            return hash("count")

        def __eq__(self, other):
            raise AssertionError("input comparison was called")

    data = {Key(): 1}
    assert (
        inputs.recognize(Scalars(data=[data], many=True), backend=backend)
        is inputs.NOT_RECOGNIZED
    )


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_direct_and_nested_sources_keep_drf_assignment_order(backend):
    class Aliases(serializers.Serializer):
        first = serializers.IntegerField(source="value")
        second = serializers.IntegerField(source="value")
        nested = serializers.IntegerField(source="detail.count")

    data = {"first": 1, "second": 2, "nested": 3}
    reference = Aliases(data=data)
    assert reference.is_valid()
    assert (
        inputs.recognize(Aliases(data=data), backend=backend)
        == reference.validated_data
    )


class Text(str):
    pass


class Mapping(dict):
    pass


PLAIN = [
    None,
    True,
    1,
    1.5,
    "text",
    datetime.date(2026, 10, 1),
    datetime.time(12, 0),
    uuid.UUID(int=1),
    [],
    {},
    {"a": [1, {"b": [None, "c"]}], "d": {"e": {"f": 1.0}}},
]
NOT_PLAIN = [
    datetime.datetime(2026, 10, 1),
    decimal.Decimal("1.5"),
    Text("text"),
    Mapping(),
    (1, 2),
    b"bytes",
    object(),
    {1: "non-string key"},
]


@pytest.mark.parametrize("value", PLAIN, ids=repr)
@pytest.mark.parametrize("wrap", ["bare", "list", "dict", "deep"])
def test_plain_input_accepts_what_a_json_parser_produces(value, wrap):
    assert inputs._plain_input(_wrapped(value, wrap))


@pytest.mark.parametrize("value", NOT_PLAIN, ids=repr)
@pytest.mark.parametrize("wrap", ["bare", "list", "dict", "deep"])
def test_plain_input_declines_other_python_objects(value, wrap):
    assert not inputs._plain_input(_wrapped(value, wrap))


def _wrapped(value, wrap):
    if wrap == "bare":
        return value
    if wrap == "list":
        return [1, "a", value, None]
    if wrap == "dict":
        return {"a": 1, "b": value, "c": None}
    return {"a": [{"b": 1}, {"c": [None, {"d": value}]}], "e": 2}


def test_recursive_input_raises_for_the_recognizer_to_decline():
    data = []
    data.append(data)
    with pytest.raises(RecursionError):
        inputs._plain_input(data)


class Tagged(serializers.Serializer):
    tags = serializers.ListField(child=serializers.CharField(allow_null=True))
    labels = serializers.DictField(child=serializers.CharField(allow_null=True))


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize(
    ("data", "recognized"),
    [
        ({"tags": ["a", None, "b"], "labels": {"x": "a", "y": None}}, True),
        ({"tags": ["a", " padded "], "labels": {}}, False),
        ({"tags": [], "labels": {"x": "a", "y": " padded "}}, False),
        ({"tags": ["a", "x\x00"], "labels": {}}, False),
    ],
    ids=["plain", "list-child", "dict-child", "list-nul"],
)
def test_collection_children_decline_what_drf_changes(backend, data, recognized):
    reference = Tagged(data=data)
    reference.is_valid()
    result = inputs.recognize(Tagged(data=data), backend=backend)
    if recognized:
        assert result == reference.validated_data
    else:
        assert result is inputs.NOT_RECOGNIZED
