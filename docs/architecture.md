# Architecture

django-fastdrf speeds up the work DRF repeats for every request without
changing what DRF returns. This document describes how, the rules the code
follows, and where the package deliberately stops.

## Design constraints

No Django or DRF class is patched, monkeypatched or replaced, at import or at
run time. Every optimization is a subclass or a mixin that a project selects.
The one scoped replacement is per instance: batched primary-key lookups give
the field instances of one validation a `to_internal_value` and restore it in
a `finally` block.

Everything is opt-in. The default settings select DRF's behaviour, and a
project enables each feature in `FASTDRF`, in a serializer's `Meta` or on a
view.

Each optimization applies only where it can show that the result equals
DRF's. A custom hook, an unknown field class, a value of an unexpected type
or a request whose state the project changed takes DRF's code path for that
step.

Framework code is identified by class, never by module name. A hook counts
as the framework's when Django's or DRF's own classes define it, or a class
registered with `fastdrf.utils.framework_base`. A hook defined by a project
class anywhere in the MRO, or set on an instance, counts as the project's.
The fields of `django.contrib.contenttypes` are registered once the
application registry is ready, when the application is installed.

A package can build on these optimizations with its own serializer bases
(aiodrf does): it registers its bases with `framework_base`, so that their
hooks count as the framework's. Register only bases whose hooks give DRF's
result; a base whose representation differs (a schema serializer's, a list
serializer that awaits) stays unregistered and keeps its serializers on its
own code.

The package is synchronous. No ORM call becomes asynchronous, and Django's
thread and transaction model is unchanged.

## Request paths

1. Input: `is_valid()` offers exact `dict` or `list` input to a recognizer
   compiled from the serializer (msgspec or pydantic backend). The recognizer
   accepts only input for which DRF would return the same `validated_data`;
   everything else, including all invalid input, is validated by DRF, so
   errors, codes, messages and their order are DRF's. A static serializer
   finds its recognizer by class, without building its fields.
2. Output: `.data` asks the compiler for an encoder. A static serializer
   uses one compiled class; a serializer whose fields change per instance
   uses one variant per field set. The encoder receives the serializer's
   context and reads the current time zone and decimal context once per
   output. The result is stored as DRF stores it (`_data`, `ReturnDict` or
   `ReturnList`). An instance whose columns hold values of exactly the types
   Django's converters produce takes a path that builds no serializer field.
3. Fields: with field caching, `get_fields()` returns copies of a
   template built once per class. `clone` copies exact scalar field state;
   `compiled` replays constructors with copied arguments through a prepared
   plan. Both are Python code, and both use `deepcopy` for objects they do
   not know.
4. Queries: `QueryOptimizationMixin` adds `select_related` and
   `prefetch_related` lookups to the queryset the view already scoped. It
   never filters, and never replaces permissions, pagination or
   transactions.
5. Dispatch: the dispatch mixins keep the results of content negotiation
   and request construction per view class and `Accept` key. Configuration
   DRF reads on every request (renderer, parser and authentication classes,
   `as_view()` arguments, lists changed in place) is still read on every
   request.

A serializer is *static* when its fields are a function of its class: DRF's
code alone builds them, its declared fields are DRF's exact classes
(children included), it has no `Meta.depth`, and the instance shadows
nothing of its class. Per-class caches hold answers for static serializers
only; anything else is analyzed per instance.

## Caches and their bounds

Every cache is keyed by classes or by small request-independent values, and
none holds a request, a serializer instance, a database connection or model
data.

| Cache | Key | Bound |
| --- | --- | --- |
| `FASTDRF` values | setting name | the settings themselves |
| Class caches (`fastdrf.utils.class_cache`): field templates, copy plans, hook classification, view plans | class (weak) | `CLASS_CACHE_SIZE` (1024) entries per cached function |
| Compiled encoders and input recognizers | serializer class (weak), then variant signature | `MAX_SERIALIZER_CLASSES` (1024) classes, `MAX_VARIANTS` (32) variants per class |
| Derived prefetch lookups | serializer class and model (weak) | one entry per static class and model |
| Content negotiation | `Accept` value, format, renderer media types | 1024 entries, emptied when full; headers over 256 characters are not kept |
| Schema-derived classes (`fastdrf.typed.BoundedCache`) | schema class or pair | `SCHEMA_CACHE_SIZE` (1024) entries each, second-chance (CLOCK) eviction |

