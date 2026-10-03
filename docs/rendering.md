# Rendering, parsing and cache codecs

## JSONRenderer with a kept encoder

DRF's `JSONRenderer` builds a new `json.JSONEncoder` for every response.
`fastdrf.renderers.JSONRenderer` is a subclass that builds it once per
`(encoder_class, ensure_ascii, compact, strict)` and reuses it. Select it
where DRF's renderer is selected:

```python
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "fastdrf.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}
```

It renders DRF's exact bytes, including with `UNICODE_JSON`, `COMPACT_JSON`,
`STRICT_JSON` and a project `encoder_class`. Indented output, a subclass that
defines `render` or `get_indent`, a `get_indent` set on the instance, and a
replaced `render` or `get_indent` on DRF's class go through DRF's `render`.

The kept encoder is shared between threads, so an `encoder_class` must not
keep state between `encode` calls. DRF's encoder does not.

## msgspec JSON renderer and parser

With the `msgspec` extra, `fastdrf.msgspec.renderers.MsgspecJSONRenderer` and
`fastdrf.msgspec.parsers.MsgspecJSONParser` encode and decode JSON with
`msgspec.json`:

```python
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "fastdrf.msgspec.renderers.MsgspecJSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "fastdrf.msgspec.parsers.MsgspecJSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
}
```

They are independent of the serializer backend: compiled serializers work
with any renderer, and these classes work with any serializer.

### Differences from DRF's renderer

The renderer's output differs from DRF's for some values:

| Value | DRF's `JSONRenderer` | `MsgspecJSONRenderer` |
| --- | --- | --- |
| `timedelta` | seconds as a string (`"3600.0"`) | ISO 8601 duration (`"PT3600S"`) |
| `bytes` | decoded as UTF-8 | base64 string |
| float NaN or infinity | raises `ValueError` (`STRICT_JSON`, the default) or writes `NaN` | `null` |
| raw `Decimal` in `.data` | number through `float` (`1.5`) | number with its own digits (`1.50`) |
| float exponent | `1e+300` | `1e300` |
| aware `time` | raises | rendered |
| `bytes`, `date`, `datetime`, `UUID` or `Enum` (not `IntEnum`) key | raises `TypeError` | written as msgspec writes the value (`"2020-01-01"`) |
| `Decimal` key | raises `TypeError` | from a conversion hook, or with `DEBUG = True`: DRF's error; otherwise a bare number, which is not valid JSON |

