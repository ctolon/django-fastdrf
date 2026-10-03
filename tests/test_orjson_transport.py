"""orjson's native contract and its DRF HTTP integration."""

import datetime
import decimal
import io
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# No wheel for free-threaded CPython: that session runs without orjson.
orjson = pytest.importorskip("orjson")

from rest_framework.exceptions import ParseError  # noqa: E402

from fastdrf.orjson import ORJSONParser, ORJSONRenderer  # noqa: E402
from tests import test_pydantic_transport as shared  # noqa: E402


@pytest.fixture(autouse=True)
def transport(monkeypatch):
    monkeypatch.setattr(shared, "PydanticJSONParser", ORJSONParser)
    monkeypatch.setattr(shared, "PydanticJSONRenderer", ORJSONRenderer)
    monkeypatch.setattr(shared, "PydanticSerializationError", orjson.JSONEncodeError)


# Run the same transport contract against both implementations. Native-type
# differences are tested below, rather than implied by shared JSON fixtures.
test_parser_reads_json_values_without_schema_validation = (
    shared.test_parser_reads_json_values_without_schema_validation
)
test_invalid_json_is_a_parse_error = shared.test_invalid_json_is_a_parse_error
test_parser_honors_the_request_charset = shared.test_parser_honors_the_request_charset
test_parser_text_stream_and_missing_stream = (
    shared.test_parser_text_stream_and_missing_stream
)
test_common_output_matches_drf = shared.test_common_output_matches_drf
test_indentation_uses_drf_before_encoding = (
    shared.test_indentation_uses_drf_before_encoding
)
test_accept_indentation_and_custom_get_indent = (
    shared.test_accept_indentation_and_custom_get_indent
)
test_drf_format_settings_take_the_drf_path = (
    shared.test_drf_format_settings_take_the_drf_path
)
test_custom_encoder_is_respected = shared.test_custom_encoder_is_respected
test_iterator_failure_is_not_retried = shared.test_iterator_failure_is_not_retried
test_repeated_iterators_and_nested_rendering = (
    shared.test_repeated_iterators_and_nested_rendering
)
test_failed_render_does_not_retain_iterator = (
    shared.test_failed_render_does_not_retain_iterator
)
test_shared_renderer_is_thread_safe = shared.test_shared_renderer_is_thread_safe
test_queryset_output_uses_drfs_default_encoder = (
    shared.test_queryset_output_uses_drfs_default_encoder
)
test_http_transport_with_both_response_types = (
    shared.test_http_transport_with_both_response_types
)


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("body", [b"NaN", b"Infinity", b"-Infinity", b"1e400"])
def test_nonfinite_input_is_always_rejected(strict, body):
    parser = ORJSONParser()
    parser.strict = strict
    with pytest.raises(ParseError):
        parser.parse(io.BytesIO(body))


def test_large_integer_input_and_duplicate_keys():
    parser = ORJSONParser()
    assert parser.parse(io.BytesIO(b'{"a":1,"a":2}')) == {"a": 2}
    assert parser.parse(io.BytesIO(b"18446744073709551615")) == 2**64 - 1
    # Beyond the native integer range, orjson can return a float, losing precision.
    assert isinstance(parser.parse(io.BytesIO(b"18446744073709551616")), float)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (decimal.Decimal("1.50"), b"1.5"),
        (b"hello", b'"hello"'),
        (datetime.timedelta(hours=1), b'"3600.0"'),
        (float("nan"), b"null"),
        (float("inf"), b"null"),
        (
            datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC),
            b'"2020-01-01T00:00:00+00:00"',
        ),
        (2**64 - 1, b"18446744073709551615"),
    ],
)
def test_native_and_drf_default_representations(data, expected):
    assert ORJSONRenderer().render(data) == expected


@pytest.mark.parametrize(
    "data", [2**64, -(2**63) - 1, {1: "value"}, b"\xff", object(), "\ud800"]
)
def test_encoding_errors_are_not_silently_replaced(data):
    with pytest.raises(orjson.JSONEncodeError):
        ORJSONRenderer().render(data)


def test_dataclasses_are_native_but_schema_models_need_representation():
    from pydantic import BaseModel

    @dataclass
    class Row:
        value: int

    class Model(BaseModel):
        value: int

    assert ORJSONRenderer().render(Row(1)) == b'{"value":1}'
    # Use serializer.data / model_dump(mode="json"), not a raw BaseModel.
    assert json.loads(
        ORJSONRenderer().render(Model(value=1).model_dump(mode="json"))
    ) == {"value": 1}


def test_circular_data_is_rejected():
    data = []
    data.append(data)
    with pytest.raises(orjson.JSONEncodeError):
        ORJSONRenderer().render(data)


def test_lazy_exports():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import fastdrf.orjson; assert 'orjson' not in sys.modules",
        ],
        check=True,
    )
    import fastdrf.orjson as integration
    from fastdrf.orjson.parsers import ORJSONParser as Parser
    from fastdrf.orjson.renderers import ORJSONRenderer as Renderer

    assert integration.ORJSONParser is Parser
    assert integration.ORJSONRenderer is Renderer
    with pytest.raises(AttributeError):
        _ = integration.unknown


def test_documented_http_example():
    from rest_framework.test import APIRequestFactory

    guide = (Path(__file__).resolve().parents[1] / "docs" / "rendering.md").read_text()
    section = guide.split("## orjson JSON renderer and parser\n", 1)[1].split(
        "\n## ", 1
    )[0]
    namespace = {}
    for block in re.findall(r"^```python\n(.*?)^```", section, re.M | re.S):
        exec(compile(block, "docs/rendering.md:orjson", "exec"), namespace)
    response = namespace["EchoAPIView"].as_view()(
        APIRequestFactory().post("/echo/", {"items": [1]}, format="json")
    )
    response.render()
    assert response.status_code == 200
    assert response.content == b'{"items":[1]}'
