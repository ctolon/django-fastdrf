# Serializer usage examples

For HTTP endpoints using these serializers, continue with
[API views and backend selection](view-examples.md): APIView, generic views,
ViewSet actions, schema endpoints, and backend-specific behavior.
For independent declaration examples and a behavior comparison, see
[serializer styles](serializer-styles.md); for an existing API, see
[migrating from DRF](migrating-from-drf.md).

This guide covers nested reads and writes, collections, partial updates, schema
serializers, and the boundaries between them. The examples use a synchronous
Django application with DRF configured. Install the `msgspec` and `pydantic`
extras to run the schema examples.

Choose the serializer contract before choosing a backend:

| Requirement | Use |
| --- | --- |
| Keep an existing DRF API's validation and representation | `fastdrf.serializers`, optionally with a compiled backend in strict parity |
| Define the API with Pydantic models or msgspec Structs | `PydanticSerializer`, `MsgspecSerializer`, or `adapt()` |
| Reduce queries for nested model output | Explicit Django relation loading, or `QueryOptimizationMixin` for DRF-defined fields |
| Write nested objects or update a collection | Explicit persistence code; neither compilation nor schema adaptation defines these operations |

Examples build on earlier definitions. The model examples assume
`catalog.models` provides these Django models:

- `Author`: a `name` string field.
- `Tag`: a unique `name` string field.
- `Book`: `title`, unique `isbn`, `pages` (default 100), an `author` foreign key
  with `related_name="books"`, and a `tags` many-to-many relation.

Use your own model imports and migrations. The repository executes these code
blocks against equivalent test models in `tests/test_docs_serializer_examples.py`.

## Nested model output

Declare each relation explicitly. A nested serializer describes one object;
`many=True` describes a collection of those objects.

```python
from catalog.models import Author, Book, Tag

from fastdrf import serializers


class AuthorSummary(serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


class TagSummary(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name"]


class BookRead(serializers.ModelSerializer):
    author = AuthorSummary(read_only=True)
    tags = TagSummary(many=True, read_only=True)

    class Meta:
        model = Book
        fields = ["id", "title", "pages", "author", "tags"]
        serializer_backend = "msgspec"


books = Book.objects.select_related("author").prefetch_related("tags")
data = BookRead(books, many=True).data
```

`many=True` does not load relations or paginate the queryset. Here
`select_related` loads the single author and `prefetch_related` loads tags for
the whole result set. The compiled serializer still uses the renderer selected
by DRF; choosing the msgspec serializer backend does not select a JSON renderer.

The same pattern works for reverse relations and additional levels:

```python
from catalog.models import Author, Book
from fastdrf import serializers


class BookSummary(serializers.ModelSerializer):
    tags = TagSummary(many=True, read_only=True)

    class Meta:
        model = Book
        fields = ["id", "title", "tags"]


class AuthorDetail(serializers.ModelSerializer):
    books = BookSummary(many=True, read_only=True)

    class Meta:
        model = Author
        fields = ["id", "name", "books"]


authors = Author.objects.prefetch_related("books__tags")
data = AuthorDetail(authors, many=True).data
```

Keep the response graph finite. Do not make `BookRead.author` contain
`AuthorDetail.books`, which contains the full author again. Use a summary
serializer, identifiers, or an explicit depth limit for recursive domains.

### Automatic loading in views

```python
from rest_framework import viewsets

from catalog.models import Book
from fastdrf.views import QueryOptimizationMixin


class LoadedBookRead(BookRead):
    class Meta(BookRead.Meta):
        auto_prefetch = True


class BookViewSet(QueryOptimizationMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Book.objects.all()
    serializer_class = LoadedBookRead
```

The mixin must precede the DRF view class. It derives loading paths from DRF
fields; it does not inspect schema fields or discover relations accessed inside
arbitrary methods. Add `Meta.prefetch` for those paths, or load them explicitly.
Loading is not authorization: keep tenant or user filtering in the queryset.
See [queries](queries.md) for filtered `Prefetch` objects, deferred columns,
batched relation validation, and per-list enrichment.

## Read nested objects, write relation identifiers

Use separate read and write fields when clients should see an object but submit
its identifier. `source` names the model attribute, not the request key.

