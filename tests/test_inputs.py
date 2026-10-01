"""
The input recognizer against DRF, with generated input.

``fastdrf.inputs`` may only accept what DRF accepts, and must then
produce DRF's ``validated_data`` exactly. The strategies below mix canonical
values with everything a JSON body can contain, and the pinned examples are
the differences between msgspec and DRF found while the recognizer was
written.
"""

import datetime
import itertools
import uuid

import pytest
from django.core.validators import (
    MaxLengthValidator,
    MinLengthValidator,
    MinValueValidator,
)
from django.http import QueryDict
from django.test import override_settings
from hypothesis import example, given, settings
from hypothesis import strategies as st
from rest_framework import serializers as drf

from fastdrf import inputs
from fastdrf import serializers as drf_serializers
from tests.models import Author


def _is_valid(serializer):
    return serializer.is_valid()


PROFILE = settings(derandomize=True, database=None, deadline=None, max_examples=200)


class LineSerializer(drf_serializers.Serializer):
    sku = drf_serializers.CharField(max_length=8)
    quantity = drf_serializers.IntegerField(min_value=1, max_value=100)


class OrderSerializer(drf_serializers.Serializer):
    reference = drf_serializers.UUIDField()
    placed = drf_serializers.DateField()
    customer = drf_serializers.CharField(min_length=2, max_length=20)
    note = drf_serializers.CharField(required=False, allow_blank=True, min_length=3)
    raw = drf_serializers.CharField(required=False, trim_whitespace=False)
    priority = drf_serializers.ChoiceField(choices=[1, 2, 3], default=2)
    channel = drf_serializers.ChoiceField(
        choices=["web", "shop"], allow_blank=True, required=False
    )
    gift = drf_serializers.BooleanField(default=False)
    discount = drf_serializers.FloatField(
        min_value=0, max_value=1, allow_null=True, required=False
    )
    coupon = drf_serializers.CharField(
        allow_null=True, required=False, source="promotion.code"
    )
    first_line = LineSerializer()
    lines = LineSerializer(many=True, allow_empty=False)


FIELDS = list(OrderSerializer().fields)

# -- Strategies -----------------------------------------------------------------

anything = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(10**30), max_value=10**30),
        st.floats(allow_nan=True, allow_infinity=True),
        # All of Unicode, surrogates included: JSON can carry them escaped.
        st.text(st.characters(), max_size=24),
        st.sampled_from(
            [
                "",
                " ",
                "  x  ",
                "x\n",
                "\x1fx",
                "a\x00",
                "\ud800",
                "1",
                "web",
                "2026-01-05",
            ]
        ),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(st.text(max_size=4), children, max_size=3),
    ),
    max_leaves=6,
)
trimmed = st.text(
    st.characters(exclude_categories=["Cs", "Cc"]), min_size=2, max_size=8
).filter(lambda value: value == value.strip())
line = st.fixed_dictionaries({"sku": trimmed, "quantity": st.integers(1, 100)})
canonical_order = st.fixed_dictionaries(
    {
        "reference": st.uuids().map(str),
        "placed": st.dates().map(lambda date: date.isoformat()),
        "customer": trimmed,
        "first_line": line,
        "lines": st.lists(line, min_size=1, max_size=3),
    },
    optional={
        "note": st.one_of(st.just(""), trimmed.filter(lambda value: len(value) >= 3)),
        "raw": st.text(
            st.characters(exclude_categories=["Cs"], exclude_characters="\x00"),
            min_size=1,
            max_size=8,
        ),
        "priority": st.sampled_from([1, 2, 3]),
        "channel": st.sampled_from(["web", "shop", ""]),
        "gift": st.booleans(),
        "discount": st.one_of(st.none(), st.floats(0, 1), st.sampled_from([0, 1])),
        "coupon": st.one_of(st.none(), trimmed),
        "ignored": anything,
    },
)


@st.composite
def perturbed_orders(draw):
    """A canonical order with some keys replaced by arbitrary JSON or removed."""
    order = dict(draw(canonical_order))
    for key in draw(st.lists(st.sampled_from(FIELDS), max_size=3, unique=True)):
        if draw(st.booleans()):
            order.pop(key, None)
        else:
            order[key] = draw(anything)
    return order


