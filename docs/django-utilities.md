# JSON in Django views and templates

`MsgspecJSONRenderer` encodes DRF responses. Two functions of the `msgspec`
extra encode JSON with msgspec where DRF is not involved: Django views
(health checks, webhooks, plain `async def` views) and Django templates.

```python
from fastdrf.msgspec.http import JsonResponse
from fastdrf.msgspec.html import json_script
```

```django
{% load fastdrf_msgspec %}
{{ value|json_script:"data" }}
```

They are not exported from `fastdrf.msgspec`, whose names are the DRF
parser and renderer. `fastdrf.msgspec.http` does not import DRF. Both modules
need msgspec, and importing one without it raises `ModuleNotFoundError`.

## JsonResponse

```python
JsonResponse(data, *, safe=True, enc_hook=None, **kwargs)
```

A subclass of Django's `HttpResponse` (not of Django's `JsonResponse`),
used as Django's `JsonResponse` is:

```python
from fastdrf.msgspec.http import JsonResponse


async def health(request):
    return JsonResponse({"status": "ok", "checked": timezone.now()})
```

- `safe=True` refuses anything but a `dict` with Django's `TypeError` and
  message; `safe=False` encodes lists and scalars too.
- `content_type` defaults to `application/json`. `status`, `reason`,
  `headers`, `charset` and cookies are `HttpResponse`'s and give the same
  response as Django's `JsonResponse`.
- `enc_hook(obj)` converts the objects msgspec does not encode; see
  [hook order](#registered-types-and-hook-order).
- `encoder=` and `json_dumps_params=` are not accepted, and passing one
  raises Python's `TypeError` for an unexpected keyword argument. They
  configure the standard library's `json` module, which is not used; a
  project's `DjangoJSONEncoder` subclass becomes an `enc_hook`.
- `safe` is keyword-only. Django's second positional parameter is
  `encoder`, so a call moved from Django's response with a positional
  encoder raises `TypeError` instead of passing the encoder as `safe`.
- The body is UTF-8, the encoding JSON requires (RFC 8259), whatever
  `charset` says. Django's body is ASCII, with `\uXXXX` escapes. Both decode
  to the same value.

It is a plain `HttpResponse`, so it works the same in synchronous and
asynchronous views and under ASGI.

## json_script and the template filter

```python
json_script(value, element_id=None, *, enc_hook=None)
```

Django's `django.utils.html.json_script`, with `encoder=` replaced by
`enc_hook=`. The tag, its `id` (escaped by `format_html`) and its type are
Django's. The JSON inside is escaped so that it cannot end the script
element or a JavaScript string:

| Character | Written as | Django escapes it |
| --- | --- | --- |
| `<` | `\u003C` | yes |
| `>` | `\u003E` | yes |
| `&` | `\u0026` | yes |
| U+2028 | `\u2028` | its output is ASCII, so the character never appears |
| U+2029 | `\u2029` | its output is ASCII, so the character never appears |

msgspec writes U+2028 and U+2029 as they are, so this function escapes them
itself; a hypothesis test checks that no generated string leaves any of the
five characters between the tags.

The template library `fastdrf_msgspec` has a `json_script` filter with the
signature of Django's built-in filter (`value|json_script` or
`value|json_script:"id"`). A template opts in with
`{% load fastdrf_msgspec %}`, and the loaded filter replaces Django's in
that template only; a template that does not load it keeps Django's filter.

Django finds the library only when `"fastdrf"` is in `INSTALLED_APPS`, which
is otherwise optional ([configuration](configuration.md)). The library
imports msgspec when the filter runs, not when Django imports it, so a
project without msgspec keeps passing Django's template checks with
`"fastdrf"` installed; a template that uses the filter then fails with
`ModuleNotFoundError`.

## Registered types and hook order

The types registered with
[`register_msgspec_type()`](extending.md#msgspec-types) are encoded with
their `encode` hook, as `MsgspecJSONRenderer` and a typed `MsgspecCodec`
encode them: one registration gives the same JSON value in all four, which
a test checks for each.

For an object msgspec does not encode itself, the conversions run in this
order:

1. the `enc_hook` passed to the call, if any. A hook that raises
   `NotImplementedError` leaves the object to the next steps;
2. the registered type's `encode`, for the class or its nearest registered
   base;
3. the text of a `str` subclass (`SafeString`, a `TextChoices` member) or of
   a lazy translation string, as `DjangoJSONEncoder` converts them;
4. otherwise `TypeError` with the message of Django's encoder: "Object of
   type X is not JSON serializable".

msgspec never calls a hook for a type it encodes itself (a `dict`, a
`datetime`, a `Decimal`, a dataclass), and `register_msgspec_type()` refuses
such types. Steps 1 and 2 run in the same order for a schema serializer's
`Meta.enc_hook` and for `MsgspecCodec(type, enc_hook=...)`. QuerySets,
generators and other iterables are not converted, as `DjangoJSONEncoder`
does not convert them; pass a list.

Two exceptions are deliberate:

- An untyped `MsgspecCodec()` refuses a registered type: it would read back
  the encoded form, not the object. `MsgspecCodec(type)` round-trips it.
- `PydanticJSONRenderer` and `ORJSONRenderer` do not use the msgspec
  registry ([rendering](rendering.md)).

## Differences from Django's DjangoJSONEncoder

The output decodes to the same value as Django's for strings (Turkish,
Japanese, emoji), numbers, booleans, `None`, nested containers, `UUID`,
`Decimal` (a string, as Django writes it), `date`, a `datetime` or `time`
without microseconds, lazy strings and `SafeString`. The bytes differ:
msgspec writes no spaces after separators and writes non-ASCII text as
UTF-8.

These values give another result, each asserted in
`tests/test_django_http.py` so that a msgspec release that changes one is
noticed:

| Value | `DjangoJSONEncoder` | fastdrf |
| --- | --- | --- |
| `datetime` with microseconds | truncated to milliseconds (`03:04:05.123`) | kept (`03:04:05.123456`) |
| `time` with microseconds | truncated to milliseconds | kept |
| aware `time` | raises `ValueError` | encoded (`"03:04:05Z"`) |
| `timedelta` | `"P1DT00H00M03.000005S"` | `"P1DT3.000005S"` |
| float NaN | `NaN`, which is not valid JSON | `null` |
| `bytes` | raises `TypeError` | base64 string |

Keep Django's `JsonResponse` where clients compare these values as text.

## No signing or session serializer

fastdrf does not provide a msgspec serializer for `django.core.signing` or
for `SESSION_SERIALIZER`, and does not plan one:

- Django's `signing.JSONSerializer` writes ASCII and reads bytes as
  latin-1. msgspec cannot write ASCII-only JSON, and Django reads its UTF-8
  output of `{"b": "ü"}` as `{"b": "Ã¼"}`. A session or signed value written
  by such a serializer is corrupted after a rollback, or on servers still
  running Django's serializer; signed values and sessions outlive a deploy.
- Escaping everything outside ASCII in Python after msgspec would make it
  compatible, and would take back most of the time msgspec saves. Session
  payloads are small, and most of the time of `signing.dumps()` goes to the
  signature and base64, not to JSON.
- A wrong session serializer logs users out or corrupts their data.

## Measuring

`tools/benchmark_json_transport.py` compares Django's `JsonResponse` and
`json_script` with these on the one-row and 1,000-row bodies of the
[JSON transport measurements](json-transport-performance.md), on 1,000 rows
of datetimes, `Decimal`s, `UUID`s and lazy strings, and `json_script` on
1,000 rows whose strings are full of `<`, `>` and `&`. Each case checks
that both outputs decode to the same value first.