Serializer output has string keys: field names, and `DictField` writes its
keys as strings. A `Decimal` key can come from data built by hand or from a
conversion hook (a registered type's `encode`, `tolist()`). The renderer
checks a hook's result as it converts it, which costs nothing for other data.
Finding a `Decimal` key in the data itself would take a pass over the encoded
output as long as the encoding, with a full parse whenever a string holds a
colon (a datetime, a URL); that check runs with `DEBUG = True` only.

`DecimalField` output is already a string when `COERCE_DECIMAL_TO_STRING` is
on (DRF's default), so the `Decimal` row only concerns values a serializer
leaves as `Decimal`. Keep DRF's renderer where clients depend on DRF's exact
bytes for these values.

The renderer answers with DRF's renderer, and DRF's bytes or errors, for:

- indented output (the browsable API, or `; indent=` in the `Accept`
  header), a custom `encoder_class`, and DRF's `UNICODE_JSON = False` or
  `COMPACT_JSON = False`;
- keys that `json` writes as strings and msgspec refuses (`True`, `None`);
- a non-finite `Decimal`, and output that contains the words `NaN` or
  `Infinity` when that output is not valid JSON.

Hook conversion results and errors are retained only for the current render,
in occurrence order, so fallback does not invoke already-run conversions again.
This includes registered types, `tolist()` objects and one-shot iterators.
Repeated references can still trigger separate conversions during the original
encoding attempt. Buffers are released on success or failure, and nested
renders have separate replay state. Hooks should return stable representations;
replay does not deep-copy mutable application objects or undo their side effects.

A value neither encoder supports raises DRF's `TypeError`. Lazy translation
strings, `ErrorDetail`, numpy values and other iterables are converted as
DRF's encoder converts them. U+2028 and U+2029 are escaped as DRF escapes
them.

The parser answers invalid JSON, and a body its charset cannot decode, with
a `ParseError` (400) as DRF's `JSONParser` does; the error's message is
msgspec's. It also refuses two inputs DRF's parser accepts:

| Input | DRF's `JSONParser` | `MsgspecJSONParser` |
| --- | --- | --- |
| a number out of the float range (`1e400`) | `inf` | `ParseError` |
| an unpaired surrogate escape (`"\ud800"`) | a string holding it | `ParseError` |

## Pydantic JSON renderer and parser

With the `pydantic` extra, `PydanticJSONParser` and `PydanticJSONRenderer` use
`pydantic-core` for JSON transport. They accept ordinary Python input/output
data independently of the serializer backend. No BaseModel is required, and
the parser does not perform schema validation.

```python
REST_FRAMEWORK = {
    "DEFAULT_PARSER_CLASSES": [
        "fastdrf.pydantic.parsers.PydanticJSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "fastdrf.pydantic.renderers.PydanticJSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}
```

An endpoint can select them explicitly instead of changing project defaults:

```python
from rest_framework.views import APIView

from fastdrf.pydantic.parsers import PydanticJSONParser
from fastdrf.pydantic.renderers import PydanticJSONRenderer
from fastdrf.response import Response


class EchoAPI(APIView):
    parser_classes = [PydanticJSONParser]
    renderer_classes = [PydanticJSONRenderer]

    def post(self, request):
        return Response(request.data)
```

This echo endpoint demonstrates transport only. Validate application input with
a serializer before using it for writes or business operations. Both classes
are also available as lazy exports from `fastdrf.pydantic`. Importing the package
alone does not load the optional dependency. The renderer supports `DataResponse`
through the usual `DataResponseMixin` integration.

### Parser contract

- The parser returns Python values through `pydantic_core.from_json()`; it does
  not create models or run validators. Serializer context, PATCH, and validation
  remain the serializer's responsibility.
- Invalid JSON, invalid Unicode, and charset decoding errors become `ParseError`
  (400), with pydantic-core's error message. The request charset is respected.
- Partial JSON is never accepted. Duplicate object keys retain the last value.
- With DRF's default `STRICT_JSON=True`, explicit `NaN` and `Infinity` tokens are
  rejected; with strict JSON disabled, they are accepted. Numeric overflow such
  as `1e400` still produces infinity, as DRF does, unlike the msgspec parser.
- An unpaired surrogate escape is rejected, unlike DRF's default parser.

`PydanticJSONParser.cache_strings` defaults to `False`. The minimum supported
pydantic-core version's cache was substantially slower on the benchmark's large
body. A subclass may set it to `True`, `"all"`, `"keys"`, or `"none"`; measure
with your dependency versions and payloads before changing it. This changes
allocation/caching, not schema validation or the parsed values.

### Renderer contract

The compact native path uses `pydantic_core.to_json()`. It does not revalidate
`.data` through the input schema. For plain values, its important differences are:

| Value | Pydantic renderer's native path |
| --- | --- |
| `Decimal("1.50")` | JSON string `"1.50"`, not a JSON number |
| Non-finite Decimal | String such as `"NaN"` |
| Bytes | URL-safe base64, including padding (`b"\xff"` becomes `"_w=="`) |
| Float NaN/infinity | `null`, including when DRF strict JSON is enabled |
| `timedelta(hours=1)` | ISO 8601 duration `"PT1H"` |
| Aware `time` | Serialized rather than rejected by DRF |
| `None` dictionary key | `"None"`, not DRF's `"null"` |
| Boolean dictionary key | `"true"` or `"false"` |

Keep serializer output keyed by field names. Native Pydantic models and dataclasses
can also have their own native serialization behavior; passing `.data` from the
intended output serializer keeps that contract explicit. Lazy translations,
QuerySets, and unknown mapping/numpy-like objects use DRF's encoder `default`
where pydantic-core needs a fallback. U+2028/U+2029 are escaped as in DRF.
The msgspec custom-type registry is not used by this renderer.

Indentation (including `indent=0`), `UNICODE_JSON=False`, `COMPACT_JSON=False`,
or a custom `encoder_class` selects DRF's renderer **before** encoding starts.
Those paths follow DRF's representation rules too: a raw Decimal is a number
there, not the native path's string. Keep DRF's renderer if clients require one
unchanging DRF representation across every value and media-type option.

An encoding failure is not retried by rerendering the original object. This
avoids silently rereading exhausted iterators or calling application code twice.
Unsupported output raises the native serialization exception, normally
`PydanticSerializationError`, rather than promising DRF's exact exception type.
There are no retained iterator replay buffers. Subclasses/custom encoders should
be tested with their own output types and formatting settings.

Pydantic is an alternative transport, not a claim to outperform msgspec. See
[JSON transport measurements](json-transport-performance.md) for the benchmark
method, results on both dependency sets, and limitations.

## orjson JSON renderer and parser

Install `django-fastdrf[orjson]` to use `ORJSONParser` and `ORJSONRenderer`.
This extra adds HTTP JSON transport only, not a serializer backend, schema
serializer, or cache codec. It works with ordinary DRF serializers and all
fastdrf serializer backends. Do not set `SERIALIZER_BACKEND` to `"orjson"`.

```python
from rest_framework.response import Response
from rest_framework.views import APIView

from fastdrf.orjson.parsers import ORJSONParser
from fastdrf.orjson.renderers import ORJSONRenderer


class EchoAPIView(APIView):
    parser_classes = [ORJSONParser]
    renderer_classes = [ORJSONRenderer]

    def post(self, request):
        return Response(request.data)
```

These classes also work on generic views and viewsets through the same class
attributes, and with fastdrf's `DataResponse`. Both names are lazily exported
from `fastdrf.orjson`; importing that package alone does not require orjson.
For project-wide use:

```python
REST_FRAMEWORK = {
    "DEFAULT_PARSER_CLASSES": [
        "fastdrf.orjson.parsers.ORJSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "fastdrf.orjson.renderers.ORJSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}
```

The parser returns Python values; it does not validate a schema. It honors the
request charset, rejects malformed JSON and invalid Unicode with `ParseError`
(HTTP 400), and keeps the last duplicate object key. Unlike DRF, it rejects
`NaN`, `Infinity`, and overflowing floats such as `1e400`, even when `strict`
is false. Integers outside orjson's 64-bit range can become floats and lose
precision. Send identifiers or arbitrary-precision quantities as JSON strings
when that distinction matters to the API.

The renderer uses native orjson encoding with DRF's encoder default for types
orjson does not handle. It does not use the msgspec type registry or Pydantic
model configuration. Pass `serializer.data`; for a raw Pydantic model, first
call `model_dump(mode="json")` rather than depending on iterable conversion.

| Value | Native rendering path |
| --- | --- |
| `None` response data | Empty body, as in DRF |
| Non-finite float | JSON `null`, even with `strict=True` |
| Raw `Decimal` | JSON number via DRF's float conversion; precision can be lost |
| `bytes` | UTF-8 text via DRF; invalid UTF-8 raises an encoding error |
| `timedelta` | String containing total seconds, as in DRF |
| UTC `datetime` | ISO timestamp with `+00:00`, not DRF's `Z` suffix |
| Dataclass, UUID, enum | orjson's native representation |
| Integer outside `[-2**63, 2**64 - 1]` | Encoding error |
| Non-string dictionary key | Encoding error, including integer keys |

Lazy translations, QuerySets, mappings and objects with `tolist()` use DRF's
default conversion when orjson calls it. Unknown types fail; they are not
silently rendered as `null`. U+2028 and U+2029 are escaped as in DRF.

Indentation (including `Accept: application/json; indent=2`), ASCII-only output,
noncompact output, or a custom `encoder_class` selects DRF before encoding
starts. This is a semantic switch too: DRF can serialize integer dictionary
keys and larger integers, and handles non-finite floats according to `strict`.
Do not rely on formatting negotiation to choose a data contract.

Native failures propagate as `orjson.JSONEncodeError` (a `TypeError`); there is
no whole-body retry after an iterator has been consumed. No global orjson
options or per-request callback settings are introduced. Use a custom encoder
for a DRF contract, or a separate renderer subclass for a different native
contract. Subclasses need explicit registration for `DataResponse`, as described
in [views and responses](views.md).

See [JSON transport measurements](json-transport-performance.md) for the
reproducible benchmark and its limitations.

## Cache codecs

`fastdrf.codecs.MsgspecCodec` (MessagePack) and `fastdrf.codecs.PydanticCodec`
(JSON through a pydantic `TypeAdapter`) implement the serializer interface of
Django's `django.core.cache.backends.redis.RedisCache`, so cached values are
not pickled:

```python
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379",
        "OPTIONS": {"serializer": "fastdrf.codecs.MsgspecCodec"},
    }
}
```

A dotted path or a class is instantiated without arguments and accepts any
value the library encodes. Pass an instance to decode into a type:
`MsgspecCodec(Row)` or `PydanticCodec(Row)` (a model, a type expression or a
`TypeAdapter`). `MsgspecCodec` also takes `enc_hook` and `dec_hook`, and a
typed one uses the types registered with `register_msgspec_type()`
([msgspec types](extending.md#msgspec-types)).

- Integers are stored as Redis integers, as Django's own serializer stores
  them, so `incr()` and `decr()` use Redis's atomic `INCR`.
- `PydanticCodec` validates each value before storing it. For a type
  expression it enables the pydantic options under which every accepted value
  round-trips (non-finite floats, bytes as base64); a model, dataclass,
  `TypedDict` or `TypeAdapter` keeps its own configuration.
- Entries written by Django's pickle serializer cannot be read by these
  codecs, and the other way round. Clear the cache or change `KEY_PREFIX`
  when switching.
