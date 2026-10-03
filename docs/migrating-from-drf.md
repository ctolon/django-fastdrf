# Migrating from DRF serializers

There are two migrations with different risks:

1. Keep DRF declarations and the public contract; opt into fastdrf execution.
2. Replace field declarations with native Pydantic/msgspec schemas; review the
   resulting contract changes explicitly.

The first does not require the second. A compiled Pydantic backend does not
require converting your serializer into a BaseModel, and a compiled msgspec
backend does not require a hand-written Struct.

## Route A: retain the DRF contract

### 1. Establish the existing behavior

Keep tests for successful input, rejected input, error codes, omission versus
null, defaults, aliases, nested collections, PATCH, and rendered output. For
model endpoints, include query counts and relationship/uniqueness validation.
Use representative fixtures rather than measuring only an empty list.

An existing declaration might be:

```python
from rest_framework import serializers

from catalog.models import Book


class BookSerializer(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "title", "pages"]
```

`catalog.models.Book` is the application model used in the
[usage examples](serializer-examples.md). Replace that import with your model.

### 2. Change the base import, then select a backend

Install `django-fastdrf[msgspec]` or `django-fastdrf[pydantic]` for the chosen
backend. The dependency-free `python` backend needs only the core package.

```python
from catalog.models import Book
from fastdrf import serializers


class BookSerializer(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "title", "pages"]
        serializer_backend = "msgspec"
```

To use Pydantic execution instead, set `serializer_backend = "pydantic"` on
the same declaration. For python output readers, use `"python"`. Both native
schema libraries are optional; do not import them merely to write DRF-style
fields. Custom base serializers also need to derive from the corresponding
fastdrf base. Existing DRF classes are not monkey-patched.

Start per serializer. Leave strict parity and the DRF parser/renderer unchanged
while evaluating this migration. Backend choice does not replace HTTP transport.
The same serializer classes work in APIView, generic views, and ViewSets; view
optimization mixins are optional, not a prerequisite.

### 3. Check actual eligibility and output

This block is a project test, using the migrated serializer saved in
`catalog.serializers`:

```python
import pytest

from catalog.models import Book
from catalog.serializers import BookRead
from fastdrf.compiler import report_details
from fastdrf.inputs import report_input_details
from fastdrf.testing import assert_compiled_as_drf


@pytest.mark.django_db
def test_book_read_backend():
    # Supply nonempty fixtures in the test database.
    books = Book.objects.select_related("author").prefetch_related("tags")
    assert books.exists()
    assert report_details(BookRead(), backend="msgspec").eligible
    assert_compiled_as_drf(BookRead, books, backends=["msgspec"], queries=True)


input_report = report_input_details(BookRead(data={}), backend="msgspec")
```

Use your own serializer name in that test. The included `BookRead` is the nested
read serializer from the usage guide. `report_input_details()` reports eligibility,
not an input success rate. Input that needs coercion or custom validation can
still go through DRF even when another input uses compiled recognition.

`SERIALIZER_BACKEND_FALLBACK="error"` is useful in tests for unexpected
structural/source declines. It does not disable strict-mode unreadable-value
fallback or input recognition fallback. For a Pydantic backend, use
`backend="pydantic"` and `backends=["pydantic"]` in the checks above.

### 4. Preserve custom behavior and loading

Do not remove `validate()`, relation querysets, permissions, custom fields, or
representation hooks just to improve an eligibility report. Unsupported
behavior normally runs on DRF. `delegate_fields` can retain partial compilation,
but its evaluation order differs: delegated fields run after compiled reads.
Check side effects before enabling it.

Nested serializers still need `select_related()`/`prefetch_related()` or the
query-optimization mixin. A dictionary from `.values()` is not a model instance
for strict compiled output. Introduce field caching, query optimization,
response changes, and JSON transport changes separately so regressions remain
attributable to one decision.

## Route B: adopt a native schema contract

Choose this route when the API is intended to follow Pydantic or msgspec rules,
not merely to execute existing DRF fields faster. Keep the original serializer
while comparing accepted input, error responses, and output from the replacement.

### 1. Write explicit schemas

Pydantic example:

```python
from pydantic import BaseModel, Field

from fastdrf.pydantic.serializers import PydanticSerializer


class BookInput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    pages: int = Field(default=100, ge=1)


class BookOutput(BaseModel):
    id: int
    title: str
    pages: int


class BookSerializer(PydanticSerializer):
    class Meta:
        input_schema = BookInput
        output_schema = BookOutput
```

msgspec equivalent in style, not a promise of identical coercion:

```python
from typing import Annotated

import msgspec

from fastdrf.msgspec.serializers import MsgspecSerializer


class BookInput(msgspec.Struct):
    title: Annotated[str, msgspec.Meta(min_length=1, max_length=100)]
    pages: Annotated[int, msgspec.Meta(ge=1)] = 100


class BookOutput(msgspec.Struct):
    id: int
    title: str
    pages: int


class BookSerializer(MsgspecSerializer):
    class Meta:
        input_schema = BookInput
        output_schema = BookOutput
```

These declarations do not save objects. The application's example Book also
needs an author and ISBN; add complete input and persistence rules rather than
adding `Meta.model` to an incomplete create contract. When output needs generated
fields such as `id`, save or construct a complete output object before accessing
`.data`.

The positive-pages constraint is an explicit new schema rule, not a property
inferred from the earlier DRF declaration. Remove or adjust it if the old API
accepts zero or negative values and that behavior must remain supported.

### 2. Review contract differences explicitly

| Existing DRF behavior | Migration decision |
| --- | --- |
| `CharField` trims whitespace | Configure native string handling or preserve that rule with a validator |
| Integer strings and other coercions | Test native acceptance; msgspec defaults to strict, Pydantic uses model configuration |
| `required=False` without null | Preserve omission separately from explicit null; msgspec offers `UNSET` |
| `source`, read-only/write-only fields | Use deliberate attribute/alias mappings and separate input/output schemas |
| Model uniqueness and relation validation | Reimplement appropriate checks or keep those fields on DRF; `Meta.model` does not derive them |
| DRF validation hooks | Move logic into native validators/hooks or retain serializer-level logic; do not silently drop it |
| Nested PATCH | Specify a patch schema for the required nesting level; native nested schemas keep their own required fields |
| Errors consumed by clients | Check structure, field paths, messages, codes, and first-error versus multiple-error behavior |
| Unknown fields | Choose ignore/forbid/allow intentionally; extras can enter validated data and are not necessarily model columns |
| Datetime/Decimal output | Verify time-zone behavior, precision, scale, and JSON representation |
| ORM output | Load relations explicitly; automatic prefetch cannot inspect schema fields |
| OpenAPI | Enable fastdrf's drf-spectacular integration and compare request/response components |

`validated_data` remains a top-level dictionary, but nested BaseModel/Struct
values remain native objects. Existing code expecting nested dictionaries must
be adapted. Native schema constructors and serializers are not substitutes for
DRF's model validation or transactional nested-write logic.

### 3. Use the converter as a draft, not a contract guarantee

Install the optional `fastdrf` application to use its management commands:

```console
python manage.py fastdrf_convert catalog.serializers.BookRead --to pydantic
python manage.py fastdrf_convert catalog.serializers.BookRead --to msgspec
```

Review every `TODO(convert)` before adopting the output. The converter cannot
infer application validators, nested persistence, relation access policies, or
all source/format semantics. Generated Pydantic optional defaults can widen
nullability; generated aliases can accept names the old serializer did not.
Constraints with matching names can still differ, especially Decimal precision
and regex behavior. See [conversion limits](commands.md#fastdrf_convert).

### 4. Switch the view only after comparison tests pass

DRF generic views can use a `PydanticSerializer`/`MsgspecSerializer` subclass
directly. For a bare BaseModel/Struct as `serializer_class`, add `SchemaViewMixin`
or wrap it with `adapt()`. For APIView, use the schema mixin's body/response
helpers or instantiate the schema serializer explicitly. See
[schema endpoints](view-examples.md#schemaviewmixin-with-apiview).

Input and output schemas on one serializer must use the same library. Do not
replace a serializer and its JSON parser/renderer in the same comparison step:
transport differences are independent of schema differences.

## Before deploying either route

- Exercise valid and invalid real payloads, nested lists, form input if supported,
  PATCH omission, explicit null, and output aliases.
- Check create/update behavior and rollback for multi-row writes. `many=True`
  is neither a bulk-update policy nor an automatic transaction.
- Check nonempty list endpoints for query counts and pagination behavior.
- Compare status, headers, rendered bytes where contractual, and OpenAPI output.
- Measure representative successful and fallback paths, not just synthetic
  canonical input. There is no universal fallback percentage supplied by fastdrf.
- Retain the previous route for rollback. Route A can switch its backend to
  `"drf"`; Route B needs the old serializer/view contract, since a schema
  serializer ignores `SERIALIZER_BACKEND`.

For independent examples of all four declaration choices and raw library APIs,
see [serializer styles](serializer-styles.md).
