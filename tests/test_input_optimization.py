"""Rejection shortcuts cannot change validation or call arbitrary input code."""

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