```python
from catalog.models import Author, Book, Tag
from fastdrf import serializers


class BookWrite(serializers.ModelSerializer):
    author = AuthorSummary(read_only=True)
    author_id = serializers.PrimaryKeyRelatedField(
        source="author", queryset=Author.objects.all(), write_only=True
    )
    tags = TagSummary(many=True, read_only=True)
    tag_ids = serializers.PrimaryKeyRelatedField(
        source="tags",
        queryset=Tag.objects.all(),
        many=True,
        write_only=True,
        required=False,
    )

    class Meta:
        model = Book
        fields = ["id", "title", "isbn", "author", "author_id", "tags", "tag_ids"]


author = Author.objects.create(name="Ada")
tag = Tag.objects.create(name="Django")
serializer = BookWrite(
    data={
        "title": "Practical APIs",
        "isbn": "9780000000001",
        "author_id": author.pk,
        "tag_ids": [tag.pk],
    }
)
serializer.is_valid(raise_exception=True)
book = serializer.save()
assert serializer.validated_data["author"] == author
```

`PrimaryKeyRelatedField` validates against its queryset and returns model
instances, not integer IDs. Scope that queryset to the objects the caller may
select. Model-generated uniqueness validation and relation validation remain
DRF's; compiled input recognition does not bypass them.

For an update, omitted `tag_ids` leaves the relation unchanged; an explicit
empty list clears it. These are different from `null`:

```python
patch = BookWrite(book, data={"tag_ids": []}, partial=True)
patch.is_valid(raise_exception=True)
patch.save()
assert not book.tags.exists()
```

## Writable nested objects

Nested validation does not define nested persistence. Write `create()` or
`update()` for the operation your API promises. This example creates a new
author with a new book; it does not look up, merge, or update an existing author.

```python
from django.db import transaction

from catalog.models import Author, Book
from fastdrf import serializers


class NewAuthor(serializers.Serializer):
    name = serializers.CharField(max_length=100)


class NewBook(serializers.ModelSerializer):
    author = NewAuthor()

    class Meta:
        model = Book
        fields = ["title", "isbn", "author"]

    @transaction.atomic
    def create(self, validated_data):
        author_data = validated_data.pop("author")
        author = Author.objects.create(**author_data)
        return Book.objects.create(author=author, **validated_data)


serializer = NewBook(
    data={"title": "Nested writes", "isbn": "9780000000002", "author": {"name": "Lin"}}
)
serializer.is_valid(raise_exception=True)
created = serializer.save()
```

For nested updates, decide which identifier selects a child, whether omitted
children are preserved or deleted, and how conflicts are handled. Implement
those rules in `update()` and make multi-row writes atomic. The example above
is create-only: passing an existing book does not add nested update support.
`many=True` likewise does not supply bulk-update matching or deletion rules.

## Missing, null, empty, and partial input

These options apply to different cases:

| Input | DRF field option |
| --- | --- |
| Key omitted | `required=False`, a default, or a partial request |
| Explicit `null` | `allow_null=True` |
| Empty string | `allow_blank=True` on string fields |
| Empty list | `allow_empty=True` on collection fields |

```python
from fastdrf import serializers


class Contact(serializers.Serializer):
    name = serializers.CharField()
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class ContactEnvelope(serializers.Serializer):
    contact = Contact()
    contacts = Contact(many=True, required=False, allow_empty=True)


patch = ContactEnvelope(data={"contact": {"note": None}}, partial=True)
patch.is_valid(raise_exception=True)
assert patch.validated_data == {"contact": {"note": None}}

invalid = ContactEnvelope(data={"contact": {"note": None}})
assert not invalid.is_valid()
assert "name" in invalid.errors["contact"]
```

For DRF-defined nested serializers, the root's `partial=True` allows missing
child fields too. Defaults are not injected for omitted fields during a partial
update. Partial validation does not implement nested saving, and it does not
make an explicitly supplied invalid value valid.

### Lists and dictionaries of nested objects

```python
from fastdrf import serializers


class Directory(serializers.Serializer):
    ordered = serializers.ListField(child=Contact(), required=False)
    by_name = serializers.DictField(child=Contact(), required=False)


directory = Directory(
    data={
        "ordered": [{"name": "Ada"}],
        "by_name": {"maintainer": {"name": "Lin"}},
    }
)
directory.is_valid(raise_exception=True)

batch = Contact(data=[{"name": "Ada"}, {"name": "Lin"}], many=True)
batch.is_valid(raise_exception=True)
assert len(batch.validated_data) == 2
```

