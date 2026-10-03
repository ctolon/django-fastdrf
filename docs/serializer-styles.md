# DRF declarations and native schema declarations

fastdrf supports two different contracts. A DRF-style serializer keeps DRF's
fields and validation rules while an eligible execution path uses a compiled
backend. A schema serializer uses Pydantic or msgspec as the API's validation
and representation contract. Both can be used in DRF views, but they are not
interchangeable implementations of every field rule.

The examples below include their imports and can be read independently.
`catalog.models.Book` is the application model from
[serializer usage examples](serializer-examples.md), not a fastdrf model.
Install the `pydantic` or `msgspec` extra for the corresponding examples.

## DRF style, msgspec execution

```python
from catalog.models import Book
from fastdrf import serializers


class BookSerializer(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "title", "pages"]
        serializer_backend = "msgspec"


incoming = BookSerializer(data={"title": " Notes ", "pages": "12"})
incoming.is_valid(raise_exception=True)
assert incoming.validated_data == {"title": "Notes", "pages": 12}

book = Book(id=1, title="Notes", pages=12)
assert BookSerializer(book).data == {"id": 1, "title": "Notes", "pages": 12}
```

There is no hand-written Struct. fastdrf constructs the compiled representation.
The string integer and surrounding whitespace above require DRF validation;
selecting msgspec does not remove those DRF conversions. Supported canonical
input, such as `{"title": "Notes", "pages": 12}`, can use msgspec recognition.

