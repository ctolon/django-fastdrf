"""The msgspec renderer answers with DRF's bytes or error where msgspec cannot."""

import datetime
import decimal
import uuid
from unittest import mock

import pytest
from rest_framework.exceptions import ErrorDetail
from rest_framework.renderers import JSONRenderer

from fastdrf.msgspec.renderers import MsgspecJSONRenderer, enc_hook


def test_renderer_matches_drf_json_for_common_data():
    data = {
        "detail": ErrorDetail("Nope", code="x"),
        "when": datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
        "id": uuid.UUID(int=1),
        "price": decimal.Decimal("1.5"),
        "text": "a b c",  # line breaks in JavaScript, not in JSON
        "items": [1, 2.5, None, True],
    }
    assert MsgspecJSONRenderer().render(data) == JSONRenderer().render(data)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime.timedelta(hours=1), b'"PT3600S"'),
        (b"ab", b'"YWI="'),
        (float("nan"), b"null"),
        (float("inf"), b"null"),
        ([decimal.Decimal("1.50"), decimal.Decimal(2)], b"[1.50,2]"),
        ([1e300, 1e-7], b"[1e300,1e-7]"),
        (datetime.time(1, tzinfo=datetime.UTC), b'"01:00:00Z"'),
    ],
    ids=repr,
)
def test_the_documented_differences_from_drfs_encoder(value, expected):
    assert MsgspecJSONRenderer().render(value) == expected


def test_an_unsupported_value_fails_as_in_drf():
    message = "Object of type object is not JSON serializable"
    for renderer in (JSONRenderer(), MsgspecJSONRenderer()):
        with pytest.raises(TypeError, match=message):
            renderer.render({"value": object()})
    with pytest.raises(TypeError, match=message):
        enc_hook(object())


@pytest.mark.parametrize(
    "data",
    [
        {True: 1, None: 2},  # json stringifies these keys; msgspec refuses them
        {"counts": {False: 3}},
        {"note": "NaN", "limit": "Infinity"},  # the words, in strings
        {"price": decimal.Decimal("1.5"), "none": "NaN"},
    ],
    ids=repr,
)
def test_the_renderer_outputs_drfs_bytes_where_msgspec_cannot(data):
    assert MsgspecJSONRenderer().render(data) == JSONRenderer().render(data)


@pytest.mark.parametrize(
    "value",
    [
        decimal.Decimal("NaN"),
        decimal.Decimal("Infinity"),
        decimal.Decimal("-Infinity"),
        decimal.Decimal("sNaN"),
    ],
    ids=str,
)
def test_a_non_finite_decimal_fails_as_in_drf(value):
    # msgspec writes the bare token, which no JSON parser reads.
    message = "Out of range float values|cannot convert signaling NaN"
    with pytest.raises(ValueError, match=message) as drf:
        JSONRenderer().render({"value": value})
    with pytest.raises(ValueError, match=message) as ours:
        MsgspecJSONRenderer().render({"value": value})
    assert str(ours.value) == str(drf.value)


@pytest.mark.parametrize("size", [1024, 8 * 1024 * 1024])
@pytest.mark.parametrize("accept", ["application/json", "application/json; indent=4"])
def test_large_plain_json_matches_drf_bytes_with_and_without_indentation(size, accept):
    data = {
        "text": "x" * size,
        "unicode": "ş🙂  ",
        "flag": True,
        "missing": None,
    }
    assert MsgspecJSONRenderer().render(data, accept) == JSONRenderer().render(
        data, accept
    )


@pytest.mark.parametrize(
    "data",
    [
        {"text": "ş🙂  ", "n": 1.5, "big": 10**30, "none": None},
        [1, "two", [3.0, True], {"nested": {"deep": []}}],
        None,
    ],
)
@pytest.mark.parametrize(
    ("accept", "context"),
    [
        ("application/json", {}),
        ("application/json", {"indent": 0}),
        ("application/json", {"indent": 2}),
        ("application/json; indent=4", {}),
        ("application/json; charset=utf-8", {}),
        (None, None),
    ],
)
def test_indentation_is_drfs(data, accept, context):
    assert MsgspecJSONRenderer().render(data, accept, context) == (
        JSONRenderer().render(data, accept, context)
    )


def test_get_indent_runs_only_when_something_can_ask_for_indentation():
    renderer = MsgspecJSONRenderer()
    # DRF's ``get_indent`` parses the media type.
    with mock.patch(
        "rest_framework.renderers.parse_header_parameters", side_effect=AssertionError
    ):
        assert renderer.render({"a": 1}, "application/json", {}) == b'{"a":1}'
        assert renderer.render({"a": 1}, None, None) == b'{"a":1}'
        with pytest.raises(AssertionError):
            renderer.render({"a": 1}, "application/json; indent=2", {})


def test_a_get_indent_of_the_projects_is_asked():
    class Indented(MsgspecJSONRenderer):
        def get_indent(self, accepted_media_type, renderer_context):
            return 2

    expected = JSONRenderer().render({"a": 1}, "application/json; indent=2")
    assert Indented().render({"a": 1}, "application/json", {}) == expected
    instance = MsgspecJSONRenderer()
    instance.get_indent = lambda accepted_media_type, renderer_context: 2
    assert instance.render({"a": 1}, "application/json", {}) == expected