Top-level `many=True` creates a list serializer. `ListField(child=...)` and
`DictField(child=...)` are fields inside an object. Their error shapes are not
interchangeable. Collection nullability and item nullability are separate;
place `allow_null=True` on the field whose value may actually be null.
Validation of a batch is not an automatic database transaction.
The default list `create()` calls each child's `create()`; `many=True` is not
an instruction to use Django's `bulk_create()`.

With `Contact(many=True, allow_null=True)`, DRF passes the null option to both
the list and its child. Use explicit containers when those rules differ:

```python
from fastdrf import serializers


class NullableCollections(serializers.Serializer):
    nullable_list = serializers.ListSerializer(child=Contact(), allow_null=True)
    nullable_items = serializers.ListField(child=Contact(allow_null=True))


serializer = NullableCollections(
    data={"nullable_list": None, "nullable_items": [None, {"name": "Ada"}]}
)
serializer.is_valid(raise_exception=True)
```

Here `nullable_list` accepts null but rejects null items, while `nullable_items`
accepts null items but rejects a null list.

## Pydantic: nested schema validation and aliases

```python
from pydantic import BaseModel, Field

from fastdrf.typed import adapt


class Person(BaseModel):
    name: str


class Document(BaseModel):
    title: str = Field(validation_alias="displayTitle", serialization_alias="title")
    owner: Person
    reviewers: list[Person] = Field(default_factory=list)


DocumentSerializer = adapt(Document)
serializer = DocumentSerializer(
    data={"displayTitle": "Notes", "owner": {"name": "Ada"}}
)
serializer.is_valid(raise_exception=True)
assert serializer.validated_data["title"] == "Notes"
assert isinstance(serializer.validated_data["owner"], Person)
assert serializer.data == {"title": "Notes", "owner": {"name": "Ada"}, "reviewers": []}
```

The input alias, Python attribute, and output alias have separate roles.
`validated_data` uses Python attribute names; nested values remain schema
instances rather than recursively becoming DRF dictionaries.
`validated_object` is the full input schema instance. Pydantic validation,
serialization, exclusion rules, and error codes apply, not DRF field coercions.

`partial=True` does not recursively turn every nested Pydantic model into a
partial model. A supplied `owner` still follows `Person`'s schema. Define a
nested patch schema if the nested object's own fields must be optional.

This differs from embedding an adapted serializer as a DRF field. In that case
the root partial flag reaches the child serializer, which selects its partial
schema:

```python
from fastdrf import serializers
from fastdrf.typed import adapt


class PersonEnvelope(serializers.Serializer):
    person = adapt(Person)()


patch = PersonEnvelope(data={"person": {}}, partial=True)
patch.is_valid(raise_exception=True)
assert patch.validated_data == {"person": {}}
```

Both combinations validate nested data, but neither implements nested database
updates. Keep the distinction between a nested schema field (`owner: Person`)
and a nested DRF serializer field (`person = adapt(Person)()`) explicit.

### Validation context and an explicit patch schema

```python
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from fastdrf.pydantic.serializers import PydanticSerializer


class PageCount(BaseModel):
    pages: int = Field(default=100, gt=0)

    @field_validator("pages")
    @classmethod
    def within_limit(cls, value: int, info: ValidationInfo) -> int:
        limit = (info.context or {}).get("max_pages", 1000)
        if value > limit:
            raise ValueError("Page count exceeds the allowed limit")
        return value


class PageCountSerializer(PydanticSerializer):
    class Meta:
        schema = PageCount
        partial_schema = PageCount


patch = PageCountSerializer(data={}, partial=True, context={"max_pages": 200})
patch.is_valid(raise_exception=True)
assert patch.validated_data == {}

invalid = PageCountSerializer(data={"pages": 201}, context={"max_pages": 200})
assert not invalid.is_valid()
```

This patch schema explicitly makes `pages` omittable through its default while
still rejecting `null` and validating supplied values. Partial validated data
contains supplied fields only. It does not write `100` when the key is absent.
The same class works for both operations here because their field rules match;
use a separate class when they do not.

Do not assume automatic partial derivation will preserve validators or
initialization hooks. With those features, set `Meta.partial_schema` explicitly.
Context is also passed to Pydantic serialization callbacks, including bulk output.

## msgspec: nested schemas and omitted fields

