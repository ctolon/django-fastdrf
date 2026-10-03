# Schema serializers

See [serializer usage examples](serializer-examples.md#pydantic-nested-schema-validation-and-aliases)
for nested schemas, aliases, explicit patch schemas, context, model persistence,
and form input with executable examples.
For complete declarations of both libraries, including raw native APIs and
DRF-style compiled backends, see [serializer styles](serializer-styles.md).
For request/response handling in APIView and generic views, see
[schema endpoint examples](view-examples.md#schemaviewmixin-with-apiview).

`fastdrf.msgspec.serializers.MsgspecSerializer` and
`fastdrf.pydantic.serializers.PydanticSerializer` are DRF serializers defined
by a msgspec `Struct` or a pydantic `BaseModel`. The schema library validates
the input and represents the output, with its own types, coercions and
messages; DRF's fields are not involved. They need the `msgspec` or
`pydantic` extra.

Unlike [compiled serializers](serializers.md), whose output equals DRF's,
schema serializers follow the library's rules. Use them for endpoints that
are designed around a schema, not to speed up an existing DRF serializer.

## Defining one

```python
import msgspec

from fastdrf.msgspec.serializers import MsgspecSerializer


class BookIn(msgspec.Struct):
    title: str
    pages: int = 100


class BookSerializer(MsgspecSerializer):
    class Meta:
        schema = BookIn
```

| `Meta` option | Meaning |
| --- | --- |
| `schema` | The schema for input and output. |
| `input_schema`, `output_schema` | Separate schemas for input and output; each falls back to `schema` when not set. |
| `partial_schema` | The schema for `partial=True` input (see [partial updates](#partial-updates)). |
| `model` | A Django model that `create()` and `update()` write. |
| `strict` | msgspec: strict by default; `False` accepts `"12"` for an int, as DRF does. pydantic: the model's own `strict` by default; `True` turns on pydantic's strict mode, `False` turns it off. |
| `dec_hook`, `enc_hook`, `schema_hook` | msgspec only: hooks for custom types in validation, output and the JSON Schema. For every msgspec serializer, bare schemas of views included, register the type instead ([msgspec types](extending.md#msgspec-types)). |

## Behaviour

They are DRF serializers: `is_valid()`, `errors`, `validated_data`,
`save()`, `data`, `many=True`, `context` and generic views work as usual.

- `validated_data` is a dict of the schema's attributes, and
  `serializer.validated_object` is the schema instance. Pydantic cached
  properties are not input fields. Msgspec `UNSET` fields are omitted from
  validated data in both full and partial requests, so missing optional
  fields are not written to the model.
- With `Meta.model`, `create()` and `update()` assign the fields as given (a
  foreign key as `author_id`) and set to-many relations after the save, as
  DRF's `ModelSerializer` does. Nested writes are not handled. Without a
  model, write `create()` and `update()` as for any DRF serializer.
- Errors have DRF's `ValidationError` structure. msgspec stops at its first
  error (code `invalid` or `required`); pydantic reports all errors with its
  own codes. Nested lists are keyed as DRF's `LIST_SERIALIZER_ERRORS_AS_DICT`
  says; the items of an array input (an `array_like` Struct, a list
  `RootModel`) are keyed by index.
- Output is the output schema's. An instance of a subclass is represented
  with the schema's fields only, at every depth, and model instances are read
  by attribute. A to-many relation is read as the list of its items, as DRF
  reads `.all()`: prefetch it (`Meta.auto_prefetch` does not see schema
  fields) or each instance queries it. pydantic uses
  serialization aliases and its field and model serializers; msgspec uses the
  Struct's `rename` and tag. A `RootModel`, an `array_like` Struct or a
  `model_serializer` keeps its array or scalar shape.
- Datetimes follow the library's rules, not Django's time zone conversion.
- `.fields` are read-only DRF fields describing the output schema, for
  `OrderingFilter`, `OPTIONS` metadata and the browsable API. Form input
  (`QueryDict`) is validated with lenient coercion, a pydantic model's own
  `strict` included. A field that takes a list (a collection, `Sequence`,
  `deque`, or a union with one such as `list[str] | str`) is given the list
  of its values, one value included; other fields get the last value, as
  Django's `QueryDict` gives it.
- `PydanticSerializer` passes the serializer's `context` to pydantic's
  validation and serialization callbacks (`info.context`), for `many=True`
  and partial output too.
- Existing Pydantic model instances are serialized without revalidation,
  for single and bulk output alike, including mixed lists of models and
  input mappings. Mappings and attribute objects still need output validation.
- Changes made by `validate()` keep their final representation even when
  Python considers the replacement equal, such as `1` replaced by `True`
  or `Decimal("1.0")` replaced by `Decimal("1.00")`.
- `SERIALIZER_BACKEND` and `SERIALIZER_BACKEND_FALLBACK` do not apply to
  them.

### Partial updates

For `partial=True`, a schema with every field optional is derived from the
input schema, but only when nothing is lost by that, since a derived class
runs none of the schema's own validation. With a msgspec `__post_init__`, an
`array_like` Struct, pydantic validators, `model_post_init`,
`validate_default` or a `RootModel`, set `Meta.partial_schema`; without it a
partial update raises `ImproperlyConfigured`. A derived msgspec schema keeps
the Struct's names, unknown-field policy and tag. A schema serializer nested
in another serializer is partial when that serializer is, as DRF's fields are.

Before a save, `.data` of partial input holds the given fields only,
represented by the output schema. An output with a `model_serializer`, or an
`array_like` output Struct, raises `TypeError` instead.

### Classes without a subclass

`fastdrf.typed.adapt(Schema)` returns the serializer class for a bare schema
class, and `schema_serializer(In, Out, model=None)` the class for an input
and output pair of one library. Each class is built once and kept in a
bounded cache (`fastdrf.typed.SCHEMA_CACHE_SIZE`, 1024 classes); the caches
hold classes, never request data.

The default list serializer for `many=True` is `SchemaListSerializer`, which
represents plain children in one call;
`fastdrf.list_serializers.SchemaListSerializer` is its weakly bound version
(see [list serializers](serializers.md#weakly-bound-children)).

## In views

`fastdrf.typed.SchemaViewMixin` gives a view an input and an output schema;
either one alone does both:

```python
import msgspec
from rest_framework import viewsets
from rest_framework.views import APIView

from catalog.models import Book
from fastdrf.typed import SchemaViewMixin


class BookIn(msgspec.Struct):
    title: str
    isbn: str
    author_id: int


class BookOut(msgspec.Struct):
    id: int
    title: str


class BookViewSet(SchemaViewMixin, viewsets.ModelViewSet):
    queryset = Book.objects.all()
    input_schema = BookIn
    output_schema = BookOut


class NewBook(SchemaViewMixin, APIView):
    input_schema = BookIn
    output_schema = BookOut

    def post(self, request):
        body = self.get_validated_body()  # a BookIn; DRF's 400 if invalid
        book = Book.objects.create(**msgspec.structs.asdict(body))
        return self.schema_response(book, status=201)
```

`Book` is the application's model from the usage guide. An integer `author_id`
in a schema does not validate relationship access or model uniqueness; add those
checks to the write contract before exposing this endpoint.

- In a generic view, the serializer writes `queryset.model`: the view's
  `queryset` attribute, set on the class or given to `as_view()`. A
  `get_queryset()` override does not change it.
- `get_validated_body(partial=False)` validates `request.data` with the
  view's serializer context and returns the input schema instance.
- `schema_response(data, many=False, **kwargs)` represents `data` with the
  output schema and returns a [`fastdrf.response.Response`](views.md#responses-that-release-their-request-objects);
  `kwargs` are its `status`, `headers` and so on.
- Without either schema, a bare schema class set as `serializer_class`, or
  returned by the project's `get_serializer_class()`, is wrapped with
  `adapt()`, so a Struct or a model can be a generic view's serializer.
- `as_view()` raises `ImproperlyConfigured` when the schemas come from two
  libraries, or when the view has neither a schema, a `serializer_class` nor
  its own `get_serializer_class()`.

### Restricting serializer kinds

`FASTDRF["ALLOWED_SERIALIZER_BACKENDS"]` restricts the kinds of serializer
`SchemaViewMixin` views may use:

| Kind | Serializers |
| --- | --- |
| `"drf"` | DRF serializers, compiled or not |
| `"msgspec"` | a `Struct` or a `MsgspecSerializer` |
| `"pydantic"` | a pydantic model or a `PydanticSerializer` |

A view whose serializer or schemas are of another kind fails in `as_view()`
with `ImproperlyConfigured` naming the view. A serializer chosen per request
is checked when the view builds it. With the app installed, `manage.py
check` lists every such view in the URLconf (`fastdrf.E005`). Views without
the mixin are not checked, and the setting does not change how DRF
serializers are executed (`SERIALIZER_BACKEND` does).

## Converting existing code

[`manage.py fastdrf_convert`](commands.md#fastdrf_convert) writes the source
of a msgspec Struct or pydantic model for a DRF serializer, and of a DRF
serializer for a schema.

## JSON Schema

A schema serializer's backend describes its schemas:

```python
import msgspec

from fastdrf.msgspec.serializers import MsgspecSerializer


class BookSchema(msgspec.Struct):
    title: str


class BookSchemaSerializer(MsgspecSerializer):
    class Meta:
        schema = BookSchema


body, components = BookSchemaSerializer().backend.json_schema(
    BookSchema, ref_prefix="#/components/schemas/", direction="response"
)
```

`body` is the JSON Schema of the schema class, `components` those of the
schemas it refers to, which refer to each other through `ref_prefix`. A
pydantic model is described with its validation schema for a `"request"`
and its serialization schema (serialization aliases) for a `"response"`; a
Struct alike for both.

## OpenAPI (drf-spectacular)

With drf-spectacular installed (`pip install django-fastdrf[spectacular]`)
and `"fastdrf"` in `INSTALLED_APPS`, the OpenAPI document describes a schema
serializer from its schema classes (`fastdrf.spectacular`): request bodies
from the input schema, responses from the output schema, with their
constraints, nested classes (as components) and enums. A pydantic model whose
validation and serialization differ (aliases, computed fields) has a separate
`<Name>Request` component; a PATCH body requires nothing, or is
`Meta.partial_schema` when one is set. `Meta.ref_name` names the component.
Both OpenAPI 3.0 (drf-spectacular's default; `nullable` instead of a `null`
type) and 3.1 are written.

Without the application, drf-spectacular reads the serializer's synthetic
fields, which carry no types, constraints or nested classes: every field is
documented as a read-only string and request bodies are empty. DRF's own
serializers, compiled or not, need nothing: drf-spectacular reads their
fields as usual.

fastdrf's view mixins have no docstrings (their descriptions are comments),
so a view without a docstring of its own has no description in the
document; drf-spectacular takes the first docstring among a view's classes.