The example validates without saving: this input does not provide all fields
needed to create the application's `Book`. Use a complete write serializer for
persistence, as in the [relation-ID example](serializer-examples.md#read-nested-objects-write-relation-identifiers).

## DRF style, Pydantic execution

```python
from catalog.models import Book
from fastdrf import serializers


class BookSerializer(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "title", "pages"]
        serializer_backend = "pydantic"


incoming = BookSerializer(data={"title": " Notes ", "pages": "12"})
incoming.is_valid(raise_exception=True)
assert incoming.validated_data == {"title": "Notes", "pages": 12}

book = Book(id=1, title="Notes", pages=12)
assert BookSerializer(book).data == {"id": 1, "title": "Notes", "pages": 12}
```

No `BaseModel` declaration is required either. This is still a DRF contract:
Pydantic's own defaults for extras, whitespace, aliases, and errors do not become
the endpoint's rules. Application validation hooks run through DRF. A plain
`fastdrf.serializers.Serializer` supports the same backend option, but strict
output compilation generally requires model-backed fields and sources; input
eligibility is checked separately.

Use fastdrf's serializer bases for both examples. Changing settings does not
patch existing classes based only on `rest_framework.serializers`. The `python`
backend is another execution choice for DRF-style declarations: compiled output
readers with no optional schema library and no compiled input recognition.

## Native Pydantic, without a DRF wrapper

```python
from pydantic import BaseModel, Field


class BookSchema(BaseModel):
    title: str = Field(min_length=1)
    pages: int = Field(default=100, ge=1)


book = BookSchema.model_validate({"title": "Notes", "pages": "12"})
assert book.pages == 12
data = book.model_dump(mode="json")
json_text = book.model_dump_json()
decoded = BookSchema.model_validate_json(json_text)
assert decoded == book
```

These are Pydantic APIs. `model_dump()` produces Python data;
`model_dump_json()` produces JSON text. There is no `.is_valid()`, `.save()`,
DRF error response, or automatic Django model persistence. Validation raises
Pydantic's exception. If raw validation is called directly in an APIView, the
view must deliberately translate those exceptions; a schema serializer does
that integration for you.

This model accepts `"12"` by Pydantic's default coercion. It does not trim the
title unless configured to do so. A nonempty string constraint alone is not
equivalent to DRF's default `CharField` treatment of whitespace-only input.

## Native msgspec, without a DRF wrapper

```python
from typing import Annotated

import msgspec


class BookSchema(msgspec.Struct):
    title: Annotated[str, msgspec.Meta(min_length=1)]
    pages: Annotated[int, msgspec.Meta(ge=1)] = 100


book = msgspec.convert({"title": "Notes", "pages": 12}, type=BookSchema)
data = msgspec.to_builtins(book)
json_bytes = msgspec.json.encode(book)
decoded = msgspec.json.decode(json_bytes, type=BookSchema)
assert decoded == book
```

`convert()` validates parsed Python values; typed `json.decode()` parses and
validates bytes. Conversion is strict by default, so `"12"` is not an integer
here. `strict=False` enables msgspec's conversions, not DRF's full coercion rules.
Struct construction such as `BookSchema(title="Notes", pages=12)` is not a
replacement for validating untrusted input through conversion or typed decoding.

Raw msgspec likewise provides no DRF `.save()` or error-response integration.
`json.encode()` returns bytes, unlike Pydantic's `model_dump_json()` text.

## Pydantic style, integrated as a DRF serializer

```python
from pydantic import BaseModel, Field

from fastdrf.pydantic.serializers import PydanticSerializer


class AuthorSchema(BaseModel):
    name: str


class BookSchema(BaseModel):
    title: str = Field(min_length=1)
    author: AuthorSchema
    pages: int = Field(default=100, ge=1)


class BookSerializer(PydanticSerializer):
    class Meta:
        schema = BookSchema


serializer = BookSerializer(data={"title": "Notes", "author": {"name": "Ada"}})
serializer.is_valid(raise_exception=True)
assert isinstance(serializer.validated_object, BookSchema)
assert isinstance(serializer.validated_data["author"], AuthorSchema)
assert serializer.data == {"title": "Notes", "author": {"name": "Ada"}, "pages": 100}
```

Now `.is_valid()`, `.errors`, `.validated_data`, `.data`, `many=True`, and serializer
context are available, but validation is Pydantic's. Nested validated values
remain model instances. With no `Meta.model` or custom persistence methods,
`.save()` has no implementation for creating your application objects.

## msgspec style, integrated as a DRF serializer

```python
import msgspec

from fastdrf.msgspec.serializers import MsgspecSerializer


class AuthorSchema(msgspec.Struct):
    name: str


class BookSchema(msgspec.Struct):
    title: str
    author: AuthorSchema
    pages: int = 100
    note: str | None | msgspec.UnsetType = msgspec.UNSET


class BookSerializer(MsgspecSerializer):
    class Meta:
        schema = BookSchema


serializer = BookSerializer(data={"title": "Notes", "author": {"name": "Ada"}})
serializer.is_valid(raise_exception=True)
assert isinstance(serializer.validated_object, BookSchema)
assert isinstance(serializer.validated_data["author"], AuthorSchema)
assert "note" not in serializer.validated_data
assert serializer.data == {"title": "Notes", "author": {"name": "Ada"}, "pages": 100}
```

`UNSET` distinguishes absence from explicit null. It is excluded from validated
data in full requests as well as partial ones. The bare `title: str` declaration
does not reject an empty string; add `Annotated` constraints when required.

### Adapt a schema without declaring a serializer subclass

```python
import msgspec
from pydantic import BaseModel

from fastdrf.typed import adapt


class PydanticItem(BaseModel):
    name: str


class MsgspecItem(msgspec.Struct):
    name: str


for schema in (PydanticItem, MsgspecItem):
    Serializer = adapt(schema)
    serializer = Serializer(data=[{"name": "first"}, {"name": "second"}], many=True)
    serializer.is_valid(raise_exception=True)
    assert serializer.data == [{"name": "first"}, {"name": "second"}]
```

`adapt()` returns a serializer class, not an instance. For separate schemas,
use `schema_serializer(InputSchema, OutputSchema, model=Book)` or explicit
`Meta.input_schema`/`Meta.output_schema`. Both schemas must come from one library.
See [model persistence](serializer-examples.md#separate-input-and-output-schemas-with-a-django-model)
and [schema API views](view-examples.md#schemaviewmixin-with-apiview).

## Behavior comparison

| Concern | DRF style with msgspec/Pydantic backend | Native schema wrapped by fastdrf |
| --- | --- | --- |
| Rules | DRF fields, validators, defaults, source mappings | The declared schema library's rules |
| Unsupported optimization | Falls back to DRF by default | Does not fall back to DRF field validation |
| Input coercion | DRF's result, using recognition only where safe | Pydantic configuration or msgspec strictness |
| Errors | DRF field messages/codes | DRF error structure containing native-library messages/codes |
| Unknown keys | DRF's handling | Pydantic `extra` or msgspec `forbid_unknown_fields` policy |
| Output source | Compiled strict output expects eligible model instances | Schema instances, mappings, and compatible attribute objects; nested ORM managers are adapted by fastdrf |
| Existing Pydantic instances | Not a DRF model-output replacement | Represented without revalidation; mappings/ORM objects undergo output validation |
| Nested validated data | Usually dictionaries/model instances as DRF fields produce | Nested BaseModel/Struct instances remain native objects |
| ORM validation | ModelSerializer derives relation and uniqueness validators | `Meta.model` provides writes, not those validators or `full_clean()` |
| Nested writes | Explicit application `create()`/`update()` | Also explicit; a nested schema is not a persistence strategy |
| Partial input | DRF root partial semantics | Safe top-level derivation, otherwise explicit `partial_schema`; supplied nested schema fields retain their own required rules |
| Context | DRF field/serializer hooks | Pydantic callbacks receive context; msgspec hooks follow msgspec's API |
| Output formatting | Strict parity aims at DRF `.data` | Native aliases, tags, exclusions, datetime and Decimal formatting |
| Output shape | DRF serializer/list shape | RootModel, array-like Structs, and model serializers may produce non-object output |
| Settings | Backend, parity, fallback, and field options apply | `SERIALIZER_BACKEND`/fallback do not change schema execution |

Additional boundaries matter when changing contracts:

- `required=False`, nullability, empty strings, and empty collections are separate
  concerns. Do not replace all optional fields with nullable defaults without
  checking the accepted payloads.
- Field names, input aliases, output aliases, and Django attribute names need not
  match. Pydantic can distinguish validation/serialization aliases; msgspec uses
  Struct renames/tags. DRF dotted `source` and `source="*"` have no universal
  direct-schema equivalent.
- Validation hooks/default validation can prevent automatic partial derivation.
  Pydantic RootModel and msgspec `array_like` need an explicit partial contract.
  Partial pre-save output does not support `model_serializer` or array-like output.
- Schema forms use lenient coercion, including when the schema normally runs
  strict. Repeated form keys become lists for collection fields. This is not
  general nested-form parsing.
- Datetimes and Decimal precision/serialization can differ. Do not infer parity
  from matching type names or constraint names.
- `many=True` means list processing, not `bulk_create()`, collection matching,
  deletion rules, or an automatic transaction.
- OpenAPI for schema serializers needs the `fastdrf` app and drf-spectacular
  integration. DRF-style serializers retain ordinary field-based introspection.

## Python data versus JSON transport

For both integrated styles, `.data` is Python data. A view's parser and renderer
are separate choices. Raw `msgspec.json.decode()` or Pydantic
`model_validate_json()` already parses JSON; do not call them on a dictionary
from DRF's `request.data`.

Use `MsgspecJSONParser` and `MsgspecJSONRenderer` to select msgspec HTTP transport,
or use `PydanticJSONParser`/`PydanticJSONRenderer` from `fastdrf.pydantic.parsers`
and `fastdrf.pydantic.renderers`. Keeping DRF transport is also supported.
Both optional renderers have documented differences for bytes,
Decimal, non-finite floats, and other values even with strict serializer parity.
See [rendering](rendering.md) before changing transport independently.

For an existing API, start with [migrating DRF serializers](migrating-from-drf.md).
