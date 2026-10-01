"""Canonical collection and temporal input must not require DRF fallback."""

import datetime
import gc
import uuid
import weakref
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from rest_framework import serializers

from fastdrf import inputs
from tests.test_inputs import exact


class CollectionSerializer(serializers.Serializer):
    dates = serializers.ListField(child=serializers.DateField(allow_null=True))
    identifiers = serializers.DictField(child=serializers.UUIDField())
    times = serializers.ListField(child=serializers.TimeField())
    matrix = serializers.ListField(
        child=serializers.DictField(child=serializers.IntegerField(min_value=0))
    )


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_canonical_collections_are_recognized(backend):
    data = {
        "dates": ["2026-09-27", None],
        "identifiers": {"one": str(uuid.UUID(int=1))},
        "times": ["12:34:56.123456"],
        "matrix": [{"one": 1}],
    }
    reference = CollectionSerializer(data=data)
    assert reference.is_valid(), reference.errors
    recognized = inputs.recognize(CollectionSerializer(data=data), backend=backend)
    assert recognized is not inputs.NOT_RECOGNIZED
    assert exact(recognized) == exact(reference.validated_data)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@settings(derandomize=True, database=None, deadline=None, max_examples=75)
@given(st.lists(st.one_of(st.integers(), st.text(), st.none()), max_size=5))
def test_collection_recognition_preserves_drf_values(backend, values):
    class Example(serializers.Serializer):
        values = serializers.ListField(
            child=serializers.IntegerField(min_value=0, allow_null=True),
            min_length=1,
            max_length=4,
        )

    data = {"values": values}
    reference = Example(data=data)
    valid = reference.is_valid()
    recognized = inputs.recognize(Example(data=data), backend=backend)
    if recognized is not inputs.NOT_RECOGNIZED:
        assert valid
        assert exact(recognized) == exact(reference.validated_data)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_collection_cache_tracks_child_options_and_instance_hooks(backend):
    class Example(serializers.Serializer):
        values = serializers.ListField(child=serializers.IntegerField(min_value=0))

    data = {"values": [2]}
    assert inputs.recognize(Example(data=data), backend=backend) == data
    changed = Example(data=data)
    changed.fields["values"].child = serializers.CharField()
    assert inputs.recognize(changed, backend=backend) is inputs.NOT_RECOGNIZED
    hooked = Example(data=data)
    with patch.object(hooked.fields["values"].child, "run_validation", return_value=9):
        assert inputs.recognize(hooked, backend=backend) is inputs.NOT_RECOGNIZED
    assert inputs.recognize(Example(data=data), backend=backend) == data


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (serializers.DateField(), "2026-09-27"),
        (serializers.UUIDField(), str(uuid.UUID(int=1))),
        (serializers.TimeField(), "12:34:56.123456"),
        (serializers.TimeField(), datetime.time(12, 30)),
    ],
)
def test_canonical_temporal_values_are_recognized(backend, field, value):
    serializer = type("Example", (serializers.Serializer,), {"value": field})
    data = {"value": value}
    reference = serializer(data=data)
    assert reference.is_valid()
    recognized = inputs.recognize(serializer(data=data), backend=backend)
    assert recognized is not inputs.NOT_RECOGNIZED
    assert exact(recognized) == exact(reference.validated_data)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize("value", ["12:30:00Z", "12:30:00+03:00", "12:30", "bad", 1])
