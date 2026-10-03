# django-fastdrf documentation

| Document | Contents |
| --- | --- |
| [Configuration](configuration.md) | Installation, the optional app, every `FASTDRF` setting, per-serializer and per-view options, system checks |
| [Serializers](serializers.md) | Serializer bases, output backends, parity and fallback, input recognition, field caching, list serializers |
| [Serializer styles](serializer-styles.md) | DRF declarations with msgspec/Pydantic execution, raw native APIs, native schemas wrapped as serializers, and behavior differences |
| [Migrating from DRF serializers](migrating-from-drf.md) | Contract-preserving backend adoption versus native schema migration, validation and persistence decisions, testing and rollback |
| [Serializer usage examples](serializer-examples.md) | Executable nested read/write, relation ID, collection, PATCH, schema, alias, context, ORM, form, and view examples with behavior limits |
| [Queries](queries.md) | Lookups derived from serializers, `Meta.prefetch`, `FETCH_MODE`, batched primary-key lookups, per-list enrichment |
| [Views and responses](views.md) | Dispatch mixins, compiled create and update responses, the releasing `Response`, `DataResponse` |
| [API views and backend selection](view-examples.md) | APIView, generic views, ViewSet actions and routes, schema endpoints, pagination, context, and python/msgspec/pydantic differences |
| [DRF view examples by class](drf-view-reference.md) | Function views, APIView, GenericAPIView, all concrete generic views and ViewSets, full imports, URL wiring, and DRF declarations compiled with msgspec |
| [Rendering and codecs](rendering.md) | The kept-encoder `JSONRenderer`, msgspec/Pydantic/orjson parsers and renderers, Redis cache codecs |
| [JSON in Django views and templates](django-utilities.md) | msgspec's `JsonResponse` and `json_script` with the `fastdrf_msgspec` template filter, registered types and hook order, differences from `DjangoJSONEncoder`, why there is no signing or session serializer |
| [JSON transport measurements](json-transport-performance.md) | Reproducible DRF, msgspec, Pydantic and orjson parser/renderer timings on current and minimum dependencies |
| [Benchmark environment](benchmarks.md) | The machine, software and method behind the published timings, and what changes them |
| [Fields of other packages](extending.md) | A worked example, `fastdrf.registry`, the django-phonenumber-field, django-countries and django-money integrations, `fastdrf.testing`, serializer mixins of other packages, msgspec types |
| [Schema serializers](schema-serializers.md) | `MsgspecSerializer`, `PydanticSerializer`, `SchemaViewMixin`, `ALLOWED_SERIALIZER_BACKENDS` |
| [Management commands](commands.md) | `fastdrf_inspect_serializers` and `fastdrf_convert` |
| [Architecture](architecture.md) | Design constraints, caches and their bounds, invalidation, thread safety, limits |

The [blog example](../examples/blog/README.md) serves the same API with plain
DRF and with fastdrf, and tests that both answer with the same bytes.
