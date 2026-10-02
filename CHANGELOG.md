# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-02

### Added

- The schema backends describe and validate a schema without a serializer:
  `backend.json_schema(schema, ref_prefix=..., direction=...)` returns its
  JSON Schema and the schemas it refers to (an OpenAPI generator's
  components), `backend.validate(...)` its validated values.
  `MsgspecSerializer` takes `Meta.schema_hook` for custom types, as msgspec's
  `schema_components` does.
- `serializer_for()` of `fastdrf.msgspec.serializers` and
  `fastdrf.pydantic.serializers` takes `base=`, a subclass of the schema
  serializer to build. With it, the internal hooks the schema serializers,
  the field cache, the static classification, the system checks and the
  `fastdrf_inspect_serializers` command now have let a package build on them
  with its own serializer and view classes (aiodrf does) instead of copying
  them.

### Changed

- `manage.py fastdrf_convert` marks what it did not convert with
  `# TODO(convert): ...` and names the converter, not the command, in the
  generated module's docstring, so that a package built on fastdrf can run it
  under its own command. It recognizes the project's validation hooks by the
  registered framework classes (`fastdrf.utils.framework_base`) instead of
  module names.
- `MsgspecJSONRenderer` looks for a single byte before searching the output
  for non-finite numbers and the U+2028/U+2029 separators: rendering 1,000
  rows takes about a third of the time when neither is present.
- Input recognition checks that the request body holds only JSON types
  without a call per value, skips the surrogate search for ASCII strings, and
  finishes list, dictionary and nested values with less work per item:
  validating a 100-line body takes about 40% less time.
- The msgspec backend converts datetime and decimal columns at once, reading
  the time zone and the decimal context once per column. Values whose
  conversion could run project code (another time zone class, a string
  subclass) are still converted row by row, in DRF's order. A list of 1,000
  rows of seven fields, one datetime and one decimal, takes about 28% less
  time.

### Fixed

- `fastdrf.response` no longer imports `rest_framework.views` when it is
  imported: a policy module named in DRF's settings (`DEFAULT_*_CLASSES`)
  that imported it was left partly initialized.
- A `ModelSerializer` of another framework's base registered with
  `framework_base` raised `TypeError` when its field template was built with
  `CACHE_SERIALIZER_FIELDS`.
- The fields of `django.contrib.contenttypes` (`GenericForeignKey`,
  `GenericRelation`), of composite primary keys and Django's JSON field module
  are Django's code: a model with them no longer keeps its serializers from
  being static (field caching, compiled output).
- A serializer whose representation is a coroutine function somewhere fastdrf
  did not look (a list serializer's own `to_representation`,
  `ato_representation` or `adata`, an `async def get_<field>` of a
  `SerializerMethodField`, a field reading an async model property or
  method, also behind a `functools.wraps` wrapper) is no longer compiled: it
  is left to its own code.

## [0.1.0] - 2026-10-01

First release.

### Added

Serializers:

- Serializer bases in `fastdrf.serializers` (`BaseSerializer`, `Serializer`,
  `ListSerializer`, `ModelSerializer`, `HyperlinkedModelSerializer`) that
  behave as DRF's with the default settings.
- Compiled serializer output with the `msgspec`, `pydantic` or
  dependency-free `python` backend (`SERIALIZER_BACKEND`, or
  `Meta.serializer_backend`). In `strict` parity the output equals DRF's,
  including datetimes in the current time zone, quantized decimals,
  primary-key and slug relations, file URLs, nested and to-many serializers.
  `fast` parity accepts documented differences.
- `SERIALIZER_BACKEND_FALLBACK`: serializers the backend cannot compile are
  represented by DRF, or raise with the reason.
- Input recognition for the msgspec and pydantic backends: JSON input that
  DRF would accept unchanged is validated by a compiled class; all other
  input, and every error, is DRF's.
- Field caching (`CACHE_SERIALIZER_FIELDS`) with `deepcopy`, `clone` and
  `compiled` copy modes (`FIELD_COPY_MODE`), configurable per serializer and
  per view.
- `default_list_serializer_class` for choosing the `many=True` list
  serializer of a project base, and weakly bound list serializers in
  `fastdrf.list_serializers`.
- Schema serializers: `MsgspecSerializer` and `PydanticSerializer`, defined by
  a msgspec `Struct` or a pydantic model, with `fastdrf.typed.adapt`,
  `schema_serializer`, `SchemaViewMixin` and the
  `ALLOWED_SERIALIZER_BACKENDS` setting.

Queries:

- `QueryOptimizationMixin`, which derives `select_related` and
  `prefetch_related` lookups from the serializer (`Meta.auto_prefetch`) and
  applies explicit `Meta.prefetch` hints.
- `FETCH_MODE` for Django 6.1's `QuerySet.fetch_mode()`.
- `BATCH_RELATED_LOOKUPS`: one query for the items of
  `PrimaryKeyRelatedField(many=True)` input.
- `PrefetchListSerializer` for loading data for all items of a list at once.

Views and responses:

- Dispatch mixins `NegotiationCacheMixin`, `RequestPlanMixin`,
  `DataResponseMixin` and `DispatchOptimizationMixin`.
- `fastdrf.mixins.CreateModelMixin` and `UpdateModelMixin`, which produce
  create and update responses with the serializer class's compiled encoder.
- `fastdrf.response.Response`, which releases its request objects when
  closed, and `DataResponse`, rendered without DRF's template response.

Rendering and caching:

- `fastdrf.renderers.JSONRenderer`, DRF's renderer with a kept encoder.
- `MsgspecJSONRenderer` and `MsgspecJSONParser`.
- `MsgspecCodec` and `PydanticCodec` for Django's `RedisCache`.

Tooling:

- An optional Django app (`"fastdrf"`) with system checks `fastdrf.E001` to
  `fastdrf.E006` and the management commands `fastdrf_inspect_serializers`
  and `fastdrf_convert`.
- Support for Python 3.12 to 3.14, Django 5.2, 6.0 and 6.1, and DRF 3.16 to
  3.18, tested on free-threaded Python 3.14t too.
- Type information (`py.typed`).
- An example project, `examples/blog`, that serves the same API with DRF and
  with fastdrf and tests both for identical responses.
