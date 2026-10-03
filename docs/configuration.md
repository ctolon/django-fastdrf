# Configuration reference

## Installation

```console
pip install django-fastdrf
pip install "django-fastdrf[msgspec]"    # for the msgspec backend, renderer, parser and codec
pip install "django-fastdrf[pydantic]"   # backend, schema serializers, JSON parser/renderer and codec
pip install "django-fastdrf[orjson]"     # JSON parser/renderer only, not a serializer backend
pip install "django-fastdrf[spectacular]"  # for OpenAPI of schema serializers
pip install "django-fastdrf[countries]"    # fastdrf.contrib.countries; also money, phonenumber
```

django-fastdrf depends on Django and Django REST framework only. Install an
extra only for the features that need it; the `drf` and `python` backends
need neither. Adding the app to `INSTALLED_APPS` is optional; see
[the optional app](#the-optional-app).

### Requirements

| Django | DRF | Python |
| --- | --- | --- |
| 5.2 | 3.16, 3.17, 3.18 | 3.12, 3.13, 3.14 |
| 6.0 | 3.17, 3.18 | 3.12, 3.13, 3.14 |
| 6.1 | 3.18 | 3.12, 3.13, 3.14 |

These are the combinations the test suite runs in CI. It also runs on
free-threaded Python 3.14t, and at the declared minimum versions (Django 5.2,
DRF 3.16, msgspec 0.19, pydantic 2.9, orjson 3.11). `FETCH_MODE` needs Django 6.1.

## The `FASTDRF` setting

All project-wide options live in one dictionary. Every key is optional; these
are the defaults:

```python
FASTDRF = {
    "SERIALIZER_BACKEND": "drf",
    "SERIALIZER_BACKEND_PARITY": "strict",
    "SERIALIZER_BACKEND_FALLBACK": "drf",
    "DELEGATE_FIELDS": False,
    "CACHE_SERIALIZER_FIELDS": False,
    "FIELD_COPY_MODE": "deepcopy",
    "BATCH_RELATED_LOOKUPS": False,
    "FETCH_MODE": None,
    "ALLOWED_SERIALIZER_BACKENDS": ("drf", "msgspec", "pydantic"),
}
```

| Key | Default | Allowed values | Effect |
| --- | --- | --- | --- |
| `SERIALIZER_BACKEND` | `"drf"` | `"drf"`, `"msgspec"`, `"pydantic"`, `"python"` | What produces the output of fastdrf's serializer bases, and recognizes their input. See [output backends](serializers.md#output-backends). |
| `SERIALIZER_BACKEND_PARITY` | `"strict"` | `"strict"`, `"fast"` | `strict` compiles only what equals DRF's output; `fast` also accepts documented differences. See [parity](serializers.md#parity). |
| `SERIALIZER_BACKEND_FALLBACK` | `"drf"` | `"drf"`, `"error"` | What happens to a serializer the backend cannot compile: DRF represents it, or an exception is raised. See [fallback](serializers.md#fallback). |
| `DELEGATE_FIELDS` | `False` | `True`, `False` | Represent the fields the backend cannot compile with their own code, in the compiled output, instead of leaving the serializer to DRF. See [delegated fields](serializers.md#delegated-fields). |
| `CACHE_SERIALIZER_FIELDS` | `False` | `True`, `False` | Build a serializer class's fields once and copy them per instance. See [field caching](serializers.md#field-caching-and-copying). |
| `FIELD_COPY_MODE` | `"deepcopy"` | `"deepcopy"`, `"clone"`, `"compiled"` | How cached fields are copied. Applies only when fields are cached. |
| `BATCH_RELATED_LOOKUPS` | `False` | `True`, `False` | Look up the items of `PrimaryKeyRelatedField(many=True)` input in one query. See [batched lookups](queries.md#batched-primary-key-lookups). |
| `FETCH_MODE` | `None` | `None`, `"peers"`, `"raise"` | Django 6.1's `QuerySet.fetch_mode()` for querysets of `QueryOptimizationMixin` views. See [fetch mode](queries.md#fetch-mode). |
| `ALLOWED_SERIALIZER_BACKENDS` | `("drf", "msgspec", "pydantic")` | A non-empty list or tuple of `"drf"`, `"msgspec"`, `"pydantic"` | The kinds of serializer `SchemaViewMixin` views may use. See [restricting serializer kinds](schema-serializers.md#restricting-serializer-kinds). |

`SERIALIZER_BACKEND` and the options that follow it apply to fastdrf's
serializer bases (`fastdrf.serializers`) only. Serializers that subclass DRF's
classes directly are not affected.

Values are validated the first time each is read and kept until Django's
`setting_changed` signal (sent by `override_settings`) reloads them. An
invalid value, an unknown key, or a `FASTDRF` that is not a mapping raises
`ImproperlyConfigured` on every read; the message names the allowed values.
With the app installed, `manage.py check` reports the same errors at start-up
instead of in the first request that reads them.

Settings that DRF already has (parsers, renderers, pagination, schema
generation, `COERCE_DECIMAL_TO_STRING` and so on) stay in `REST_FRAMEWORK`
and keep their meaning. Databases, caches, middleware and transactions stay
Django configuration.

## Per-serializer options

These go in a fastdrf serializer's `Meta`:

| Option | Values | Effect |
| --- | --- | --- |
| `serializer_backend` | as `SERIALIZER_BACKEND` | Overrides the project backend for this serializer. |
| `serializer_backend_fallback` | as `SERIALIZER_BACKEND_FALLBACK` | Overrides the project fallback for this serializer. |
| `delegate_fields` | `True`, `False`, `None` | Overrides `DELEGATE_FIELDS`. `None` inherits. |
| `cache_fields` | `True`, `False`, `None` | Overrides `CACHE_SERIALIZER_FIELDS`. `None` inherits. |
| `field_copy_mode` | as `FIELD_COPY_MODE`, or `None` | Overrides `FIELD_COPY_MODE`. `None` inherits. |
| `auto_prefetch` | `True`, `False` | Lets `QueryOptimizationMixin` derive related lookups from the fields. |
| `prefetch` | lookups as `QuerySet.prefetch_related` takes them | Extra lookups for relations the fields do not show. |
| `list_serializer_class` | a list serializer class | DRF's option, unchanged. |

A serializer class may also set `default_list_serializer_class`, the list
serializer for `many=True` when `Meta.list_serializer_class` is not set; a
project base serializer sets it once for all its subclasses. See
[list serializers](serializers.md#list-serializers).

Schema serializers have their own `Meta` options; see
[schema serializers](schema-serializers.md).

## Per-view options

| Attribute | Values | Effect |
| --- | --- | --- |
| `serializer_field_cache` | `True`, `False`, `None` | Field caching for the serializers this view builds, below any serializer `Meta`. |
| `serializer_field_copy_mode` | as `FIELD_COPY_MODE`, or `None` | Copy mode for the serializers this view builds, below any serializer `Meta`. |

`QueryOptimizationMixin` declares both as `None`. The view is found through
the serializer's `context["view"]`, as DRF's generic views set it.

## The optional app

```python
INSTALLED_APPS = [
    # ...
    "rest_framework",
    "fastdrf",
]
```

The app (`fastdrf.apps.FastDRFConfig`) defines no models. It registers the
[system checks](#system-checks), makes the
[management commands](commands.md) available and, with drf-spectacular,
loads the OpenAPI extension for schema serializers. Every other feature works
without it.

The applications of `fastdrf.contrib` (`fastdrf.contrib.phonenumber`,
`fastdrf.contrib.countries`, `fastdrf.contrib.money`) register the fields of
those packages with the compiler; add the ones whose package the project
uses ([fields of other packages](extending.md)).

## System checks

With the app installed, `manage.py check`, and every command that runs the
system checks (`runserver`, `migrate`, Django's test runner), reports:

| Check | Reported when |
| --- | --- |
| `fastdrf.E001` | A `FASTDRF` value is not allowed. |
| `fastdrf.E002` | `FASTDRF` has a key that is not a setting. |
| `fastdrf.E003` | `FASTDRF` is not a mapping. |
| `fastdrf.E004` | `SERIALIZER_BACKEND` is `msgspec` or `pydantic` and that package is not installed. |
| `fastdrf.E005` | A `SchemaViewMixin` view in the URLconf uses a serializer that `ALLOWED_SERIALIZER_BACKENDS` does not allow. Every such view is listed. |
| `fastdrf.E006` | `FETCH_MODE` is set and Django has no `QuerySet.fetch_mode()` (before 6.1). |
| `fastdrf.I001` | `SERIALIZER_BACKEND` (not a serializer's `Meta.serializer_backend`) compiles, and django-phonenumber-field, django-countries or django-money can be imported without its `fastdrf.contrib` application installed ([fields of other packages](extending.md)). |

All but `fastdrf.I001`, a hint, are errors. Without the app none of them runs and the same mistakes
surface later: an invalid `FASTDRF` raises `ImproperlyConfigured` when a
value is first read, a missing backend package raises `ImportError` when a
serializer first uses it, a disallowed serializer raises in the view's
`as_view()`, and `FETCH_MODE` raises when a `QueryOptimizationMixin` view
builds its queryset.

## Example configuration

The [blog example](../examples/blog/README.md) enables most options at once:

```python
FASTDRF = {
    "SERIALIZER_BACKEND": "msgspec",
    "SERIALIZER_BACKEND_PARITY": "strict",
    "SERIALIZER_BACKEND_FALLBACK": "drf",
    "CACHE_SERIALIZER_FIELDS": True,
    "FIELD_COPY_MODE": "compiled",
    "BATCH_RELATED_LOOKUPS": True,
    "FETCH_MODE": "peers",  # Django 6.1
}
```

Start from the defaults and enable one option at a time, with tests that
compare responses, errors and query counts for the endpoints it affects.
