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

`DecimalField` output is already a string when `COERCE_DECIMAL_TO_STRING` is
on (DRF's default), so the `Decimal` row only concerns values a serializer
leaves as `Decimal`. Keep DRF's renderer where clients depend on DRF's exact
bytes for these values.

The renderer answers with DRF's renderer, and DRF's bytes or errors, for:

- indented output (the browsable API, or `; indent=` in the `Accept`
  header);
- keys that `json` writes as strings and msgspec refuses (`True`, `None`);
- a non-finite `Decimal`, and output that contains the words `NaN` or
  `Infinity`.

A value neither encoder supports raises DRF's `TypeError`. Lazy translation
strings, `ErrorDetail`, numpy values and other iterables are converted as
DRF's encoder converts them. U+2028 and U+2029 are escaped as DRF escapes
them.

The parser answers invalid JSON, and a body its charset cannot decode, with
a `ParseError` (400) as DRF's `JSONParser` does; the error's message is
msgspec's.

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
`TypeAdapter`). `MsgspecCodec` also takes `enc_hook` and `dec_hook`.

- Integers are stored as Redis integers, as Django's own serializer stores
  them, so `incr()` and `decr()` use Redis's atomic `INCR`.
- `PydanticCodec` validates each value before storing it. For a type
  expression it enables the pydantic options under which every accepted value
  round-trips (non-finite floats, bytes as base64); a model, dataclass,
  `TypedDict` or `TypeAdapter` keeps its own configuration.
- Entries written by Django's pickle serializer cannot be read by these
  codecs, and the other way round. Clear the cache or change `KEY_PREFIX`
  when switching.
