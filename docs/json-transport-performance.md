# JSON transport measurements

These timings compare DRF's JSON parser and renderer with the optional
msgspec, Pydantic and orjson classes. All optional transports are opt-in; the
default renderer and serializer backend are DRF's. They were measured on the
[reference machine](benchmarks.md), an Intel Core i7-14700F, on one
performance core: other hardware gives other times.

## Method

Run from the repository root with the `msgspec`, `pydantic` and `orjson`
extras installed:

```console
taskset -c 4 .venv/bin/python tools/benchmark_json_transport.py
```

The script configures a minimal Django environment, checks the decoded output
against the input, warms each path, and reports the median and minimum of
seven repeats in microseconds per call. A call includes a new parser or
renderer instance; parsing includes a new `BytesIO`.

The body is a list of nested objects with numeric IDs, Unicode text, an author
object, tags and a boolean. One row is 95 bytes (5,000 calls per repeat);
1,000 rows are 95,891 bytes (300 calls per repeat). It holds no values whose
representation differs between the libraries (a raw `Decimal`, `bytes`).
These are warm microbenchmarks: they exclude HTTP dispatch, validation,
database access and network I/O.

## Current dependencies

Python 3.14.4, Django 6.1.1, DRF 3.18.1, msgspec 0.22.0, Pydantic 2.13.5
(pydantic-core 2.46.5), orjson 3.12.0. Medians in microseconds:

| Operation | DRF | fastdrf kept encoder | msgspec | Pydantic | orjson |
| --- | ---: | ---: | ---: | ---: | ---: |
| Render 1 row | 2.08 | 1.60 | 0.67 | 0.76 | 0.46 |
| Parse 1 row | 2.84 | — | 0.59 | 0.82 | 0.63 |
| Render 1,000 rows | 537.6 | 544.9 | 86.4 | 264.7 | 81.1 |
| Parse 1,000 rows | 486.7 | — | 252.5 | 375.7 | 233.6 |

## Minimum dependencies

Python 3.12.14, Django 5.2, DRF 3.16.0, msgspec 0.19.0, Pydantic 2.9.0
(pydantic-core 2.23.2), orjson 3.11.0:

| Operation | DRF | fastdrf kept encoder | msgspec | Pydantic | orjson |
| --- | ---: | ---: | ---: | ---: | ---: |
| Render 1 row | 1.88 | 1.47 | 0.74 | 0.90 | 0.50 |
| Parse 1 row | 2.88 | — | 0.61 | 0.81 | 0.64 |
| Render 1,000 rows | 417.4 | 415.9 | 89.0 | 421.9 | 123.3 |
| Parse 1,000 rows | 490.0 | — | 262.7 | 354.9 | 228.8 |

msgspec and orjson render and parse several times faster than DRF in both
environments. Pydantic's large render is close to DRF's on the minimum
versions and twice as fast on the current ones. The ranking of msgspec and
orjson changes with the versions and the body; measure your own payloads
before choosing.

## Pydantic string cache

`PydanticJSONParser.cache_strings` is `False` by default. On the large body,
the minimum pydantic-core decodes in about 1,700 µs with its string cache and
360 µs without it; the current pydantic-core is faster with the cache (about
320 µs against 385 µs). The parser uses one default for every supported
version; a subclass can set `cache_strings` to `True` or `"keys"`.

## Limits

- The libraries represent some values differently: speed does not make them
  interchangeable. See the [transport contracts](rendering.md).
- Indented, non-compact or ASCII output and a custom `encoder_class` use
  DRF's renderer and are not the native path measured here.
- Lazy objects, iterators, error responses, querysets and long unique strings
  have other costs than this body.
- Peak memory, throughput under concurrency, import time and whole-request
  latency are not measured.
