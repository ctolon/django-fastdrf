"""
``fastdrf.msgspec.http.JsonResponse`` against Django's ``JsonResponse``: the
same response for the same arguments, the same decoded body, and each
documented difference of the encoding asserted as it is.
"""

import datetime
import decimal
import json
import os
import subprocess
import sys
import textwrap
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

pytest.importorskip("msgspec")

from django import http  # noqa: E402
from django.utils.functional import lazy  # noqa: E402
from django.utils.safestring import mark_safe  # noqa: E402

from fastdrf.msgspec.http import JsonResponse  # noqa: E402
from fastdrf.registry import register_msgspec_type  # noqa: E402
from fastdrf.testing import isolated_registry  # noqa: E402

lazy_text = lazy(lambda: "çeviri", str)


class Money:
    """A project's type, which msgspec does not know."""

    def __init__(self, amount, currency):
        self.amount, self.currency = amount, currency


def register_money():
    register_msgspec_type(
        Money,
        encode=lambda value: f"{value.amount} {value.currency}",
        decode=lambda type_, value: type_(*value.split(" ")),
    )


def test_the_response_is_djangos_for_the_same_arguments():
    arguments = {
        "status": 201,
        "headers": {"X-Request-Id": "abc"},
        "reason": "Made",
    }
    ours = JsonResponse({"a": 1}, **arguments)
    django = http.JsonResponse({"a": 1}, **arguments)
    for response in (ours, django):
        response.set_cookie("seen", "1", max_age=10)
    assert isinstance(ours, http.HttpResponse)
    assert not isinstance(ours, http.JsonResponse)
    assert ours.status_code == django.status_code == 201
    assert ours.reason_phrase == django.reason_phrase == "Made"
    assert dict(ours.headers) == dict(django.headers)
    assert ours["Content-Type"] == "application/json"
    assert ours.charset == django.charset
    assert ours.cookies.output() == django.cookies.output()
    # Django's body has spaces after separators; msgspec writes none.
    assert ours.content == b'{"a":1}'
    assert django.content == b'{"a": 1}'


@pytest.mark.parametrize(
    "arguments",
    [
        {"content_type": "application/vnd.api+json"},
        {"content_type": "application/json; charset=utf-8"},
        {"charset": "latin-1"},
    ],
)
def test_content_type_and_charset_are_djangos(arguments):
    ours = JsonResponse({"a": 1}, **arguments)
    django = http.JsonResponse({"a": 1}, **arguments)
    assert ours["Content-Type"] == django["Content-Type"]
    assert ours.charset == django.charset


def test_the_body_is_utf8_whatever_the_charset():
    # Django's body is ASCII: \u escapes. Both decode to the same value.
    ours = JsonResponse({"a": "ü"}, charset="latin-1")
    django = http.JsonResponse({"a": "ü"}, charset="latin-1")
    assert ours.content == '{"a":"ü"}'.encode()
    assert django.content == b'{"a": "\\u00fc"}'
    assert json.loads(ours.content) == json.loads(django.content)


@pytest.mark.parametrize("data", [[1, 2], "text", 1, None, (1, 2)], ids=repr)
def test_safe_refuses_what_is_not_a_dict_with_djangos_message(data):
    for response_class in (JsonResponse, http.JsonResponse):
        with pytest.raises(TypeError) as error:
            response_class(data)
        assert str(error.value) == (
            "In order to allow non-dict objects to be serialized set the "
            "safe parameter to False."
        )


@pytest.mark.parametrize("data", [[1, 2], "text", 1, None, (1, 2)], ids=repr)
def test_safe_false_encodes_any_value(data):
    assert json.loads(JsonResponse(data, safe=False).content) == json.loads(
        http.JsonResponse(data, safe=False).content
    )


@pytest.mark.parametrize("name", ["encoder", "json_dumps_params"])
def test_the_parameters_of_the_json_module_are_not_accepted(name):
    with pytest.raises(TypeError, match=f"unexpected keyword argument '{name}'"):
        JsonResponse({}, **{name: None})


def test_a_positional_encoder_is_not_taken_for_safe():
    # Django's second positional parameter is ``encoder``: code moved from
    # Django's response must fail, not pass the encoder as ``safe``.
    from django.core.serializers.json import DjangoJSONEncoder

    with pytest.raises(TypeError, match="positional argument"):
        JsonResponse([1], DjangoJSONEncoder)


PARITY = {
    "str": "text",
    "int": 42,
    "negative": -7,
    "float": 1.5,
    "bool": True,
    "none": None,
    "nested": {"list": [1, {"inner": [True, None]}], "empty": {}},
    "turkish": "Çağrı ığdır şükür",
    "japanese": "日本語のテキスト",
    "emoji": "👍🏽 🇹🇷",
    "uuid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    "decimal": decimal.Decimal("1.50"),
    "naive_datetime": datetime.datetime(2024, 1, 2, 3, 4, 5),
    "utc_datetime": datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
    "date": datetime.date(2024, 1, 2),
    "time": datetime.time(3, 4, 5),
    "lazy": lazy_text(),
    "safe_string": mark_safe("<b>bold</b>"),
}