def exact(value):
    """``value`` with its types, so that 1, 1.0 and True differ."""
    if isinstance(value, dict):
        return ("dict", tuple((key, exact(item)) for key, item in value.items()))
    if isinstance(value, list):
        return ("list", tuple(exact(item) for item in value))
    return (type(value).__name__, repr(value))


def assert_recognizer_agrees_with_drf(serializer_class, data, **kwargs):
    recognized = inputs.recognize(serializer_class(data=data, **kwargs))
    reference = serializer_class(data=data, **kwargs)
    valid = reference.is_valid()
    if recognized is not inputs.NOT_RECOGNIZED:
        assert valid, (data, reference.errors)
        assert exact(recognized) == exact(reference.validated_data)
    return recognized, reference


# -- Properties -------------------------------------------------------------------


def test_the_serializer_under_test_is_eligible():
    assert inputs.report_input(OrderSerializer(data={})) is None


@PROFILE
@given(perturbed_orders())
@example({"discount": float("nan")})
@example({"discount": float("inf")})
@example({"quantity": 10**400})
@example({"customer": "ab\n"})
@example({"customer": "\x1fab"})
@example({"customer": "a\x00b"})
@example({"customer": "a\ud800b"})
@example({"customer": "a\udfffb"})
@example({"customer": "a\ud7ff\ue000b"})
@example({"customer": 12})
@example({"gift": 1})
@example({"priority": True})
@example({"priority": "1"})
@example({"placed": "2026-1-5"})
@example({"placed": "2026-01-05T00:00:00"})
@example({"reference": 5})
@example({"reference": "{12345678-1234-5678-1234-567812345678}"})
@example({"lines": []})
@example({"note": " "})
def test_what_is_recognized_is_what_drf_returns(order):
    base = {
        "reference": "12345678-1234-5678-1234-567812345678",
        "placed": "2026-01-05",
        "customer": "Ursula",
        "first_line": {"sku": "A1", "quantity": 1},
        "lines": [{"sku": "A1", "quantity": 1}],
    }
    assert_recognizer_agrees_with_drf(OrderSerializer, {**base, **order})


@PROFILE
@given(anything)
def test_arbitrary_json_never_raises(data):
    assert_recognizer_agrees_with_drf(OrderSerializer, data)
    assert_recognizer_agrees_with_drf(OrderSerializer, data, many=True)


@PROFILE
@given(canonical_order)
def test_canonical_input_is_recognized(order):
    # Without this the recognizer could "pass" by never accepting anything.
    recognized, reference = assert_recognizer_agrees_with_drf(OrderSerializer, order)
    assert reference.errors == {}
    assert recognized is not inputs.NOT_RECOGNIZED


@PROFILE
@given(st.lists(canonical_order, max_size=3))
def test_lists_of_canonical_input_are_recognized(orders):
    recognized, _ = assert_recognizer_agrees_with_drf(
        OrderSerializer, orders, many=True
    )
    assert recognized is not inputs.NOT_RECOGNIZED


@PROFILE
@given(perturbed_orders())
def test_partial_updates(order):
    recognized, _ = assert_recognizer_agrees_with_drf(
        OrderSerializer, order, partial=True
    )
    if recognized is not inputs.NOT_RECOGNIZED:
        # Defaults do not apply to partial updates.
        assert set(recognized) <= set(order) | {"promotion"}


@PROFILE
@given(perturbed_orders())
def test_the_backend_is_transparent(order):
    # Through the public API the result is DRF's whatever the recognizer did.
    reference = OrderSerializer(data=order)
    valid = reference.is_valid()
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": "msgspec"}):
        serializer = OrderSerializer(data=order)
        assert _is_valid(serializer) is valid
    assert exact(serializer.validated_data) == exact(reference.validated_data)
    assert serializer.errors == reference.errors


# -- Eligibility -------------------------------------------------------------------


class Scalar(drf_serializers.BaseSerializer):
    def to_internal_value(self, data):
        return int(data)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_a_serializer_without_fields_is_validated_by_drf(backend):
    assert inputs.recognize(Scalar(data="3"), backend=backend) is inputs.NOT_RECOGNIZED
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        serializer = Scalar(data="3")
        assert _is_valid(serializer)
    assert serializer.validated_data == 3


class Validated(drf_serializers.Serializer):
    name = drf_serializers.CharField()

    def validate_name(self, value):
        return value.upper()


