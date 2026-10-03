# django-fastdrf

Opt-in serializer, query and response optimizations for synchronous
[Django REST framework](https://www.django-rest-framework.org/) projects.

django-fastdrf is for DRF projects that stay synchronous (WSGI, or ASGI with
synchronous views) and want less time per request without leaving DRF. It
adds subclasses, mixins and settings next to DRF's own classes. Each
optimization is enabled explicitly, per project, per view or per serializer,
and falls back to DRF's code wherever it cannot produce DRF's result.

It does not replace or patch any Django or DRF class, add async support, or
change DRF's validation errors.

## Installation

```console
pip install django-fastdrf
```

The distribution is `django-fastdrf`; the import name is `fastdrf`. Extras
install the optional parts:

```console
pip install "django-fastdrf[msgspec]"      # msgspec backend, renderer, parser, codec
pip install "django-fastdrf[pydantic]"     # pydantic backend, schemas, JSON transport, codec
pip install "django-fastdrf[orjson]"       # JSON parser and renderer only
pip install "django-fastdrf[spectacular]"  # OpenAPI for schema serializers
pip install "django-fastdrf[countries]"    # also money, phonenumber: fastdrf.contrib
```

Adding `"fastdrf"` to `INSTALLED_APPS` is optional. It registers system
checks, two management commands and, with drf-spectacular, the OpenAPI
extension for schema serializers; everything else works without it.

## Quick start

Use fastdrf's serializer bases and choose a backend for their output:

```python
# settings.py
FASTDRF = {
    "SERIALIZER_BACKEND": "msgspec",  # or "pydantic", or "python" (no dependency)
    "CACHE_SERIALIZER_FIELDS": True,
    "FIELD_COPY_MODE": "compiled",
}
```

```python
# serializers.py
from fastdrf import serializers


class ArticleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Article
        fields = ["id", "title", "author", "published_at"]
        auto_prefetch = True
```

```python
# views.py
from rest_framework import viewsets

from fastdrf.views import DispatchOptimizationMixin, QueryOptimizationMixin


class ArticleViewSet(
    DispatchOptimizationMixin, QueryOptimizationMixin, viewsets.ModelViewSet
):
    queryset = Article.objects.all()
    serializer_class = ArticleSerializer
```

`.data`, `.is_valid()`, `.save()`, `many=True`, routers, pagination,
permissions and error responses are DRF's. With `"fastdrf"` in
`INSTALLED_APPS`, this command lists which serializers the backend compiles,
and why the others stay on DRF:

```console
python manage.py fastdrf_inspect_serializers
```

## Features

[Serializer styles](docs/serializer-styles.md) compares DRF declarations, raw
Pydantic/msgspec, and schema serializers. For an existing API, follow
[migrating from DRF serializers](docs/migrating-from-drf.md).

See the [serializer usage examples](docs/serializer-examples.md) for nested
relations, explicit writes, partial updates, collections, and Pydantic/msgspec
schemas. The examples include behavior limits and are executed in the test suite.
Continue with [API views and backend selection](docs/view-examples.md) for
APIView, generic views, ViewSets, schema endpoints, and backend-specific behavior.
The [DRF view reference](docs/drf-view-reference.md) covers each view class with
imports and URL wiring.

Serialization:

- Compiled serializer output with the msgspec, pydantic or dependency-free
  `python` backend, equal to DRF's `.data` in the default strict parity mode
  ([serializers](https://github.com/ctolon/django-fastdrf/blob/main/docs/serializers.md#output-backends)).
- Input recognition: JSON input that DRF would accept unchanged is validated
  by a compiled class; everything else, including every error, is DRF's
  ([input recognition](https://github.com/ctolon/django-fastdrf/blob/main/docs/serializers.md#input-recognition)).
- Cached field templates, copied per request with `deepcopy`, a scalar clone
  or a compiled copy plan
  ([field caching](https://github.com/ctolon/django-fastdrf/blob/main/docs/serializers.md#field-caching-and-copying)).
- List serializers whose child refers to the list weakly
  ([list serializers](https://github.com/ctolon/django-fastdrf/blob/main/docs/serializers.md#list-serializers)).
- Schema serializers: a msgspec `Struct` or a pydantic model as a DRF
  serializer, and `SchemaViewMixin` for views
  ([schema serializers](https://github.com/ctolon/django-fastdrf/blob/main/docs/schema-serializers.md)).
- Fields of other packages and of the project compiled through
  `fastdrf.registry`, with integrations for django-phonenumber-field,
  django-countries and django-money, project-wide msgspec types, and
  `fastdrf.testing` to check the compiled output against DRF's
  ([fields of other packages](https://github.com/ctolon/django-fastdrf/blob/main/docs/extending.md)).
- Delegated fields: `SerializerMethodField`, hyperlinked fields and other
  fields the backend cannot compile run their own code inside the compiled
  output (opt-in)
  ([delegated fields](https://github.com/ctolon/django-fastdrf/blob/main/docs/serializers.md#delegated-fields)).

Queries:

- `select_related` and `prefetch_related` derived from the serializer by
  `QueryOptimizationMixin`, explicit `Meta.prefetch` hints, Django 6.1's
  `FETCH_MODE`, one query for many-valued primary-key input, and
  `PrefetchListSerializer` for per-list enrichment
  ([queries](https://github.com/ctolon/django-fastdrf/blob/main/docs/queries.md)).

Views and responses:

- Dispatch mixins that keep content negotiation and request construction
  between requests, compiled create and update responses, a `Response` that
  releases its request objects when closed, and `DataResponse`, rendered
  without DRF's template response
  ([views and responses](https://github.com/ctolon/django-fastdrf/blob/main/docs/views.md)).
- A `JSONRenderer` that keeps its encoder, msgspec, Pydantic and orjson JSON
  parsers/renderers, and msgspec and Pydantic codecs for Django's Redis cache
  ([rendering and codecs](https://github.com/ctolon/django-fastdrf/blob/main/docs/rendering.md)).
- `JsonResponse` and `json_script` encoded by msgspec for Django views and
  templates outside DRF, with the registered msgspec types
  ([JSON in Django views and templates](https://github.com/ctolon/django-fastdrf/blob/main/docs/django-utilities.md)).

Tooling:

- System checks for the `FASTDRF` setting and two management commands:
  `fastdrf_inspect_serializers` and `fastdrf_convert`, which writes a schema
  for a serializer or a serializer for a schema
  ([commands](https://github.com/ctolon/django-fastdrf/blob/main/docs/commands.md)).

## Guarantees and limits

- No Django or DRF class is replaced, patched or monkeypatched. fastdrf's
  classes are subclasses and mixins that a project selects.
- Everything is opt-in. With the default settings, fastdrf's serializer bases
  behave as DRF's.
- In strict parity, compiled output equals DRF's output; a field, value or
  hook the compiler cannot prove equal keeps the serializer, or that one
  source, on DRF. Input recognition never produces an error of its own.
- Synchronous only: no ORM call becomes asynchronous, and transactions,
  authentication, permissions, throttling and pagination are DRF's and
  Django's.
- Schema serializers, `fast` parity and the optional JSON renderers have their own
  documented output and validation rules; they are not DRF-identical by
  design.
- A serializer instance belongs to one request; do not share it between
  threads.

See [architecture](https://github.com/ctolon/django-fastdrf/blob/main/docs/architecture.md)
for how the caches are bounded and invalidated, and for the deliberate
differences.

## Compatibility

| Django | DRF | Python |
| --- | --- | --- |
| 5.2 | 3.16, 3.17, 3.18 | 3.12, 3.13, 3.14 |
| 6.0 | 3.17, 3.18 | 3.12, 3.13, 3.14 |
| 6.1 | 3.18 | 3.12, 3.13, 3.14 |

The test suite also runs on free-threaded Python 3.14t (Django 6.1,
DRF 3.18) and at the declared minimum versions (Django 5.2, DRF 3.16,
msgspec 0.19, pydantic 2.9, orjson 3.11). `FETCH_MODE` needs Django 6.1.

## Documentation

- [Documentation index](https://github.com/ctolon/django-fastdrf/blob/main/docs/README.md)
- [Configuration reference](https://github.com/ctolon/django-fastdrf/blob/main/docs/configuration.md)
- [Example project](https://github.com/ctolon/django-fastdrf/blob/main/examples/blog/README.md):
  the same API with plain DRF and with fastdrf, with parity tests and a
  measurement script
- [Changelog](https://github.com/ctolon/django-fastdrf/blob/main/CHANGELOG.md)
- [Contributing](https://github.com/ctolon/django-fastdrf/blob/main/CONTRIBUTING.md)
- [Security policy](https://github.com/ctolon/django-fastdrf/blob/main/SECURITY.md)

## License

BSD 3-Clause. See
[LICENSE](https://github.com/ctolon/django-fastdrf/blob/main/LICENSE) and
[NOTICE](https://github.com/ctolon/django-fastdrf/blob/main/NOTICE): the optimizations derive from the aiodrf project, and the list-construction protocol from Django REST framework.