@pytest.mark.parametrize("name", PARITY)
def test_the_decoded_body_is_djangos(name):
    data = {"value": PARITY[name]}
    assert json.loads(JsonResponse(data).content) == json.loads(
        http.JsonResponse(data).content
    )


# The differences from DjangoJSONEncoder in docs/django-utilities.md, one test
# per row: a msgspec release that changes one fails here.


def test_datetimes_keep_their_microseconds():
    value = datetime.datetime(2024, 1, 2, 3, 4, 5, 123456)
    assert JsonResponse({"v": value}).content == b'{"v":"2024-01-02T03:04:05.123456"}'
    assert json.loads(http.JsonResponse({"v": value}).content) == {
        "v": "2024-01-02T03:04:05.123"
    }


def test_times_keep_their_microseconds():
    value = datetime.time(3, 4, 5, 123456)
    assert JsonResponse({"v": value}).content == b'{"v":"03:04:05.123456"}'
    assert json.loads(http.JsonResponse({"v": value}).content) == {"v": "03:04:05.123"}


def test_an_aware_time_is_encoded():
    value = datetime.time(3, 4, 5, tzinfo=datetime.UTC)
    assert JsonResponse({"v": value}).content == b'{"v":"03:04:05Z"}'
    with pytest.raises(ValueError, match="timezone-aware times"):
        http.JsonResponse({"v": value})


def test_a_timedelta_is_a_shorter_iso_8601_duration():
    value = datetime.timedelta(days=1, seconds=3, microseconds=5)
    assert JsonResponse({"v": value}).content == b'{"v":"P1DT3.000005S"}'
    assert json.loads(http.JsonResponse({"v": value}).content) == {
        "v": "P1DT00H00M03.000005S"
    }


def test_nan_is_null():
    assert JsonResponse({"v": float("nan")}).content == b'{"v":null}'
    assert http.JsonResponse({"v": float("nan")}).content == b'{"v": NaN}'


def test_bytes_are_base64():
    assert JsonResponse({"v": b"ab"}).content == b'{"v":"YWI="}'
    with pytest.raises(TypeError, match="Object of type bytes"):
        http.JsonResponse({"v": b"ab"})


def test_a_registered_type_is_encoded_by_the_registry():
    with isolated_registry():
        register_money()
        response = JsonResponse({"price": Money("1.50", "EUR")})
    assert response.content == b'{"price":"1.50 EUR"}'


def test_the_callers_hook_runs_before_the_registry():
    def enc_hook(value):
        return "caller's"

    with isolated_registry():
        register_money()
        response = JsonResponse({"price": Money("1", "EUR")}, enc_hook=enc_hook)
    assert response.content == b'{"price":"caller\'s"}'


def test_a_hook_that_raises_not_implemented_leaves_the_value_to_the_registry():
    seen = []

    def enc_hook(value):
        seen.append(type(value))
        if isinstance(value, complex):
            return [value.real, value.imag]
        raise NotImplementedError

    data = {"price": Money("1", "EUR"), "z": 1 + 2j, "lazy": lazy_text()}
    with isolated_registry():
        register_money()
        response = JsonResponse(data, enc_hook=enc_hook)
    assert json.loads(response.content) == {
        "price": "1 EUR",
        "z": [1.0, 2.0],
        "lazy": "çeviri",
    }
    assert set(seen) == {Money, complex, type(lazy_text())}


@pytest.mark.parametrize("hooked", [False, True])
def test_an_unsupported_object_raises_djangos_error(hooked):
    def enc_hook(value):
        raise NotImplementedError

    message = "Object of type object is not JSON serializable"
    with pytest.raises(TypeError) as django:
        http.JsonResponse({"v": object()})
    with pytest.raises(TypeError) as ours:
        JsonResponse({"v": object()}, enc_hook=enc_hook if hooked else None)
    # On Python 3.14 json adds a note naming the item; the message is the same.
    assert ours.value.args == django.value.args == (message,)


def test_the_shared_encoder_gives_each_thread_its_own_body():
    workers = 8
    barrier = Barrier(workers)
    data = [
        {
            "id": index,
            "rows": [
                {"text": f"{index}-{row}", "lazy": lazy_text(), "n": row}
                for row in range(200)
            ],
        }
        for index in range(workers)
    ]
    expected = [JsonResponse(item).content for item in data]

    def encode(item):
        barrier.wait(timeout=10)
        return [JsonResponse(item).content for _ in range(50)]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(encode, data))
    for bodies, body in zip(results, expected, strict=True):
        assert bodies == [body] * 50


def test_the_module_does_not_import_drf():
    code = """
        import sys

        from django.conf import settings

        settings.configure()
        from fastdrf.msgspec.http import JsonResponse

        assert JsonResponse({"a": 1}).content == b'{"a":1}'
        assert "rest_framework" not in sys.modules, sorted(
            name for name in sys.modules if name.startswith("rest_framework")
        )
        """
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(["src", "."])}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_iterables_are_not_converted_as_in_django():
    for value in ((n for n in (1, 2)), iter([1])):
        with pytest.raises(TypeError) as django:
            http.JsonResponse({"v": value})
        with pytest.raises(TypeError) as ours:
            JsonResponse({"v": value})
        assert ours.value.args == django.value.args
