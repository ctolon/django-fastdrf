# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/).

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
