# django-fastdrf documentation

| Document | Contents |
| --- | --- |
| [Configuration](configuration.md) | Installation, the optional app, every `FASTDRF` setting, per-serializer and per-view options, system checks |
| [Serializers](serializers.md) | Serializer bases, output backends, parity and fallback, input recognition, field caching, list serializers |
| [Queries](queries.md) | Lookups derived from serializers, `Meta.prefetch`, `FETCH_MODE`, batched primary-key lookups, per-list enrichment |
| [Views and responses](views.md) | Dispatch mixins, compiled create and update responses, the releasing `Response`, `DataResponse` |
| [Rendering and codecs](rendering.md) | The kept-encoder `JSONRenderer`, msgspec's renderer and parser, Redis cache codecs |
| [Schema serializers](schema-serializers.md) | `MsgspecSerializer`, `PydanticSerializer`, `SchemaViewMixin`, `ALLOWED_SERIALIZER_BACKENDS` |
| [Management commands](commands.md) | `fastdrf_inspect_serializers` and `fastdrf_convert` |
| [Architecture](architecture.md) | Design constraints, caches and their bounds, invalidation, thread safety, limits |

The [blog example](../examples/blog/README.md) serves the same API with plain
DRF and with fastdrf, and tests that both answer with the same bytes.