```python
import msgspec

from fastdrf.msgspec.serializers import MsgspecSerializer


class StructPerson(msgspec.Struct):
    name: str


class StructDocument(msgspec.Struct, rename="camel"):
    display_title: str
    owner: StructPerson
    reviewers: list[StructPerson] = msgspec.field(default_factory=list)
    note: str | None | msgspec.UnsetType = msgspec.UNSET


class StructDocumentSerializer(MsgspecSerializer):
    class Meta:
        schema = StructDocument


serializer = StructDocumentSerializer(
    data={"displayTitle": "Notes", "owner": {"name": "Ada"}}
)
serializer.is_valid(raise_exception=True)
assert isinstance(serializer.validated_data["owner"], StructPerson)
assert "note" not in serializer.validated_data
assert serializer.data["displayTitle"] == "Notes"
```

`UNSET` means omitted; `None` means explicit null. An `UNSET` field is excluded
from validated data on both full and partial requests. Wire renaming does not
rename Python attributes. msgspec input is strict by default; `Meta.strict =
False` allows the conversions msgspec supports, not all DRF conversions.

As with Pydantic, nested Struct fields keep their own required-field rules.
If a Struct has `__post_init__` or is `array_like`, provide an explicit
`partial_schema`; automatic derivation would lose its validation or positional
meaning. Use object-shaped schemas for field-level PATCH APIs.

### Tagged alternatives

Use a discriminator when a nested object has several distinct shapes. Pydantic
declares it on the union field; msgspec declares tags on the Struct classes:

```python
from typing import Literal

import msgspec
from pydantic import BaseModel, Field

from fastdrf.typed import adapt


class EmailTarget(BaseModel):
    kind: Literal["email"]
    address: str


class QueueTarget(BaseModel):
    kind: Literal["queue"]
    name: str


class Delivery(BaseModel):
    target: EmailTarget | QueueTarget = Field(discriminator="kind")


class StructEmailTarget(msgspec.Struct, tag="email", tag_field="kind"):
    address: str


class StructQueueTarget(msgspec.Struct, tag="queue", tag_field="kind"):
    name: str


class StructDelivery(msgspec.Struct):
    target: StructEmailTarget | StructQueueTarget


payload = {"target": {"kind": "queue", "name": "reports"}}
for schema in (Delivery, StructDelivery):
    serializer = adapt(schema)(data=payload)
    serializer.is_valid(raise_exception=True)
    assert serializer.data == payload
```

The tag is part of the wire contract. A missing or unknown tag is a validation
error, not a request to infer a default variant. Native library rules govern
unions; do not assume an untagged Pydantic union can be represented by the same
msgspec annotation. `address: str` above checks the type, not email syntax.

## Separate input and output schemas with a Django model

```python
from pydantic import BaseModel

from catalog.models import Book
from fastdrf.pydantic.serializers import PydanticSerializer


class BookInput(BaseModel):
    title: str
    isbn: str
    author_id: int


class BookOutput(BaseModel):
    id: int
    title: str
    author: Person


class SchemaBookSerializer(PydanticSerializer):
    class Meta:
        input_schema = BookInput
        output_schema = BookOutput
        model = Book


serializer = SchemaBookSerializer(
    data={"title": "Schema APIs", "isbn": "9780000000003", "author_id": author.pk}
)
serializer.is_valid(raise_exception=True)
saved = serializer.save()
assert serializer.data["id"] == saved.pk
assert serializer.data["author"] == {"name": "Ada"}

books = Book.objects.select_related("author")
data = SchemaBookSerializer(books, many=True).data
```

Save before reading `.data` when the output schema needs generated IDs or
database relations absent from input. As in DRF, reading `.data` first prevents
a subsequent `.save()` on that serializer instance.

`Meta.model` supplies model persistence, not model-derived validation. This
`author_id` is an integer field, not a `PrimaryKeyRelatedField`: validate existence
and caller access yourself. Likewise, add input constraints and uniqueness
handling deliberately; Django `save()` does not call `full_clean()` for you.
Schema serializers do not implement nested writes, and multi-row relation writes
are not automatically wrapped in a transaction.

Nested ORM output reads related managers as collections. Load them explicitly:
`Meta.auto_prefetch` cannot derive paths from schema fields. Existing Pydantic
model instances are represented without revalidation; mappings and ORM objects
still go through output validation.

### A generic view using schemas

