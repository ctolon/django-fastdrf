"""
Hooks a project assigns to a serializer or field instance keep its input on
DRF's validation, whatever the compiled recognizers already hold.
"""

import pytest
from django.test import override_settings
from rest_framework import serializers as drf

from fastdrf import serializers

BACKENDS = ["msgspec", "pydantic"]


class Item(serializers.Serializer):
    value = drf.IntegerField()


class Container(serializers.Serializer):
    children = Item(many=True)


def reject(data):
    raise drf.ValidationError("reject nested child")


def plus_ten(dictionary, keys, value):
    dictionary[keys[-1]] = value + 10


def outcome(serializer):
    valid = serializer.is_valid()
    return valid, serializer.errors if not valid else dict(serializer.validated_data)


def hooked_container():
    serializer = Container(data={"children": [{"value": 1}]})
    serializer.fields["children"].run_child_validation = reject
    return serializer


def hooked_item():
    serializer = Item(data={"value": 1})
    serializer.set_value = plus_ten
    return serializer


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("hooked", [hooked_container, hooked_item])
def test_an_instance_hook_keeps_drfs_validation(backend, hooked):
    expected = outcome(hooked())
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        # Recognizers compiled for plain instances first.
        assert outcome(Container(data={"children": [{"value": 1}]}))[0]
        assert outcome(Item(data={"value": 1}))[0]
        assert outcome(hooked()) == expected
        assert outcome(Item(data={"value": 1})) == (True, {"value": 1})


class Kebab(serializers.Serializer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["display-name"] = drf.CharField(source="name")
        self.fields["class"] = drf.IntegerField(required=False)


class FloatBound(serializers.Serializer):
    low = drf.IntegerField(min_value=1.5, required=False)
    high = drf.IntegerField(max_value=2.5, required=False)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("serializer_class", "data"),
    [
        (Kebab, {"display-name": "Ada", "class": 1}),
        (FloatBound, {"low": 2, "high": 2}),
        (FloatBound, {"low": 1}),
        (FloatBound, {"high": 3}),
    ],
)
def test_input_drf_accepts_is_validated_as_drf_validates_it(
    backend, serializer_class, data
):
    expected = outcome(serializer_class(data=data))
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert outcome(serializer_class(data=data)) == expected


def _serializer(**declared):
    return type("Probe", (serializers.Serializer,), declared)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (lambda: drf.DateField(input_formats=[]), "2026-10-02"),
        (lambda: drf.DateField(input_formats=()), "2026-10-02"),
        (lambda: drf.TimeField(input_formats=[]), "12:34:56"),
        (lambda: drf.DateField(), "2026-10-02"),
        (lambda: drf.TimeField(), "12:34:56"),
    ],
)
def test_no_input_formats_reads_no_text(backend, field, value):
    serializer_class = _serializer(x=field())
    expected = outcome(serializer_class(data={"x": value}))
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert outcome(serializer_class(data={"x": value})) == expected


class DynamicDate(serializers.Serializer):
    def __init__(self, *args, formats=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["x"] = (
            drf.DateField() if formats is None else drf.DateField(input_formats=formats)
        )


@pytest.mark.parametrize("backend", BACKENDS)
def test_default_and_empty_formats_are_told_apart_per_instance(backend):
    data = {"x": "2026-10-02"}
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        for formats in (None, [], None, []):
            expected = outcome(DynamicDate(data=data, formats=formats))
            assert expected[0] is (formats is None)
            assert outcome(DynamicDate(data=data, formats=formats)) == expected


EDGE = 2**53


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (lambda: drf.FloatField(min_value=EDGE + 1), float(EDGE)),
        (lambda: drf.FloatField(min_value=EDGE + 1), float(EDGE + 2)),
        (lambda: drf.FloatField(max_value=EDGE + 3), float(EDGE + 4)),
        (lambda: drf.FloatField(max_value=EDGE + 3), float(EDGE + 2)),
        (lambda: drf.FloatField(max_value=-(EDGE + 1)), float(-EDGE)),
        (lambda: drf.FloatField(min_value=-(EDGE + 3)), float(-(EDGE + 4))),
        (lambda: drf.FloatField(min_value=11), 10.0),
    ],
)
def test_a_float_bound_is_drfs_exactly(backend, field, value):
    serializer_class = _serializer(x=field())

    def many_outcome(serializer):
        valid = serializer.is_valid()
        return valid, serializer.errors if not valid else serializer.validated_data

    for data, many in (({"x": value}, False), ([{"x": value}], True)):
        expected = many_outcome(serializer_class(data=data, many=many))
        with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
            assert many_outcome(serializer_class(data=data, many=many)) == expected


class DefaultChild(serializers.Serializer):
    a = drf.IntegerField()

    def get_default(self):
        return {"a": 42}


class InheritedDefaultChild(DefaultChild):
    pass


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("child", [DefaultChild, InheritedDefaultChild])
@pytest.mark.parametrize("data", [{}, {"child": {"a": 1}}])
@pytest.mark.parametrize("partial", [False, True])
def test_a_nested_serializers_own_default_is_drfs(backend, child, data, partial):
    parent = _serializer(child=child(required=False))
    expected = outcome(parent(data=data, partial=partial))
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert outcome(parent(data=data, partial=partial)) == expected