class CrossValidated(drf_serializers.Serializer):
    name = drf_serializers.CharField()

    def validate(self, attrs):
        return attrs


class Slugged(drf_serializers.Serializer):
    name = drf_serializers.SlugField()


class Stamped(drf_serializers.Serializer):
    name = drf_serializers.CharField(default=str)


class Related(drf_serializers.Serializer):
    author = drf_serializers.PrimaryKeyRelatedField(queryset=Author.objects.all())


class AuthorModelSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


@pytest.mark.parametrize(
    ("serializer_class", "reason"),
    [
        (Validated, "validate_name() hook"),
        (CrossValidated, "overrides a validation method"),
        (Slugged, "is a SlugField"),
        (Stamped, "default that is not a constant"),
        (Related, "is a PrimaryKeyRelatedField"),
    ],
)
def test_serializers_the_fields_do_not_fully_describe_stay_on_drf(
    serializer_class, reason
):
    assert reason in inputs.report_input(serializer_class(data={}))
    assert (
        inputs.recognize(serializer_class(data={"name": "x"})) is inputs.NOT_RECOGNIZED
    )


class AsyncValidated(drf_serializers.Serializer):
    name = drf_serializers.CharField()

    async def avalidate(self, attrs):
        return attrs


def test_async_validation_hooks_stay_on_drf():
    report = inputs.report_input_details(AsyncValidated(data={}))
    assert report.code == "async_validation"
    assert inputs.recognize(AsyncValidated(data={"name": "x"})) is inputs.NOT_RECOGNIZED


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize("many", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_unsupported_fields_decline_before_building_a_cache_key(
    monkeypatch, backend, many, nested
):
    class Payload(drf_serializers.Serializer):
        value = Slugged() if nested else drf_serializers.SlugField()

    value = {"name": "valid-slug"} if nested else "valid-slug"
    data = [{"value": value}] if many else {"value": value}
    serializer = Payload(data=data, many=many)

    def unexpected_signature(serializer):
        raise AssertionError("Unsupported input does not need a cache key.")

    monkeypatch.setattr(inputs, "_signature", unexpected_signature)
    assert inputs.recognize(serializer, backend=backend) is inputs.NOT_RECOGNIZED
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == data


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_unsupported_fields_do_not_decline_other_instances_of_the_class(backend):
    class Payload(drf_serializers.Serializer):
        name = drf_serializers.SlugField()

    assert (
        inputs.recognize(Payload(data={"name": "value"}), backend=backend)
        is inputs.NOT_RECOGNIZED
    )
    changed = Payload(data={"name": "value"})
    changed.fields["name"] = drf_serializers.CharField()
    assert inputs.recognize(changed, backend=backend) == {"name": "value"}
    assert (
        inputs.recognize(Payload(data={"name": "value"}), backend=backend)
        is inputs.NOT_RECOGNIZED
    )


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_unsupported_read_only_fields_do_not_prevent_input_recognition(backend):
    class Payload(drf_serializers.Serializer):
        slug = drf_serializers.SlugField(read_only=True)
        name = drf_serializers.CharField()

    serializer = Payload(data={"name": "value"})
    assert inputs.recognize(serializer, backend=backend) == {"name": "value"}


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize("many", [False, True])
def test_relation_fallback_keeps_drf_errors_without_a_compiler_cache_key(
    monkeypatch, backend, many
):
    class Payload(drf_serializers.Serializer):
        authors = drf_serializers.PrimaryKeyRelatedField(
            queryset=Author.objects.none(), many=many
        )

    data = {"authors": [1] if many else 1}
    reference = Payload(data=data)
    assert not reference.is_valid()

    def unexpected_signature(serializer):
        raise AssertionError("Relations do not need an input compiler cache key.")

    monkeypatch.setattr(inputs, "_signature", unexpected_signature)
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        serializer = Payload(data=data)
        assert not _is_valid(serializer)
    assert serializer.errors == reference.errors
    assert serializer.validated_data == reference.validated_data


class AtLeastOne(drf_serializers.ListSerializer):
    def validate(self, attrs):
        return attrs


class Batched(drf_serializers.Serializer):
    name = drf_serializers.CharField()

    class Meta:
        list_serializer_class = AtLeastOne


def test_report_input_explains_list_serializers():
    assert inputs.report_input(OrderSerializer(data=[], many=True)) is None
    assert "list" in inputs.report_input(Batched(data=[], many=True))
    assert inputs.report_input(Validated(data=[], many=True)) == inputs.report_input(
        Validated(data={})
    )


def test_model_serializers_without_constraints_are_eligible():
    assert inputs.report_input(AuthorModelSerializer(data={})) is None
    assert inputs.recognize(AuthorModelSerializer(data={"name": "Ursula"})) == {
        "name": "Ursula"
    }


def test_form_input_is_left_to_drf():
    assert (
        inputs.recognize(AuthorModelSerializer(data=QueryDict("name=Ursula")))
        is inputs.NOT_RECOGNIZED
    )


def test_selected_backends_recognize_input():
    calls = []
    original = inputs.recognize

    def spy(serializer, **kwargs):
        calls.append(serializer)
        return original(serializer, **kwargs)

    inputs.recognize = spy
    try:
        for backend, expected in (("drf", 0), ("pydantic", 1), ("msgspec", 1)):
            calls.clear()
            with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
                assert _is_valid(AuthorModelSerializer(data={"name": "Ursula"}))
            assert len(calls) == expected, backend
    finally:
        inputs.recognize = original


# -- Pinned counterexamples ----------------------------------------------------


class BlankWithMinimum(drf_serializers.Serializer):
    name = drf_serializers.CharField(allow_blank=True, min_length=3)


class TwoMinimums(drf_serializers.Serializer):
    value = drf_serializers.IntegerField(
        validators=[MinValueValidator(10), MinValueValidator(1)]
    )


class TwoLengthBounds(drf_serializers.Serializer):
    name = drf_serializers.CharField(
        validators=[
            MinLengthValidator(2),
            MinLengthValidator(4),
            MaxLengthValidator(9),
            MaxLengthValidator(6),
        ]
    )


@pytest.mark.parametrize(
    ("serializer_class", "data"),
    [
        (BlankWithMinimum, {"name": ""}),
        (BlankWithMinimum, {"name": "a"}),
        (BlankWithMinimum, {"name": "abc"}),
        (TwoMinimums, {"value": 5}),
        (TwoMinimums, {"value": 10}),
        (TwoLengthBounds, {"name": "abc"}),
        (TwoLengthBounds, {"name": "abcd"}),
        (TwoLengthBounds, {"name": "abcdefg"}),
    ],
)
def test_blank_exception_and_repeated_bounds_follow_drf(serializer_class, data):
    recognized, reference = assert_recognizer_agrees_with_drf(serializer_class, data)
    # And the valid ones are recognized, so the assertion above compared something.
    assert (recognized is not inputs.NOT_RECOGNIZED) == reference.is_valid()


class CallableLimit(drf_serializers.Serializer):
    value = drf_serializers.IntegerField(validators=[MinValueValidator(lambda: 3)])


def test_a_callable_limit_is_left_to_drf():
    assert "MinValueValidator" in inputs.report_input(CallableLimit(data={}))


class Defaulted(drf_serializers.Serializer):
    value = drf_serializers.IntegerField(required=False)

    def __init__(self, *args, default, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["value"].default = default


@pytest.mark.parametrize("order", [(True, 1, 1.0), (1, 1.0, True), (1.0, True, 1)])
def test_defaults_of_different_types_are_different_recognizers(order):
    inputs.clear_recognizers(setting="REST_FRAMEWORK")
    for default in order:
        recognized, _ = assert_recognizer_agrees_with_drf(
            Defaulted, {}, default=default
        )
        assert recognized is not inputs.NOT_RECOGNIZED
        assert type(recognized["value"]) is type(default)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@PROFILE
@given(
    minimum=st.integers(0, 8),
    second_minimum=st.integers(0, 8),
    allow_blank=st.booleans(),
    data=st.dictionaries(
        st.sampled_from(["name", "number", "choice"]), anything, max_size=3
    ),
)
def test_generated_definitions_preserve_input_parity(
    backend, minimum, second_minimum, allow_blank, data
):
    class Example(drf_serializers.Serializer):
        name = drf_serializers.CharField(
            min_length=minimum,
            allow_blank=allow_blank,
            required=False,
            validators=[MinLengthValidator(second_minimum)],
        )
        number = drf_serializers.IntegerField(
            min_value=minimum,
            required=False,
            validators=[MinValueValidator(second_minimum)],
        )
        choice = drf_serializers.ChoiceField(choices=[1, 2, "web"], required=False)

    recognized = inputs.recognize(Example(data=data), backend=backend)
    reference = Example(data=data)
    if recognized is not inputs.NOT_RECOGNIZED:
        assert reference.is_valid(), reference.errors
        assert exact(recognized) == exact(reference.validated_data)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize("many", [False, True])
def test_backends_recognize_nested_input_defaults_and_partial(backend, many):
    class Child(drf_serializers.Serializer):
        number = drf_serializers.IntegerField(default=True)
        name = drf_serializers.CharField(min_length=2)

    class Parent(drf_serializers.Serializer):
        child = Child()
        children = Child(many=True, allow_empty=False)

    value = {"child": {"name": "ok"}, "children": [{"name": "ok", "number": 3}]}
    data = [value] if many else value
    for partial in (False, True):
        reference = Parent(data=data, many=many, partial=partial)
        assert reference.is_valid()
        result = inputs.recognize(
            Parent(data=data, many=many, partial=partial), backend=backend
        )
        assert result is not inputs.NOT_RECOGNIZED
        assert exact(result) == exact(reference.validated_data)


def test_pydantic_input_field_names_do_not_collide_with_model_methods():
    example = type(
        "Example",
        (drf_serializers.Serializer,),
        {
            "model_dump": drf_serializers.IntegerField(),
            "_private": drf_serializers.CharField(),
        },
    )
    data = {"model_dump": 1, "_private": "value"}
    assert inputs.recognize(example(data=data), backend="pydantic") == data


@pytest.mark.parametrize("value", [True, False, "1", 1.0])
def test_pydantic_noncanonical_choices_decline(value):
    class Example(drf_serializers.Serializer):
        choice = drf_serializers.ChoiceField(choices=[1, 2])

    assert (
        inputs.recognize(Example(data={"choice": value}), backend="pydantic")
        is inputs.NOT_RECOGNIZED
    )


def test_pydantic_input_respects_per_serializer_opt_out():
    from unittest.mock import patch

    class Example(drf_serializers.Serializer):
        value = drf_serializers.IntegerField()

        class Meta:
            serializer_backend = "drf"

    with (
        override_settings(FASTDRF={"SERIALIZER_BACKEND": "pydantic"}),
        patch.object(inputs, "recognize") as recognize,
    ):
        serializer = Example(data={"value": "12"})
        assert _is_valid(serializer)
        assert serializer.validated_data == {"value": 12}
        recognize.assert_not_called()


def test_pydantic_nested_model_instances_cannot_bypass_drf_input_rules():
    class Parent(drf_serializers.Serializer):
        child = LineSerializer()

    parent = inputs._recognizer_for(Parent(data={}), "pydantic")
    converted = parent.convert({"child": {"sku": "ab", "quantity": 1}}, strict=True)
    data = {"child": converted.field_0}
    assert (
        inputs.recognize(Parent(data=data), backend="pydantic") is inputs.NOT_RECOGNIZED
    )
    assert not Parent(data=data).is_valid()


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_recursive_unused_input_declines_without_changing_drf_validation(backend):
    data = {"sku": "ab", "quantity": 1}
    data["unused"] = data
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        serializer = LineSerializer(data=data)
        assert _is_valid(serializer) is True
        assert serializer.validated_data == {"sku": "ab", "quantity": 1}


# -- Methods assigned to instances, and the variant budget ----------------------------


class Value(drf_serializers.Serializer):
    value = drf_serializers.IntegerField()


class Nested(drf_serializers.Serializer):
    item = Value()


def _rejecting(serializer, field):
    def reject(value):
        raise drf_serializers.ValidationError("rejected by the configured field")

    field.run_validation = reject
    return serializer


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_a_method_assigned_to_a_field_keeps_drfs_validation(backend):
    def plain():
        return Value(data={"value": 1})

    def rejecting():
        serializer = plain()
        return _rejecting(serializer, serializer.fields["value"])

    def nested_rejecting():
        serializer = Nested(data={"item": {"value": 1}})
        return _rejecting(serializer, serializer.fields["item"].fields["value"])

    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        # Recognized, then an instance with its own hook, then recognized again.
        assert _is_valid(plain()) is True
        for make in (rejecting, nested_rejecting):
            serializer = make()
            assert _is_valid(serializer) is False
            assert serializer.is_valid() is False
        assert inputs.recognize(rejecting(), backend=backend) is inputs.NOT_RECOGNIZED
        assert _is_valid(plain()) is True


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize("hook", ["run_validation", "to_internal_value"])
def test_a_method_assigned_to_a_list_keeps_drfs_validation(backend, hook):
    def reject(data):
        raise drf_serializers.ValidationError("rejected by the list")

    serializer = Value(data=[{"value": 1}], many=True)
    setattr(serializer, hook, reject)
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert _is_valid(Value(data=[{"value": 1}], many=True)) is True
        assert inputs.recognize(serializer, backend=backend) is inputs.NOT_RECOGNIZED
        assert _is_valid(serializer) is False


class Moment(drf_serializers.Serializer):
    at = drf_serializers.TimeField()


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_the_fold_of_a_time_object_is_kept(backend):
    data = {"at": datetime.time(1, 30, fold=1)}
    reference = Moment(data=data)
    assert reference.is_valid()
    assert reference.validated_data["at"].fold == 1
    recognized = inputs.recognize(Moment(data=data), backend=backend)
    assert recognized == reference.validated_data
    assert recognized["at"].fold == 1
    assert type(recognized["at"]) is datetime.time


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize(
    ("field", "text", "value"),
    [
        ("placed", "2026-01-05", datetime.date(2026, 1, 5)),
        ("at", "01:30:15.250000", datetime.time(1, 30, 15, 250000)),
        (
            "reference",
            "12345678-1234-5678-1234-567812345678",
            uuid.UUID("12345678-1234-5678-1234-567812345678"),
        ),
    ],
)
def test_iso_strings_of_dates_times_and_uuids_are_recognized(
    backend, field, text, value
):
    # Both backends parse canonical ISO and UUID text, as DRF's parsers and
    # ``uuid.UUID`` do; pydantic's strict mode alone would decline it.
    class Dated(drf_serializers.Serializer):
        placed = drf_serializers.DateField(required=False)
        at = drf_serializers.TimeField(required=False)
        reference = drf_serializers.UUIDField(required=False)

    recognized = inputs.recognize(Dated(data={field: text}), backend=backend)
    reference = Dated(data={field: text})
    assert reference.is_valid(), reference.errors
    assert recognized == reference.validated_data == {field: value}
    assert exact(recognized) == exact(reference.validated_data)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize("text", ["01:30+02:00", "01:30:15.1234567", "1:30", "25:00"])
def test_times_drf_reads_otherwise_are_left_to_it_or_agree(backend, text):
    recognized = inputs.recognize(Moment(data={"at": text}), backend=backend)
    reference = Moment(data={"at": text})
    valid = reference.is_valid()
    if recognized is not inputs.NOT_RECOGNIZED:
        assert valid, reference.errors
        assert exact(recognized) == exact(reference.validated_data)


def test_a_full_variant_cache_builds_nothing_more(monkeypatch):
    from fastdrf.compiler import MAX_VARIANTS

    class Fields(drf_serializers.Serializer):
        a = drf_serializers.IntegerField()
        b = drf_serializers.IntegerField()
        c = drf_serializers.IntegerField()
        d = drf_serializers.IntegerField()
        e = drf_serializers.IntegerField()
        f = drf_serializers.IntegerField()
        g = drf_serializers.IntegerField()

        def __init__(self, *args, keep, **kwargs):
            super().__init__(*args, **kwargs)
            for name in set(self.fields) - set(keep):
                self.fields.pop(name)

    shapes = [
        *itertools.combinations("abcdefg", 2),
        *itertools.combinations("abcdefg", 3),
    ]
    for keep in shapes[:MAX_VARIANTS]:
        inputs.recognize(Fields(data=dict.fromkeys(keep, 1), keep=keep))
    assert len(inputs._recognizers.get(Fields)) == MAX_VARIANTS

    def build(*args, **kwargs):
        raise AssertionError("a recognizer was built for a full cache")

    monkeypatch.setattr(inputs, "Recognizer", build)
    keep = shapes[MAX_VARIANTS]
    assert (
        inputs.recognize(Fields(data=dict.fromkeys(keep, 1), keep=keep))
        is inputs.NOT_RECOGNIZED
    )
    # A variant already there is still recognized.
    known = shapes[0]
    assert inputs.recognize(
        Fields(data=dict.fromkeys(known, 1), keep=known)
    ) == dict.fromkeys(known, 1)


def test_serializer_classes_are_bounded(monkeypatch):
    from fastdrf import compiler

    monkeypatch.setattr(compiler, "MAX_SERIALIZER_CLASSES", 4)
    inputs.clear_recognizers(setting="REST_FRAMEWORK")
    classes = [
        type(f"Bounded{index}", (Value,), {"__module__": __name__})
        for index in range(10)
    ]
    for cls in classes:
        assert inputs.recognize(cls(data={"value": 1})) == {"value": 1}
    kept = [cls for cls in classes if inputs._recognizers.get(cls) is not None]
    assert 1 <= len(kept) <= 4
    assert classes[-1] in kept


# -- Serializers whose fields are a function of their class ---------------------------


def _static_classes():
    class Item(drf_serializers.Serializer):
        value = drf_serializers.IntegerField(max_value=10)
        note = drf_serializers.CharField(required=False)

    class Order(drf_serializers.Serializer):
        item = Item()
        items = Item(many=True)

    return Item, Order


def _agrees_with_drf(serializer, reference, backend):
    recognized = inputs.recognize(serializer, backend=backend)
    valid = reference.is_valid()
    if recognized is not inputs.NOT_RECOGNIZED:
        assert valid, reference.errors
        assert recognized == reference.validated_data
    return recognized


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize("many", [False, True])
def test_a_static_serializer_is_recognized_without_building_its_fields(backend, many):
    Item, Order = _static_classes()
    item = {"value": 1, "note": "a"}
    for cls, data in ((Item, item), (Order, {"item": item, "items": [item]})):
        payload = [data] if many else data
        first = cls(data=payload, many=many)
        assert inputs.recognize(first, backend=backend) == payload
        later = cls(data=payload, many=many)
        assert inputs.recognize(later, backend=backend) == payload
        target = later.child if many else later
        assert "fields" not in vars(target)


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_a_static_answer_is_not_reused_for_an_instance_that_changed(backend):
    Item, _ = _static_classes()
    assert inputs.recognize(Item(data={"value": 1}), backend=backend) == {"value": 1}

    def replaced(data):
        serializer = Item(data=data)
        serializer.fields["value"] = drf_serializers.CharField()
        return serializer

    recognized = _agrees_with_drf(
        replaced({"value": "x"}), replaced({"value": "x"}), backend
    )
    assert recognized in (inputs.NOT_RECOGNIZED, {"value": "x"})

    def field_validator(data):
        serializer = Item(data=data)
        serializer.fields["value"].validators.append(_reject)
        return serializer

    assert (
        _agrees_with_drf(
            field_validator({"value": 1}), field_validator({"value": 1}), backend
        )
        is inputs.NOT_RECOGNIZED
    )

    def serializer_validator(data):
        serializer = Item(data=data)
        serializer.validators = [_reject]
        return serializer

    assert (
        _agrees_with_drf(
            serializer_validator({"value": 1}),
            serializer_validator({"value": 1}),
            backend,
        )
        is inputs.NOT_RECOGNIZED
    )

    def instance_hook(data):
        serializer = Item(data=data)
        serializer.validate_value = _reject
        return serializer

    assert (
        _agrees_with_drf(
            instance_hook({"value": 1}), instance_hook({"value": 1}), backend
        )
        is inputs.NOT_RECOGNIZED
    )

    def contextual(data):
        return Item(data=data, read_only=False, context={})

    _agrees_with_drf(contextual({"value": 11}), contextual({"value": 11}), backend)
    # The class's answer is intact.
    assert inputs.recognize(Item(data={"value": 1}), backend=backend) == {"value": 1}


def _reject(*args):
    raise drf_serializers.ValidationError("rejected")


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_a_static_answer_keeps_partial_defaults_and_errors_apart(backend):
    Item, Order = _static_classes()
    cases = [
        (Item, {}, False),
        (Item, {}, True),
        (Item, {"value": 11}, False),
        (Item, {"value": "2"}, True),
        (Order, {"item": {}}, True),
        (Order, {"item": {"value": 1}, "items": []}, False),
        (Order, {"items": [{"value": 20}]}, True),
    ]
    for _ in range(2):  # the second round reads the kept answers
        for cls, data, partial in cases:
            _agrees_with_drf(
                cls(data=data, partial=partial),
                cls(data=data, partial=partial),
                backend,
            )


def test_concurrent_static_lookups_publish_one_recognizer():
    import threading

    Item, _ = _static_classes()
    barrier = threading.Barrier(8)
    found = []

    def look():
        serializer = Item(data={"value": 1})
        barrier.wait()
        found.append(inputs._recognizer_for(serializer, "msgspec"))

    threads = [threading.Thread(target=look) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(found) == 8
    assert found[0] is not None
    assert all(recognizer is found[0] for recognizer in found)
    assert inputs._recognizer_for(Item(data={"value": 1}), "msgspec") is found[0]


def test_a_settings_change_forgets_static_answers():
    Item, _ = _static_classes()
    before = inputs._recognizer_for(Item(data={}), "msgspec")
    # ``analyze_input`` reads DRF's input formats.
    with override_settings(REST_FRAMEWORK={"DATE_INPUT_FORMATS": ["%d.%m.%Y"]}):
        assert inputs._recognizers.get(Item) is None
    assert inputs._recognizer_for(Item(data={}), "msgspec") is not before


def test_unsupported_constants_are_not_kept_in_signatures():
    # A callable limit may close over a request; the signature keeps a
    # marker for it, not the callable.
    def limit():
        return 3

    serializer = CallableLimit(data={"value": 4})
    serializer.fields["value"].validators = [MinValueValidator(limit)]
    signature = inputs._signature(serializer)
    assert all(
        item is not limit
        for field in signature[-1]
        for validator in field[6]
        for item in validator
    )


@pytest.mark.skipif(
    not hasattr(drf_serializers, "BigIntegerField"),
    reason="DRF 3.17 added BigIntegerField",
)
@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
@pytest.mark.parametrize("value", [2**40, -1, 0, "12", 1.5, True, None, 2**70])
def test_big_integers_are_recognized_as_integers(backend, value):
    # What ModelSerializer builds for BigAutoField and BigIntegerField; its
    # input is IntegerField's.
    class Counter(drf_serializers.Serializer):
        total = drf_serializers.BigIntegerField(max_value=2**63 - 1)

    assert inputs.report_input(Counter(data={}), backend=backend) is None
    recognized = inputs.recognize(Counter(data={"total": value}), backend=backend)
    reference = Counter(data={"total": value})
    if recognized is not inputs.NOT_RECOGNIZED:
        assert reference.is_valid(), reference.errors
        assert exact(recognized) == exact(reference.validated_data)
    if value == 2**40:
        assert recognized == {"total": 2**40}


class StampedReadOnly(drf_serializers.Serializer):
    # DRF calls a read-only field's default for the serializer's validators,
    # at every validation, whatever it returns or raises.
    name = drf_serializers.CharField()
    stamp = drf_serializers.CharField(read_only=True, default=drf.CurrentUserDefault())


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_a_callable_read_only_default_is_left_to_drf(backend):
    assert "stamp has a default that is not a constant" in inputs.report_input(
        StampedReadOnly(data={})
    )
    assert (
        inputs.report_input_details(StampedReadOnly(data={})).code == "dynamic_default"
    )
    serializer = StampedReadOnly(data={"name": "x"})
    assert inputs.recognize(serializer, backend=backend) is inputs.NOT_RECOGNIZED
    # Without a request in the context, DRF's default raises.
    with pytest.raises(KeyError):
        StampedReadOnly(data={"name": "x"}).is_valid()


class ConstantReadOnly(drf_serializers.Serializer):
    name = drf_serializers.CharField()
    kind = drf_serializers.CharField(read_only=True, default="book")


@pytest.mark.parametrize("backend", ["msgspec", "pydantic"])
def test_a_constant_read_only_default_is_recognized(backend):
    serializer = ConstantReadOnly(data={"name": "x"})
    assert inputs.recognize(serializer, backend=backend) == {"name": "x"}