```python
from rest_framework import viewsets

from catalog.models import Book
from fastdrf.typed import SchemaViewMixin


class SchemaBookViewSet(SchemaViewMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Book.objects.select_related("author")
    output_schema = BookOutput
```

Place `SchemaViewMixin` first. An output schema alone supplies both directions,
which is harmless for this read-only view. For writable views, declare the
intended input schema as well. See [schema serializers](schema-serializers.md)
for `get_validated_body()`, `schema_response()`, `ALLOWED_SERIALIZER_BACKENDS`,
and OpenAPI setup. Input and output schemas must use the same schema library.

## Forms, errors, and output shape

```python
from django.http import QueryDict
from pydantic import BaseModel

from fastdrf.typed import adapt


class SearchInput(BaseModel):
    tags: list[str]
    limit: int


search = adapt(SearchInput)(data=QueryDict("tags=django&tags=api&limit=10"))
search.is_valid(raise_exception=True)
assert search.validated_data == {"tags": ["django", "api"], "limit": 10}
```

Schema form input uses lenient coercion and keeps repeated values for collection
fields; scalar fields get the last value. This is not a general parser for
arbitrary nested form structures. Prefer JSON for nested objects. DRF-defined
serializers retain DRF's own form parsing instead of schema form behavior.

Use `is_valid(raise_exception=True)` in views to obtain DRF error responses.
Pydantic can report multiple errors; msgspec stops at its first error. Do not
assume identical messages or codes across schema libraries. `RootModel`,
`array_like` Structs, and custom Pydantic model serializers can produce a scalar
or array rather than an object. Partial pre-save output with an `array_like`
Struct or a Pydantic `model_serializer` is not supported.

## Verify the path your endpoint uses

```python
from fastdrf.compiler import report_details
from fastdrf.inputs import report_input_details

output = report_details(BookRead(), backend="msgspec")
input_ = report_input_details(BookWrite(data={}), backend="msgspec")
assert output.eligible
assert not input_.eligible  # Relations and model-derived validators need DRF.
```

Eligibility is structural, not a runtime fallback rate. A compiled output can
still return to DRF for a particular unreadable value in strict parity. Passing
dicts from `.values()` instead of model instances also changes the eligible
source path. `serializer_backend_fallback="error"` exposes structural/source
declines; it does not disable strict-parity unreadable-value fallback.

`SerializerMethodField` and custom fields normally prevent whole-serializer
compilation. Opt into `Meta.delegate_fields = True` only when their execution
order is suitable: delegated fields run after compiled reads. Context-sensitive
methods can use `self.context`, but methods that mutate values needed by other
fields should not rely on DRF's field-by-field evaluation order under delegation.

```python
from fastdrf import serializers


class LabeledBook(BookRead):
    label = serializers.SerializerMethodField()

    class Meta(BookRead.Meta):
        fields = [*BookRead.Meta.fields, "label"]
        delegate_fields = True

    def get_label(self, book):
        prefix = self.context.get("label_prefix", "Book")
        return f"{prefix}: {book.title}"


data = LabeledBook(book, context={"label_prefix": "Selected"}).data
assert data["label"] == "Selected: Practical APIs"
```

The label uses request-specific context without storing it on the serializer
class or changing the model. DRF validation hooks such as `validate()` and
`validate_<field>()` remain supported; they keep input validation on DRF even
when output compilation is eligible.

For endpoint tests, use `fastdrf.testing.assert_compiled_as_drf()` with representative
model instances and `queries=True` when query-count parity matters. Output
signals distinguish compiled output and DRF fallback, but do not measure input
recognition or renderer fallback. See [serializers](serializers.md#checking-eligibility)
and [extension testing](extending.md#testing).

## Related configuration

- [Configuration](configuration.md): installation, backend selection, cache and
  copy modes, per-view overrides, and system checks.
- [Queries](queries.md): filtered prefetches, deferred fields, `FETCH_MODE`, and
  batched relationship lookups.
- [Views and responses](views.md): dispatch mixins, response lifetime, and
  direct data responses.
- [Rendering](rendering.md): JSON renderer selection and documented msgspec
  representation differences. Serializer parity does not imply renderer parity.
- [Extensions](extending.md): custom fields and registered msgspec types.
- [Conversion](commands.md#fastdrf_convert): generated source and explicit
  conversion limits; review its TODO comments before using generated schemas.
