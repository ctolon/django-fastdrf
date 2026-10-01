# Serializers

## Serializer bases

`fastdrf.serializers` provides `BaseSerializer`, `Serializer`,
`ListSerializer`, `ModelSerializer` and `HyperlinkedModelSerializer`,
subclasses of DRF's classes of the same names. It also re-exports DRF's
fields, so the module can replace `rest_framework.serializers` in imports:

```python
from fastdrf import serializers


class ArticleSerializer(serializers.ModelSerializer):
    word_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Article
        fields = ["id", "title", "published_at", "word_count"]
```

With the default settings these bases behave as DRF's. The options below
change how they produce output, validate input and build their fields. Their
public interface (`.data`, `.is_valid()`, `.errors`, `.validated_data`,
`.save()`, `many=True`, `context`) is DRF's, and stays synchronous.

Custom `to_representation`, `validate()`, field hooks and custom field classes
are respected. They make a serializer ineligible for the optimization that
would skip them, and DRF's code runs instead.

## Output backends

`SERIALIZER_BACKEND`, or `Meta.serializer_backend` on one serializer, selects
what produces `serializer.data`:

| Backend | Output | Input | Needs |
| --- | --- | --- | --- |
| `drf` | DRF | DRF | nothing |
| `msgspec` | compiled `msgspec.Struct` | recognized by a compiled `Struct`, else DRF | the `msgspec` extra |
| `pydantic` | compiled pydantic model, `strict=True` | recognized by a compiled model, else DRF | the `pydantic` extra |
| `python` | compiled per-field readers | DRF | nothing |

A compiling backend analyzes the serializer once, builds a class that reads
the instance's attributes, and produces the output from it. The result is
what DRF's `.data` would hold: a `ReturnDict` or `ReturnList` bound to the
serializer, cached in `_data` as DRF caches it. Rendering is unchanged; the
renderer DRF negotiates produces the bytes.

A serializer is compiled as a whole or not at all. In strict parity, these
fields compile:

- `ModelSerializer` fields backed by model fields of matching types: strings,
  integers, floats, booleans, UUIDs, dates and times, and choices whose keys
  have the model field's type;
- `DateTimeField` and `DecimalField` output as strings, as DRF formats them:
  converted to the current time zone, and quantized in the thread's decimal
  context;
- `PrimaryKeyRelatedField` and `SlugRelatedField` on forward foreign keys,
  and with `many=True` on many-to-many fields and reverse foreign keys;
- `ModelField` (which `ModelSerializer` builds for a `GeneratedField`), and
  `FileField` and `ImageField` of model file fields, whose URL is absolute
  when the serializer context has a request;
- the column of a forward foreign key (`<fk>_id`);
- a dotted `source` through foreign keys that cannot be null;
- a DRF scalar field that reads a value only the instance has, such as a
  queryset annotation;
- nested serializers on forward foreign keys, and nested `many=True`
  serializers on many-to-many fields and reverse foreign keys, compiled
  recursively.

Anything else keeps the serializer on DRF: `SerializerMethodField`, custom
field classes, `to_representation` or `get_attribute` overrides, methods
assigned to an instance, a list serializer with its own
`to_representation`, other dotted sources, and a nested serializer whose
model is not the related objects' model.

A serializer whose fields depend only on its class is analyzed once per
class. One whose fields change per instance (a `?fields=` style serializer)
is compiled once per field set, up to `fastdrf.compiler.MAX_VARIANTS` (32)
field sets per class; DRF represents the others.

When an instance's columns hold values of exactly the types Django's
database converters produce, a compiled static serializer produces its output
without building any serializer field.

### Parity

`SERIALIZER_BACKEND_PARITY = "strict"` (the default) compiles only output
that equals DRF's. `"fast"` additionally accepts:

- `DecimalField` values, output as `str(value)` instead of DRF's quantized
  string, also for fields DRF would leave as `Decimal` in `.data`;
- values DRF leaves as Python objects in `.data` (a UUID or a date through
  `ReadOnlyField` or `ModelField`), output as the backend formats them;
- fields of a plain `Serializer` without a model field behind them, and
  `JSONField` values;
- attributes read through properties or managers of the project's.

In fast parity a source the compiled class cannot read raises the backend's
error instead of being represented by DRF. `fast` is an explicit relaxation
for endpoints whose clients accept these differences, not a faster form of
`strict`.

### Fallback

`SERIALIZER_BACKEND_FALLBACK` (or `Meta.serializer_backend_fallback`) decides
what happens to a serializer the backend cannot compile:

- `"drf"` (the default): DRF represents it.
- `"error"`: `serializer.data` raises `ImproperlyConfigured` with the
  reason. Use it in tests to make sure a serializer stays compiled.

