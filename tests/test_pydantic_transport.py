"""Pydantic JSON transport: native differences and DRF integration boundaries."""

import datetime
import decimal
import gc
import io
import json
import math
import subprocess
import sys
import types
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.utils.functional import lazy
from pydantic_core import PydanticSerializationError
from rest_framework.exceptions import ErrorDetail, ParseError
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from fastdrf.pydantic.parsers import PydanticJSONParser
from fastdrf.pydantic.renderers import PydanticJSONRenderer
from fastdrf.response import DataResponse, Response
from fastdrf.views import DataResponseMixin
from tests.models import Author


@pytest.mark.parametrize(
    "value",
    [None, True, 12, 1.5, "hello", "café", [], {}, {"rows": [1, None, {"a": "b"}]}],
)
def test_parser_reads_json_values_without_schema_validation(value):
    body = json.dumps(value).encode()
    assert PydanticJSONParser().parse(io.BytesIO(body)) == value


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{",
        b"{}x",
        b"[1,",
        b'"\\ud800"',
        b'"\xff"',
        b"NaN",
        b"Infinity",
        b"-Infinity",
    ],
)
def test_invalid_json_is_a_parse_error(body):
    with pytest.raises(ParseError, match="JSON parse error") as error:
        PydanticJSONParser().parse(io.BytesIO(body))
    assert error.value.status_code == 400


def test_parser_nonfinite_and_duplicate_key_contract():
    parser = PydanticJSONParser()
    assert math.isinf(parser.parse(io.BytesIO(b"1e400")))
    assert parser.parse(io.BytesIO(b'{"a":1,"a":2}')) == {"a": 2}
    parser.strict = False
    assert math.isnan(parser.parse(io.BytesIO(b"NaN")))
    assert math.isinf(parser.parse(io.BytesIO(b"Infinity")))


@pytest.mark.parametrize("encoding", ["utf-8", "UTF8", "utf-16", "iso-8859-1"])
def test_parser_honors_the_request_charset(encoding):
    body = '{"name":"café"}'.encode(encoding)
    assert PydanticJSONParser().parse(
        io.BytesIO(body), parser_context={"encoding": encoding}
    ) == {"name": "café"}


def test_parser_text_stream_and_missing_stream():
    parser = PydanticJSONParser()
    assert parser.parse(io.StringIO('{"a":1}')) == {"a": 1}
    assert parser.parse(None) is None
    with pytest.raises(ParseError):
        parser.parse(io.BytesIO(b"\xff"), parser_context={"encoding": "ascii"})


@pytest.mark.parametrize("mode", [False, True, "keys", "all", "none"])
def test_parser_string_cache_policy_keeps_values(mode):
    parser = PydanticJSONParser()
    parser.cache_strings = mode
    assert parser.parse(io.BytesIO(b'{"name":"Ada","items":["Ada"]}')) == {
        "name": "Ada",
        "items": ["Ada"],
    }


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        [],
        {"detail": ErrorDetail("bad", code="invalid")},
        {"text": "café\u2028\u2029"},
        {"value": lazy(lambda: "translated", str)()},
        types.MappingProxyType({"a": 1}),
    ],
)
def test_common_output_matches_drf(data):
    assert PydanticJSONRenderer().render(data) == JSONRenderer().render(data)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (decimal.Decimal("1.50"), b'"1.50"'),
        (decimal.Decimal("NaN"), b'"NaN"'),
        (b"\xff", b'"_w=="'),
        (datetime.timedelta(hours=1), b'"PT1H"'),
        (float("nan"), b"null"),
        (float("inf"), b"null"),
        ({None: 1, True: 2}, b'{"None":1,"true":2}'),
        (datetime.time(1, tzinfo=datetime.UTC), b'"01:00:00Z"'),
    ],
)
def test_native_output_differences_are_explicit(data, expected):
    assert PydanticJSONRenderer().render(data) == expected


@pytest.mark.parametrize("indent", [0, 2, 4])
def test_indentation_uses_drf_before_encoding(indent):
    data = {"price": decimal.Decimal("1.50")}
    context = {"indent": indent}
    assert PydanticJSONRenderer().render(
        data, renderer_context=context
    ) == JSONRenderer().render(data, renderer_context=context)


def test_accept_indentation_and_custom_get_indent():
    data = {"a": [1, 2]}
    accept = "application/json; indent=2"
    assert PydanticJSONRenderer().render(data, accept) == JSONRenderer().render(
        data, accept
    )

    class Indented(PydanticJSONRenderer):
        def get_indent(self, accepted_media_type, renderer_context):
            return 2

    assert Indented().render(data) == JSONRenderer().render(
        data, renderer_context={"indent": 2}
    )


