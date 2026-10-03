"""Compare actual HTTP JSON parser/renderer methods on identical JSON data.

Run from the repository root with msgspec, pydantic and orjson extras installed:
    .venv/bin/python tools/benchmark_json_transport.py
No database or project settings are needed. Results are microseconds per call.
"""

import argparse
import io
import json
import platform
import statistics
import timeit
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

    from rest_framework.parsers import JSONParser
    from rest_framework.renderers import JSONRenderer

    from fastdrf.msgspec.parsers import MsgspecJSONParser
    from fastdrf.msgspec.renderers import MsgspecJSONRenderer
    from fastdrf.orjson.parsers import ORJSONParser
    from fastdrf.orjson.renderers import ORJSONRenderer
    from fastdrf.pydantic.parsers import PydanticJSONParser
    from fastdrf.pydantic.renderers import PydanticJSONRenderer
    from fastdrf.renderers import JSONRenderer as KeptJSONRenderer

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
        timings = {}
        for name, run in cases.items():
            result = run()
            assert (json.loads(result) if name.startswith("render") else result) == data
            samples = timeit.repeat(run, number=number, repeat=args.repeat)
            timings[name] = {
                "median_us": round(statistics.median(samples) * 1e6 / number, 3),
                "min_us": round(min(samples) * 1e6 / number, 3),
            }
        results.append(
            {"rows": rows, "bytes": len(raw), "iterations": number, "timings": timings}
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