Weak class keys let classes created at run time (DRF builds a nested class
per instance for `Meta.depth`) be collected. Schema-derived classes refer to
their schema, so a weak cache would never release them; those caches are
bounded instead, and a project with more schemas than the bound rebuilds the
evicted ones.

## Invalidation

- `FASTDRF` values are cached until Django's `setting_changed` signal for
  `FASTDRF`.
- Field templates and copy plans are cleared when `FASTDRF` or
  `REST_FRAMEWORK` changes; compiled encoders and recognizers when
  `REST_FRAMEWORK` changes, since they depend on DRF's formats. Backend and
  parity are part of the encoder key.
- Registering a class with `framework_base` clears every cache whose answers
  depend on which classes are the framework's
  (`fastdrf.utils.depends_on_classification`). Register bases at import
  time.
- A value computed while a cache was cleared is not published: it may have
  been computed from the old state.

Changing settings is meant for tests (`override_settings`). Do not change
process-wide settings per request, and do not change serializer
declarations, `Meta` or models after the first instance is built.

## Thread safety and free-threading

The caches are designed for concurrent readers, including Python builds
without the GIL:

- A cache hit takes no lock. Values are computed outside the lock, since
  computing one may run project code, and published under it; threads racing
  on one key compute equal values and the first one is kept.
- Bounds are enforced under the lock, so concurrent publications cannot pass
  them. `BoundedCache` builds under its lock, so one key never gets two
  classes.
- Copy plans use a separate `deepcopy` memo per copy.

The test suite runs on free-threaded Python 3.14t. It does not force the GIL
off: an extension module that does not declare free-threading support
re-enables the GIL when imported, as CPython does by default.

Some state remains the project's responsibility:

- A serializer instance belongs to one request and must not be shared
  between threads, as with DRF.
- Validator objects are shared between field copies exactly as DRF shares
  them; a validator with mutable state is not made thread-safe.
- A kept JSON encoder is shared between threads; an `encoder_class` must not
  keep state between calls.

## Request lifetimes

DRF's view, request and response refer to one another, and a list serializer
and its child form a cycle that holds the page's instances. These cycles
keep a request's objects alive until the cyclic garbage collector runs, whose
cost grows with everything in flight. `fastdrf.response.Response.close()`
removes the back-references once the response is sent, and
`fastdrf.list_serializers.ListSerializer` binds its child weakly, so
reference counting frees those objects. Responses and list serializers that DRF creates keep their cycles.
See [views and responses](views.md#responses-that-release-their-request-objects).

## Deliberate differences and limits

Fast parity, schema serializers and the msgspec renderer have their own
documented output and validation rules. They are opt-in and are not
DRF-identical.

`DataResponse` keeps only its rendered content: `data` is `None` after
rendering, and `process_template_response` middleware does not see it.

With weakly bound list children, `child.parent is list_serializer` is
`False`, and a child kept after its list is gone raises `ReferenceError` when
it reads `parent`.

Third-party field classes stay on DRF's path. The compatibility tests cover
DRF's own classes, not arbitrary field classes.

Async views, async ORM access, streaming responses and concurrent list
enrichment are not included, since they need an async execution layer.
`PrefetchListSerializer` provides list enrichment in synchronous form. There
is no OpenAPI integration for schema serializers.

## Provenance

The compiler, input recognizer, field copying, relation batching and
prefetch derivation derive from the BSD-licensed aiodrf project. The
list-construction protocol follows DRF's `BaseSerializer.many_init`.
Copyright notices are in `LICENSE` and `NOTICE`.