@pytest.mark.parametrize(
    ("option", "value"), [("ensure_ascii", True), ("compact", False)]
)
def test_drf_format_settings_take_the_drf_path(option, value):
    native, reference = PydanticJSONRenderer(), JSONRenderer()
    setattr(native, option, value)
    setattr(reference, option, value)
    data = {"text": "café", "price": decimal.Decimal("1.50")}
    assert native.render(data) == reference.render(data)


def test_custom_encoder_is_respected():
    class Encoder(JSONRenderer.encoder_class):
        def default(self, obj):
            if isinstance(obj, decimal.Decimal):
                return "custom"
            return super().default(obj)

    renderer = PydanticJSONRenderer()
    renderer.encoder_class = Encoder
    assert renderer.render(decimal.Decimal("1.50")) == b'"custom"'


@pytest.mark.parametrize("nested", [False, True])
def test_iterator_failure_is_not_retried(nested):
    visits = []

    def rows():
        visits.append(1)
        yield 1
        yield object()

    data = {"rows": rows()} if nested else rows()
    with pytest.raises(PydanticSerializationError):
        PydanticJSONRenderer().render(data)
    assert visits == [1]
    assert PydanticJSONRenderer().render({"ok": True}) == b'{"ok":true}'


def test_repeated_iterators_and_nested_rendering():
    class Nested:
        def tolist(self):
            assert PydanticJSONRenderer().render(iter([9])) == b"[9]"
            return [3]

    items = iter([1, 2])
    assert PydanticJSONRenderer().render([items, Nested(), items]) == b"[[1,2],[3],[]]"


def test_failed_render_does_not_retain_iterator():
    renderer = PydanticJSONRenderer()

    def fail():
        items = (item for item in [1, object()])
        ref = weakref.ref(items)
        with pytest.raises(PydanticSerializationError):
            renderer.render(items)
        return ref

    references = [fail() for _ in range(3)]
    gc.collect()
    assert all(ref() is None for ref in references)


def test_native_models_serialize_without_validation_replay():
    from pydantic import BaseModel, Field, model_validator

    calls = []

    class Output(BaseModel):
        value: int = Field(serialization_alias="wire")

        @model_validator(mode="after")
        def record(self):
            calls.append(1)
            return self

    value = Output(value=2)
    assert PydanticJSONRenderer().render(value) == b'{"wire":2}'
    assert calls == [1]


def test_shared_renderer_is_thread_safe():
    renderer = PydanticJSONRenderer()
    with ThreadPoolExecutor(max_workers=4) as pool:
        output = list(
            pool.map(lambda i: renderer.render({"items": iter([i])}), range(50))
        )
    assert [json.loads(value) for value in output] == [
        {"items": [i]} for i in range(50)
    ]


@pytest.mark.django_db
def test_queryset_output_uses_drfs_default_encoder():
    Author.objects.create(name="Ada")
    rows = Author.objects.values("name")
    assert PydanticJSONRenderer().render(rows) == b'[{"name":"Ada"}]'


@pytest.mark.parametrize("direct", [False, True])
def test_http_transport_with_both_response_types(direct):
    class Echo(DataResponseMixin, APIView):
        parser_classes = [PydanticJSONParser]
        renderer_classes = [PydanticJSONRenderer]

        def post(self, request):
            response = DataResponse if direct else Response
            return response(request.data, status=201, headers={"X-Test": "yes"})

    factory = APIRequestFactory()
    response = Echo.as_view()(factory.post("/echo/", {"items": [1, 2]}, format="json"))
    assert response.status_code == 201
    assert response["X-Test"] == "yes"
    if not direct:
        response.render()
    else:
        assert response.data is None
    assert response.content == b'{"items":[1,2]}'
    invalid = Echo.as_view()(
        factory.post("/echo/", b"{", content_type="application/json")
    )
    assert invalid.status_code == 400
    invalid.render()


def test_exports_are_lazy_and_names_are_available():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import fastdrf.pydantic; assert 'pydantic_core' not in sys.modules; assert 'pydantic' not in sys.modules",
        ],
        check=True,
    )
    from fastdrf.pydantic import (
        PydanticJSONParser as Parser,
    )
    from fastdrf.pydantic import (
        PydanticJSONRenderer as Renderer,
    )

    assert Parser is PydanticJSONParser
    assert Renderer is PydanticJSONRenderer
