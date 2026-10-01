"""Opt-in scalar template copies preserve DRF validation and field isolation."""

import copy
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.utils import translation
from rest_framework import fields
from rest_framework.exceptions import ValidationError

from fastdrf._field_copy import plan_fields
from tests.test_field_cache_language import Cached, Reference


def copy_fields(template):
    return plan_fields(template)()


CASES = [
    (fields.BooleanField, {}),
    (fields.BooleanField, {"allow_null": True}),
    (fields.CharField, {}),
    (fields.CharField, {"min_length": 2, "max_length": 5}),
    (fields.CharField, {"allow_blank": True, "trim_whitespace": False}),
    (fields.IntegerField, {"min_value": 0, "max_value": 5}),
    (fields.FloatField, {"min_value": 0.0, "max_value": 5.0}),
    (fields.UUIDField, {"format": "hex"}),
    (fields.ReadOnlyField, {}),
    (fields.EmailField, {}),  # Unsupported exact class: normal DRF deepcopy.
    (fields.ListField, {"child": fields.CharField(max_length=5)}),
    (fields.DictField, {"child": fields.IntegerField()}),
]


def outcome(field, value):
    try:
        return field.run_validation(value)
    except ValidationError as exc:
        return exc.detail
    except fields.SkipField:
        return fields.SkipField


@pytest.mark.parametrize(("cls", "options"), CASES)
@pytest.mark.parametrize("language", ["en", "fr"])
def test_copy_matches_deepcopy(cls, options, language):
    template = cls(**copy.deepcopy(options))
    with translation.override(language):
        actual = copy_fields({"value": template})["value"]
        expected = copy.deepcopy(template)
        assert repr(actual) == repr(expected)
        for value in (None, "", "x", "123456", "3", 2, 1.5, True, [], {}, ["x"]):
            assert outcome(actual, value) == outcome(expected, value)
    assert actual is not template
    assert actual.style is not template.style
    assert actual.error_messages is not template.error_messages


def test_supported_scalar_does_not_repeat_constructor():
    template = fields.CharField(max_length=5)
    with patch.object(fields.CharField, "__init__", side_effect=AssertionError):
        assert copy_fields({"name": template})["name"].max_length == 5


def test_passed_validators_are_shared_but_generated_ones_are_not():
    def supplied(value):
        return None

    template = fields.CharField(max_length=5, validators=[supplied])
    first = copy_fields({"name": template})["name"]
    second = copy_fields({"name": template})["name"]
    assert first.validators is not second.validators
    assert first.validators[0] is second.validators[0] is supplied
    assert all(
        a is not b
        for a, b in zip(first.validators[1:], second.validators[1:], strict=True)
    )


def test_nested_mutable_arguments_are_independent():
    template = fields.CharField(style={"options": ["one"]}, default=["default"])
    first = copy_fields({"name": template})["name"]
    second = copy_fields({"name": template})["name"]
    first.style["options"].append("two")
    first.default.append("changed")
    assert second.style == template.style == {"options": ["one"]}
    assert second.default == template.default == ["default"]
    assert first.style is first._kwargs["style"]


def test_custom_constructor_and_cyclic_arguments_retain_deepcopy():
    calls = []

    class Custom(fields.CharField):
        def __init__(self, **kwargs):
            calls.append(True)
            super().__init__(**kwargs)

    template = Custom()
    copy_fields({"name": template})
    assert len(calls) == 2
    style = {}
    style["cycle"] = style
    template = fields.CharField(style=style)
    result = copy_fields({"name": template})["name"]
    assert result.style is result.style["cycle"]
    assert result.style is not style


def test_shared_template_fields_and_custom_validator_metadata():
    class Validator:
        @property
        def message(self):
            raise AssertionError("Application metadata must not be inspected")

        def __call__(self, value):
            return None

    validator = Validator()
    field = fields.CharField(validators=[validator])
    result = copy_fields({"one": field, "two": field})
    assert result["one"] is result["two"]
    assert result["one"].validators[0] is validator


@pytest.mark.parametrize("languages", [("en", "fr"), ("fr", "en")])
def test_cloned_fields_preserve_request_language(languages):
    with override_settings(
        FASTDRF={"CACHE_SERIALIZER_FIELDS": True, "FIELD_COPY_MODE": "clone"}
    ):
        for language in languages:
            with translation.override(language):
                actual, expected = (
                    Cached(data={"name": "x"}),
                    Reference(data={"name": "x"}),
                )
                assert not actual.is_valid()
                assert not expected.is_valid()
                assert actual.errors == expected.errors
