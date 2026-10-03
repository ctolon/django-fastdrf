# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-10-03

Timings below were measured on the reference machine described in
[docs/benchmarks.md](https://github.com/ctolon/django-fastdrf/blob/main/docs/benchmarks.md);
other hardware gives other times.

### Added

- Opt-in `ORJSONParser` and `ORJSONRenderer` through the `orjson` extra,
  independent of serializer backends, with lazy imports and `DataResponse`
  support. Native JSON differences, formatting fallback and benchmarks are
  documented and tested on minimum and current orjson versions.

- Opt-in `PydanticJSONParser` and `PydanticJSONRenderer`, independent of schema
  validation and serializer backend selection, with lazy package exports and
  `DataResponse` support. Transport differences and reproducible benchmarks are
  documented; minimum and current Pydantic versions are covered by tests.

- `fastdrf.registry`: `register_model_field()`, `register_field()` and
  `register_key_field()` let the compiler accept the fields of other packages
  and of the project, with their own representation; `register_msgspec_type()`
  teaches every msgspec schema serializer, bare schemas of views included, a
  type msgspec does not know (validation, output and JSON Schema).
- `fastdrf.contrib.phonenumber`, `fastdrf.contrib.countries` and
  `fastdrf.contrib.money`: applications that register the fields of
  django-phonenumber-field, django-countries and django-money (extras
  `phonenumber`, `countries`, `money`). A list of 1,000 rows with these fields
  takes 11.9 ms with the msgspec backend instead of DRF's 16.2 ms.
- `FASTDRF["DELEGATE_FIELDS"]` and `Meta.delegate_fields`: the fields a
  backend cannot compile (`SerializerMethodField`, hyperlinked fields,
  properties, dotted sources, fields of other packages) are represented by
  their own code inside the compiled output instead of leaving the
  serializer to DRF, in nested serializers of foreign keys too. A list of 1,000 rows with five compiled fields and one
  `SerializerMethodField` takes 0.87 ms with the msgspec backend instead of
  DRF's 3.05 ms. Off by default: delegated fields run after the compiled
  fields read the instance.
- `fastdrf.testing.assert_compiled_as_drf()`: compares a serializer's
  compiled output with DRF's in a project's tests, each representation with
  instances of its own (a queryset, a factory or copies), optionally with
  its number of queries; `isolated_registry()` undoes the registrations a
  test makes.
- `register_field(options={"name": key})`: an option holding an object
  that DRF copies per serializer instance is compared by a key, not its
  identity; `fastdrf.contrib.countries` compares a field's `Countries` so.
- `Eligibility.delegated`, and the delegated fields in
  `fastdrf_inspect_serializers` (`delegated` in its JSON records).
- System check `fastdrf.I001`: a package that a `fastdrf.contrib`
  application integrates is installed without that application.
- `MsgspecJSONRenderer` and a typed `MsgspecCodec` use the types registered
  with `register_msgspec_type()`, also when the renderer falls back to DRF's
  (indented output).
- `fastdrf.signals`: `output_compiled` and `output_left_to_drf` (with a
  reason code) tell what produced each output of a compiling backend, for
  metrics and tests; sent only to connected receivers.
  `assert_compiled_as_drf` uses them to fail when the backend under test did
  not produce the output (a `Meta.serializer_backend` of another).
- `fastdrf.registry.registrations()` and `fastdrf_inspect_serializers
  --registrations` list what is registered. Registering the same again
  changes nothing; another registration of a registered class raises
  `ImproperlyConfigured` unless it passes `replace=True`.
- `fastdrf.prefetch.explain()` returns the `LoadingPlan` `auto_prefetch`
  applies, with each derived lookup it leaves out and why (the queryset's
  `Prefetch` decides the rows, the queryset defers the relation, ...).
- For an asynchronous layer (aiodrf): `compiled_for(serializer, awaits=True)`
  delegates fields whose code is a coroutine, and
  `compiler.delegated_steps()` gives each delegated field's steps, which the
  caller awaits; `docs/architecture.md` lists what runs without I/O.
- `fastdrf.registry.register_data_renderer()`: `DataResponse` renders with a
  project's or a package's JSON renderer itself instead of building DRF's
  `Response` for it.

### Changed

- A compiled serializer of one instance reads its class's `Meta` once per
  class: about 20% less time per `.data` than 0.3.0 (10.7 µs to 8.5 µs for
  a four-field `ModelSerializer`).

### Fixed

- `MsgspecJSONRenderer` gives DRF's error for a `Decimal` key that a
  conversion hook returns, and for any `Decimal` key with `DEBUG = True`;
  a project's `encoder_class` is used as DRF uses it.
- When `MsgspecJSONRenderer` falls back to DRF's renderer, the conversions
  msgspec already made (an iterator read, a registered type's `encode`,
  `tolist()`, and their failures) are reused instead of being made again.
- `isolated_registry()` keeps the built-in Pydantic and orjson
  `DataResponse` registrations when their modules are first imported inside
  it.
- Projected querysets no longer receive automatic relation loading, and
  instance-only HEAD handlers are included in cached view headers.
- Schema serializers omit msgspec UNSET and Pydantic cached properties from
  model writes, preserve equal-but-differently-represented validation changes,
  and do not revalidate existing Pydantic models only because output is bulk.
- Strict msgspec output falls back to DRF when native conversion cannot
  encode a source string, including nested conversion.
- Conversion preserves nullable Any, blank multiple-choice members, regex
  blank rules, container input/output separation and simple source mappings.
  Generated names avoid annotation and model-method collisions. Unsupported
  formats, precision/default/extra policies and msgspec choices are explicitly
  marked for manual completion. See [conversion limits](https://github.com/ctolon/django-fastdrf/blob/main/docs/commands.md#fastdrf_convert).
- In fast parity, a DRF field reading a column of another package's model
  field whose values are its own objects (django-phonenumber-field's
  `PhoneNumber`) failed the request with the backend's validation error;
  the value is now output as DRF's field outputs it.
- `auto_prefetch` (`QueryOptimizationMixin`) joined a relation that the
  view's queryset prefetched with a filtered `Prefetch`, so that Django
  skipped the `Prefetch`: rows the filter left out were in the response
  (for example another user's related object). The queryset's own
  `Prefetch` objects now keep their relation from being joined. Present
  since 0.1.0.
- `auto_prefetch` joined a foreign key that the queryset defers (`only()`,
  `defer()`), which Django refuses with `FieldError`; the relation is loaded
  as without it.
- `.data` of a schema serializer after `is_valid()` represented the input
  before `validate()`, not what `validate()` returned (`validated_data`),
  as DRF does.
- `MsgspecJSONRenderer` fell back to DRF's renderer for the words `NaN` or
  `Infinity` inside a string; only a non-finite number does.
- `auto_prefetch` derived `select_related("author_id")` for a field reading
  a foreign key's column (`author_id`), which Django refuses; a column is
  read without a join. A foreign key deferred by its column name
  (`defer("author_id")`) is not joined either.
- `.data` of a `many=True` schema serializer after `is_valid()` built the
  schema objects again, running `__post_init__` a second time; one item and
  a list now represent the objects read in validation, with what
  `validate()` changed set on a copy, and its removed fields left out.
- A `model_serializer` of a pydantic schema no longer refuses `.data` after
  `validate()` changed a field of complete input.
- `django-fastdrf[phonenumber]` installs the phone number parser
  (`phonenumberslite`) that django-phonenumber-field imports.
- `BATCH_RELATED_LOOKUPS`: a queryset that finds nothing (`none()`) raised
  `EmptyResultSet` for input of more than half the database's parameter
  limit instead of DRF's validation error; a window annotation was computed
  over all the items instead of over each item's row, as DRF's `get()`
  computes it, and such querysets are now left to DRF; a repeated item's
  instance shared its mutable values (a `JSONField`'s) with the first and
  is now looked up as DRF looks it up.
- `fastdrf_convert` wrote one class for the instances of a serializer class
  nested with other field options (a `max_length` of 3 and of 10), so the
  generated schema refused input DRF accepts; each contract now has its own
  class, and equal ones share it.
- In OpenAPI 3.0 documents (`fastdrf.spectacular`), a field that is only
  `None` was written `"type": "null"`, and a schema without required fields
  `"required": []`, both of which OpenAPI 3.0 refuses; they are now
  `"nullable": true, "not": {}` (null alone) and no `required`.
- Input recognition (msgspec and pydantic backends) accepted input on its
  own when the project had assigned a validation hook to an instance, such
  as `run_child_validation` on a nested list serializer or `set_value` on a
  serializer: every hook refused on a class is now refused on an instance,
  and DRF validates.
- `MsgspecCodec` wrote an integer from 0 to 127 that another value encodes
  as (an `Enum` member's, an `enc_hook`'s) as MessagePack's single byte,
  which it read back as ASCII digits: 49 came back as 1. Such a value is
  now written as a Redis integer.
- With the msgspec backend, a serializer with two fields of one source and
  a model field named like the compiler's own names (`_fastdrf_1`) lost a
  key and output another field's value; the compiler's names avoid every
  source.
- `FIELD_COPY_MODE = "compiled"` copied what was set on a declared field
  after its construction (`trim_whitespace = False`), which DRF's copies,
  built again from the field's arguments, do not have; such a field is now
  copied as DRF copies it.
- `.data` of a schema serializer with separate input and output schemas
  left out a field of the output schema that `validate()` added.
- A foreign key, `many=True` primary key relation or `SlugRelatedField`
  whose key or slug field converts what the database returns (its own
  `str` or `int` subclass, which DRF leaves in `.data`) stays on DRF: the
  msgspec backend raised `TypeError` and the pydantic backend output a plain
  `str` in `.data`.
- `QueryOptimizationMixin.get_queryset()` called `get_serializer_class()`
  and `get_serializer()` where DRF does not (a `destroy`, a serializer
  context reading the queryset), which failed or recursed; it now derives
  lookups only for a declared serializer, once per call, and leaves a
  serializer class it cannot resolve to DRF.
- A `ready()` registering a `lambda` again (tests, `isolated_registry()`)
  raised `ImproperlyConfigured`; registrations of the same code are equal.
- A partial pydantic schema serializer crashed for a model with a
  `default_factory` field, and its `.data` lost values when an alias is
  another field's name (`a` aliased `b`, `b` aliased `c`).
- Input recognition crashed in `is_valid()` for a msgspec field name that is
  not an identifier (`kebab-case`), and for an `IntegerField` with a float
  or infinite bound; such serializers are validated by DRF.
- `MsgspecJSONRenderer` wrote a `Mapping` that is not a `dict` as its keys
  and ignored DRF's `UNICODE_JSON` and `COMPACT_JSON`; DRF's encoder decides
  these. With `DEBUG = True` a `Decimal` key, which msgspec writes as a bare
  number, gives DRF's error.
- OpenAPI 3.0 documents kept JSON Schema's `examples` list, which OpenAPI
  3.0 refuses; it is now `example`, the first of them.
- In fast parity, a `ReadOnlyField` reading a UUID, float or date column
  through a foreign key failed every request; such a field stays on DRF.
- `auto_prefetch` did not prefetch a reverse relation named by Django's
  default accessor (`edition_set`).
- A schema serializer could not represent a model instance's to-many
  relation (a related manager); it reads the relation's items, from a
  prefetch if any.
- Schema serializer errors under an empty key (`{"": ...}`, `alias=""`)
  were moved to `non_field_errors` or crashed; an `AliasPath` counting from
  the end (`-1`) named no item, and its error was dropped from list-format
  errors.
- `fastdrf_convert` wrote classes named like its imports (`Field`,
  `BaseModel`, `Annotated`), which shadowed them; and marked a `ListField`'s
  length validators as not converted.
- `BATCH_RELATED_LOOKUPS` sent more parameters than the database allows for
  a small batch of a queryset with parameters of its own, where DRF's
  lookups succeed; the parameter limit and the IN list limit are now
  counted separately, and DRF looks the items up when no key fits.
- `.data` of a schema serializer after invalid input was `{}`; as in DRF it
  is the given input of the input schema's fields.
- `fastdrf_inspect_serializers` printed a traceback for an invalid
  `FASTDRF` setting; it is a command error.
- A registered field's representation reading `self.context` failed with
  `AttributeError: ... 'parent'`; it now says that compiled
  representations have no context.
- A method assigned to a relation's `child_relation` or `pk_field` on a
  serializer instance (a per-request redaction, say) was skipped by the
  compiled output, which read the value itself; such serializers are left
  to DRF.
- The schema caches built a class while holding their lock, so a pydantic
  class hook that built another partial schema deadlocked; building now
  runs outside the lock, and the first value published is everyone's.
- `auto_prefetch` recursed without end on a serializer nesting itself (a
  tree); the relation is loaded once and deeper levels as DRF loads them.
  It joined a reverse one-to-one by its accessor where `select_related()`
  needs its `related_query_name`, and added joins and lookups to
  `union()`, `intersection()` and `difference()` querysets, which Django
  refuses; those are left as they are (`skipped` says why).
- `BATCH_RELATED_LOOKUPS` called a `pk_field` conversion assigned to the
  instance again for repeated items; such fields are looked up by DRF.
- A schema serializer gave a field typed as the model class (pydantic's
  `arbitrary_types_allowed`) a wrapper instead of the model instance; only
  relations read through a nested schema are adapted.
- Partial output of a pydantic schema built the defaults of the fields not
  given (a `default_factory` reading other fields failed) and ran
  `model_post_init()`.
- A list serializer whose `validate()` reorders, drops or adds items
  represented an item with another item's schema object (private
  attributes, computed fields); each item is matched to its own object.
- `MsgspecJSONRenderer` rendered a generator as `[]` (and hid DRF's error)
  when DRF's renderer took over after msgspec had read it; the items read
  are reused.
- OpenAPI 3.0 documents kept JSON Schema keywords OpenAPI 3.0 lacks
  (`propertyNames` of a constrained dict key, `patternProperties`, ...);
  they are left out with a warning.
- msgspec validation errors were keyed under a made-up nested path when a
  wire name holds `.`, `[` or a backtick; the input's keys decide.
- `fastdrf_convert` wrote `Child(many=True, allow_null=True)` for a
  nullable list of non-null items, which accepted null items; a `None`
  member of a `Literal` as a choice, which DRF refuses; dropped a literal's
  constraints; and wrote msgspec tags, `array_like` and refused unknown
  fields without a note. The list is a `ListSerializer(allow_null=True)`,
  `None` is `allow_null`, the constraints choose the choices, and the wire
  formats are marked.
- A schema serializer nested in a serializer validating `partial=True`
  input required its missing fields and added its defaults, as DRF's nested
  fields do not; it is partial with its root.
- Form input (`QueryDict`) kept only the last value of a field typed as a
  union with a list (`list[str] | str`), a `Sequence` or a `deque`; such a
  field gets every value. A pydantic model with `strict=True` refused form
  input, which is validated with lenient coercion; `Meta.strict` unset
  leaves the model's own `strict` for other input.
- The output of a msgspec schema nesting Structs ran their `__post_init__`
  again; exact instances are written as they are, and only a subclass value
  is projected.
- OpenAPI documents of schema serializers (`fastdrf.spectacular`): a
  component whose nested component differs between request and response
  (a serialization alias) was shared by both sides, so the documented
  response named the input's fields; the components that use a renamed one
  are renamed too, recursive ones included. A discriminator's `mapping`
  kept the response component's name, and a field named `$ref` stopped
  the document with `TypeError`; references are now read where a schema
  has them.
- `fastdrf_convert`: a class with notes and no field was not valid Python
  (it now has `pass`); a `RegexField`'s flags (`re.IGNORECASE`) and a
  Unicode `SlugField` were converted to ASCII, case-sensitive patterns
  (DRF's regex is used, its flags inline); a pydantic class with a Python
  pattern pydantic's default engine refuses (look-around, `\Z`) did not
  load, and every pattern is now validated with `regex_engine="python-re"`,
  DRF's semantics; `many=True` items allowing null became non-null; a
  model's `str_min_length` and `str_max_length` were left out (now on its
  strings, a field's own limits first); an inherited `__post_init__` or
  `model_post_init` was not marked, while pydantic's own (private
  attributes) was; an empty msgspec wire name was dropped silently.
- A schema serializer nested in a DRF serializer represented its validated
  data as input, so a field with an alias or a missing partial field
  failed the parent's `.data`; the validated values are recognized as such,
  `many=True` too. Partial input to a pydantic model with
  `extra="allow"` read an extra field named like a model attribute
  (`model_dump`, `model_config`) as that attribute.
- A schema serializer could represent model instances with to-many
  relations only when a model came first in a mixed list, or when the
  nested schema was not behind `Annotated` or a `Sequence`; a relation of a
  field with `exclude=True`, read by validation, was not adapted.
- pydantic validation errors of a model with `loc_by_alias=False` went to
  `non_field_errors`; they are keyed by the field's input name. A missing
  `AliasPath` field of a nested model is keyed where the input stops, as at
  the top.
- Input recognition read ISO 8601 text for a `DateField` or `TimeField`
  given empty `input_formats`, which DRF refuses; a `FloatField` int bound
  that a float cannot hold (`2**53 + 1`) was rounded, accepting what DRF
  refuses; a nested serializer overriding `get_default()` lost its default.
  Such fields are validated by DRF.
- In strict parity a `ReadOnlyField` number of an `int` subclass, which DRF
  leaves as it is, was output as an `int` (the python backend called its
  `__int__`); such an instance is represented by DRF.

## [0.3.0] - 2026-10-02

### Added

- `fastdrf.spectacular`: a drf-spectacular extension that documents schema
  serializers (`MsgspecSerializer`, `PydanticSerializer`, `SchemaViewMixin`
  views) from their schema classes, installed by the application with
  drf-spectacular (`django-fastdrf[spectacular]`). Without it,
  drf-spectacular documented every field as a read-only string, lost
  constraints, nested classes and enums, and gave a `SchemaViewMixin` view
  the output schema as its request body.

### Fixed

- The view mixins (`DispatchOptimizationMixin`, `QueryOptimizationMixin`,
  `DataResponseMixin`, `NegotiationCacheMixin`, `RequestPlanMixin`,
  `SchemaViewMixin`) no longer have docstrings: drf-spectacular published
  them as the description of every view without one of its own.

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