def test_time_strings_never_change_drf_semantics(backend, value):
    class Example(serializers.Serializer):
        value = serializers.TimeField()

    reference = Example(data={"value": value})
    valid = reference.is_valid()
    recognized = inputs.recognize(Example(data={"value": value}), backend=backend)
    if recognized is not inputs.NOT_RECOGNIZED:
        assert valid
        assert exact(recognized) == exact(reference.validated_data)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize(
    "value", ["10:00:00.1234567", "10:00:59.9999999", "23:59:59.9999999"]
)
def test_digits_beyond_microseconds_are_truncated_as_django_does(backend, value):
    # Django's ``parse_time`` drops them; rounding could reach the next
    # minute, or midnight.
    class Example(serializers.Serializer):
        value = serializers.TimeField()
        values = serializers.ListField(child=serializers.TimeField())

    data = {"value": value, "values": [value]}
    reference = Example(data=data)
    assert reference.is_valid(), reference.errors
    recognized = inputs.recognize(Example(data=data), backend=backend)
    assert recognized is not inputs.NOT_RECOGNIZED
    assert exact(recognized) == exact(reference.validated_data)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_collection_empty_partial_and_invalid_contracts(backend):
    class Example(serializers.Serializer):
        items = serializers.DictField(
            child=serializers.CharField(min_length=2), allow_empty=False
        )

    assert inputs.recognize(Example(data={}, partial=True), backend=backend) == {}
    for data in ({"items": {}}, {"items": {"a": "x"}}, {"items": {1: "ok"}}):
        assert (
            inputs.recognize(Example(data=data), backend=backend)
            is inputs.NOT_RECOGNIZED
        )
    data = {"items": {"a": "ok"}}
    assert inputs.recognize(Example(data=data), backend=backend) == data


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_collection_custom_child_validator_is_not_skipped(backend):
    def transform(value):
        raise serializers.ValidationError("application constraint")

    class Example(serializers.Serializer):
        items = serializers.ListField(
            child=serializers.IntegerField(validators=[transform])
        )

    data = {"items": [1]}
    assert (
        inputs.recognize(Example(data=data), backend=backend) is inputs.NOT_RECOGNIZED
    )
    reference = Example(data=data)
    assert not reference.is_valid()
    assert str(reference.errors["items"][0][0]) == "application constraint"


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_nested_serializer_inside_collection_preserves_partial_updates(backend):
    class Child(serializers.Serializer):
        name = serializers.CharField()
        amount = serializers.IntegerField(default=3)

    class Example(serializers.Serializer):
        children = serializers.DictField(child=Child())

    data = {"children": {"first": {"name": "one"}}}
    for partial in (False, True):
        reference = Example(data=data, partial=partial)
        assert reference.is_valid()
        recognized = inputs.recognize(
            Example(data=data, partial=partial), backend=backend
        )
        assert recognized is not inputs.NOT_RECOGNIZED
        assert exact(recognized) == exact(reference.validated_data)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_cached_collection_plan_does_not_retain_request_context(backend):
    class RequestContext:
        pass

    context = RequestContext()
    serializer = CollectionSerializer(
        data={"dates": [], "identifiers": {}, "times": [], "matrix": []},
        context={"request": context},
    )
    context_ref = weakref.ref(context)
    serializer_ref = weakref.ref(serializer)
    child_ref = weakref.ref(serializer.fields["dates"].child)
    assert inputs.recognize(serializer, backend=backend) is not inputs.NOT_RECOGNIZED
    del serializer, context
    gc.collect()
    assert context_ref() is serializer_ref() is child_ref() is None


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize(
    "items",
    [{"a": "x", "b": None}, {"a": " x"}, {"a": 1}, {"a": ""}, {1: "x"}, []],
)
def test_hstore_input_is_drfs_or_recognized_as_drf_returns_it(backend, items):
    # DRF's HStoreField: a DictField of nullable, blank-allowing strings.
    class Example(serializers.Serializer):
        items = serializers.HStoreField()

    data = {"items": items}
    reference = Example(data=data)
    valid = reference.is_valid()
    recognized = inputs.recognize(Example(data=data), backend=backend)
    if recognized is not inputs.NOT_RECOGNIZED:
        assert valid, reference.errors
        assert exact(recognized) == exact(reference.validated_data)
    if items == {"a": "x", "b": None}:
        assert recognized == {"items": items}
