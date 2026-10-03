"""Compare actual HTTP JSON parser/renderer methods on identical JSON data.

Run from the repository root with msgspec, pydantic and orjson extras installed:
    .venv/bin/python tools/benchmark_json_transport.py
No database or project settings are needed. Results are microseconds per call.

Django's ``JsonResponse`` and ``json_script`` are compared with
``fastdrf.msgspec.http.JsonResponse`` and ``fastdrf.msgspec.html.json_script``
on the same bodies, on one with datetimes, Decimals, UUIDs and lazy strings,
and ``json_script`` on one whose strings are full of ``<>&``.
"""

import argparse
import datetime
import decimal
import io
import json
import platform
import re
import statistics
import timeit
import uuid
from importlib.metadata import version

import django
from django.conf import settings


def main():
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--repeat", type=int, default=7)
    options.add_argument("--small-number", type=int, default=5000)
    options.add_argument("--large-number", type=int, default=300)
    args = options.parse_args()
    if min(args.repeat, args.small_number, args.large_number) < 1:
        options.error("repeat and iteration counts must be positive")
    settings.configure(SECRET_KEY="benchmark", REST_FRAMEWORK={})
    django.setup()

    from django import http
    from django.utils import html
    from django.utils.translation import gettext_lazy
    from rest_framework.parsers import JSONParser
    from rest_framework.renderers import JSONRenderer

    from fastdrf.msgspec import html as fastdrf_html
    from fastdrf.msgspec import http as fastdrf_http
    from fastdrf.msgspec.parsers import MsgspecJSONParser
    from fastdrf.msgspec.renderers import MsgspecJSONRenderer
    from fastdrf.orjson.parsers import ORJSONParser
    from fastdrf.orjson.renderers import ORJSONRenderer
    from fastdrf.pydantic.parsers import PydanticJSONParser
    from fastdrf.pydantic.renderers import PydanticJSONRenderer
    from fastdrf.renderers import JSONRenderer as KeptJSONRenderer

    def timed(cases, number, check):
        timings = {}
        for name, run in cases.items():
            check(name, run())
            samples = timeit.repeat(run, number=number, repeat=args.repeat)
            timings[name] = {
                "median_us": round(statistics.median(samples) * 1e6 / number, 3),
                "min_us": round(min(samples) * 1e6 / number, 3),
            }
        return timings

    def django_cases(data, number, *, responses=True):
        # Django's and fastdrf's outputs decode to the same value: their
        # bytes differ (spaces, \u escapes).
        cases = {}
        if responses:
            cases["response_django"] = lambda: http.JsonResponse(data, safe=False)
            cases["response_fastdrf"] = lambda: fastdrf_http.JsonResponse(
                data, safe=False
            )
        cases["json_script_django"] = lambda: html.json_script(data)
        cases["json_script_fastdrf"] = lambda: fastdrf_html.json_script(data)
        expected = {}

        def check(name, result):
            if name.startswith("response"):
                decoded = json.loads(result.content)
            else:
                body = re.fullmatch(r"<script[^>]*>(.*)</script>", result, re.DOTALL)
                assert not re.search("[<>&\u2028\u2029]", body[1]), name
                decoded = json.loads(body[1])
            assert expected.setdefault(name.rpartition("_")[0], decoded) == decoded

        return {
            "iterations": number,
            "timings": timed(cases, number, check),
        }

    results = []
    for rows in (1, 1000):
        data = [
            {
                "id": index,
                "title": "Café",
                "author": {"id": 1, "name": "Ada"},
                "tags": ["api", "django"],
                "active": True,
            }
            for index in range(rows)
        ]
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        number = args.small_number if rows == 1 else args.large_number
        cases = {
            "render_drf": lambda data=data: JSONRenderer().render(data),
            "render_kept": lambda data=data: KeptJSONRenderer().render(data),
            "render_msgspec": lambda data=data: MsgspecJSONRenderer().render(data),
            "render_pydantic": lambda data=data: PydanticJSONRenderer().render(data),
            "render_orjson": lambda data=data: ORJSONRenderer().render(data),
            "parse_orjson": lambda raw=raw: ORJSONParser().parse(io.BytesIO(raw)),
            "parse_drf": lambda raw=raw: JSONParser().parse(io.BytesIO(raw)),
            "parse_msgspec": lambda raw=raw: MsgspecJSONParser().parse(io.BytesIO(raw)),
            "parse_pydantic": lambda raw=raw: PydanticJSONParser().parse(
                io.BytesIO(raw)
            ),
        }

        def check(name, result, data=data):
            assert (json.loads(result) if name.startswith("render") else result) == data

        timings = timed(cases, number, check)
        timings.update(django_cases(data, number)["timings"])
        results.append(
            {"rows": rows, "bytes": len(raw), "iterations": number, "timings": timings}
        )
    # Values DjangoJSONEncoder converts; no microseconds, which msgspec keeps
    # and Django truncates.
    typed = [
        {
            "id": uuid.UUID(int=index),
            "created": datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
            "day": datetime.date(2024, 1, 2),
            "price": decimal.Decimal("19.90"),
            "status": gettext_lazy("Active"),
            "title": "Café",
        }
        for index in range(1000)
    ]
    results.append(
        {"body": "django_types", "rows": len(typed), **django_cases(typed, 300)}
    )
    escapes = [
        {"html": "<b>&amp;</b> <script>a < b && c > d</script>", "id": index}
        for index in range(1000)
    ]
    results.append(
        {
            "body": "escape_heavy",
            "rows": len(escapes),
            **django_cases(escapes, 300, responses=False),
        }
    )
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "versions": {
                    name: version(name)
                    for name in (
                        "django",
                        "djangorestframework",
                        "msgspec",
                        "pydantic",
                        "pydantic-core",
                        "orjson",
                    )
                },
                "repeats": args.repeat,
                "pydantic_parser_cache_strings": PydanticJSONParser.cache_strings,
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