The setting is about serializers, not about the values one instance holds.
In strict parity, an instance the compiled class cannot read as DRF would
(a value of another type than the field expects, a missing related row, an
unsaved instance's related manager) is represented by DRF whatever the
fallback setting says. Input recognition always falls back to DRF.

## Input recognition

With the `msgspec` or `pydantic` backend, `is_valid()` first offers JSON input
(an exact `dict` or `list`) to a class compiled from the serializer. The
compiled class is a recognizer, not a second validator: it accepts only input
for which DRF's validation is known to return the same `validated_data`,
value for value and type for type. Whatever it does not accept, DRF
validates as usual, so every error, error code, message and coercion (such
as `"12"` for an integer) is DRF's.

Recognized fields are DRF's exact `BooleanField`, `IntegerField`,
`BigIntegerField` (DRF 3.17 and later), `FloatField`, `CharField`,
`ChoiceField`, `UUIDField`, `DateField`, `TimeField`, `ListField`,
`DictField` and `HStoreField`, with their length, bound and `allow_null`
options, and nested serializers made of them. A serializer stays on DRF for
input when it has:

- `validate()`, a `validate_<field>()` hook, or serializer validators
  (including the unique-together validators `ModelSerializer` derives);
- relation fields, custom fields or field validators beyond the field's own
  options (a `UniqueValidator`, for example);
- a default that is not a constant, including on a read-only field;
- a field with `source="*"`.

Form input (a `QueryDict`) always goes to DRF. The `python` and `drf`
backends do not recognize input.

## Field caching and copying

DRF builds a serializer's fields for every instance: `ModelSerializer`
inspects the model each time, and declared fields are deep-copied. With
field caching, a serializer class builds its fields once, and each instance
receives copies.

```python
FASTDRF = {
    "CACHE_SERIALIZER_FIELDS": True,
    "FIELD_COPY_MODE": "compiled",
}
```

| `FIELD_COPY_MODE` | Copies made with |
| --- | --- |
| `deepcopy` | `copy.deepcopy` of the cached template, as DRF copies declared fields |
| `clone` | a direct copy of the state of DRF's exact `BooleanField`, `CharField`, `IntegerField`, `BigIntegerField`, `FloatField`, `UUIDField` and `ReadOnlyField` with plain arguments; `deepcopy` for every other field |
| `compiled` | a copy plan prepared once per class that calls each field's constructor with copied arguments, recursively for container and nested fields; `deepcopy` for objects it does not know |

Each copy is independent and unbound, as DRF's are, and keeps DRF's field
creation order. Validators and regular expressions are shared between copies
as DRF shares them, and the managers DRF passes to related fields are shared
by every copy. `UniqueValidator`
messages that DRF builds from the model are formatted in the language active
when they are read, so a cached template does not keep the first request's
language.

Caching applies to a `ModelSerializer` class whose fields are a function of
the class: no `__init__`, `get_fields()`, `build_field()` or other
field-building hook of the project's, no `Meta.depth`, and no model field
whose construction calls project code (callable `choices` or
`limit_choices_to`, a `FilePathField`). Other serializers build their fields
as DRF does. For a plain `Serializer`, the `compiled` mode copies the
declared fields with a copy plan; `clone` has no effect there.

The options are resolved per serializer, in this order:

1. `Meta.cache_fields` and `Meta.field_copy_mode` of the serializer, then of
   each parent serializer outward;
2. `serializer_field_cache` and `serializer_field_copy_mode` on the view in
   the root serializer's `context["view"]`;
3. the project settings.

`None` means "not set here". The view attributes are read without running
properties or other descriptors, and only from a `dict` context.

Serializer declarations (fields, `Meta`, the model) must not change after the
first instance is built; a changed `FASTDRF` or `REST_FRAMEWORK` setting
clears the cached templates.

## List serializers

`many=True` uses `Meta.list_serializer_class` when set, as in DRF. Otherwise
fastdrf's bases use the class's `default_list_serializer_class`, and
`fastdrf.serializers.ListSerializer` (DRF's list serializer with the backend
options above) when that is `None`. A project base serializer can choose the list serializer for all its
subclasses:

```python
from fastdrf import serializers
from fastdrf.list_serializers import ListSerializer


class ModelSerializer(serializers.ModelSerializer):
    default_list_serializer_class = ListSerializer
```

### Weakly bound children

DRF binds a list serializer's child to the list (`child.parent`) and the list
holds the child, a reference cycle that also keeps the list's instances
alive until the cyclic garbage collector runs.
`fastdrf.list_serializers.ListSerializer` binds its child through a weak
proxy, so the list, its child and the instances are freed by reference
counting.

- `WeakChildMixin` does the same for another `ListSerializer` subclass, and
  `bind_child_weakly(list_serializer)` for one instance.
- `SchemaListSerializer` is the weakly bound list serializer for
  [schema serializers](schema-serializers.md); it imports neither msgspec nor
  pydantic.
- A child kept after its list is gone raises `ReferenceError` when it reads
  `parent`. `child.parent is list_serializer` is `False` for the proxy;
  compare with `==`.

`PrefetchListSerializer`, a weakly bound list serializer that loads related
data for all items of a list at once, is described in
[queries](queries.md#per-list-enrichment).

## Checking eligibility

[`manage.py fastdrf_inspect_serializers`](commands.md#fastdrf_inspect_serializers)
reports, for each serializer the API views declare, whether the backend
compiles its output and recognizes its input, and the reason when it does
not. The same information is available in Python:

```python
from fastdrf.compiler import report_details
from fastdrf.inputs import report_input_details

output = report_details(ArticleSerializer(), backend="msgspec")
input_ = report_input_details(ArticleSerializer(data={}), backend="msgspec")
```

Both return an `Eligibility` with a stable `code` and a readable `reason`.
