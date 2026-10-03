# API views and backend selection

For an import-complete example of each general-purpose DRF view class, see
[DRF view examples by class](drf-view-reference.md).

These examples use the `Author`, `Book`, and `Tag` models and the serializers
from [serializer usage examples](serializer-examples.md). Put those serializer
definitions in `catalog/serializers.py`; the view code below belongs in your
application's views module. The examples remain synchronous, including when
Django runs under ASGI.

Schema and msgspec transport examples require their respective optional extras.
A project using only DRF serializers with the python backend needs neither
schema library; import only the examples applicable to that project.

## APIView: explicit validation and response serialization

An `APIView` does not supply `get_serializer()`, model persistence, pagination,
or `get_object()`. Build the serializer and pass its context explicitly:

```python
from rest_framework.views import APIView

from catalog.models import Book
from catalog.serializers import BookRead, BookWrite
from fastdrf.response import Response
from fastdrf.views import DispatchOptimizationMixin


class CreateBookAPI(DispatchOptimizationMixin, APIView):
    def post(self, request):
        context = {"request": request, "view": self, "format": self.format_kwarg}
        incoming = BookWrite(data=request.data, context=context)
        incoming.is_valid(raise_exception=True)
        book = incoming.save()
        outgoing = BookRead(book, context=context)
        return Response(outgoing.data, status=201)
```

Invalid input becomes a DRF 400 response. The input serializer validates
relation IDs and creates the book; the output serializer returns nested objects.
Using `Response` directly does not serialize a Django model: pass `.data`, not
the model instance. `request.data` has already been parsed by DRF's selected
parser; do not decode the request body again.

Use `transaction.atomic()` around persistence that must succeed as a multi-row
unit. Neither `APIView` nor the optimization mixins make writes atomic by default.

The dispatch mixin is optional and must precede `APIView`. It preserves DRF's
authentication, permission, throttling, and exception handling. Configure those
policies as in any DRF endpoint. For object-level permissions in a manual detail
handler, call `self.check_object_permissions(request, instance)` after lookup;
generic views do that inside `get_object()`.

## Generic views: pagination, creation, and PATCH

Use generic views when the endpoint follows DRF's standard model operations.
They supply serializer context, filtering, object lookup, and persistence hooks.

```python
from rest_framework import generics
from rest_framework.pagination import PageNumberPagination

from catalog.models import Book
from catalog.serializers import BookWrite
from fastdrf.views import DispatchOptimizationMixin, QueryOptimizationMixin


class BookPage(PageNumberPagination):
    page_size = 20


class APIBookSerializer(BookWrite):
    class Meta(BookWrite.Meta):
        auto_prefetch = True
        serializer_backend = "python"


class BookCollectionAPI(
    DispatchOptimizationMixin, QueryOptimizationMixin, generics.ListCreateAPIView
):
    queryset = Book.objects.order_by("pk")
    serializer_class = APIBookSerializer
    pagination_class = BookPage


class BookDetailAPI(
    DispatchOptimizationMixin, QueryOptimizationMixin, generics.RetrieveUpdateAPIView
):
    queryset = Book.objects.all()
    serializer_class = APIBookSerializer
```

`GET` on the collection returns DRF's paginated envelope (`count`, `next`,
`previous`, `results`), not a bare list. `POST` returns one created object.
`PATCH` on the detail view passes `partial=True`; `PUT` does not. Missing
relation keys remain unchanged on PATCH, while `tag_ids: []` clears the tags.

`QueryOptimizationMixin` reads `auto_prefetch` from the selected serializer and
loads the nested author and tags. It does not replace filters or pagination.
Override `get_queryset()` to select the caller's visible rows; call
`super().get_queryset()` if you want this mixin's loading plan to remain in use.

Add application context without dropping DRF's `request`, `format`, and `view`:

```python
class ContextBookCollectionAPI(BookCollectionAPI):
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["label_prefix"] = "Library"
        return context
```

The serializer and its nested fields share the root context. This hook is also
the place to provide values consumed by Pydantic's `ValidationInfo.context`
when using schema serializers in a generic view.

## ViewSets: action-specific serializers and routers

```python
from rest_framework import viewsets
from rest_framework.decorators import action

from catalog.models import Book
from catalog.serializers import BookRead
from fastdrf.response import Response
from fastdrf.views import DispatchOptimizationMixin, QueryOptimizationMixin


class BookAPI(DispatchOptimizationMixin, QueryOptimizationMixin, viewsets.ModelViewSet):
    queryset = (
        Book.objects.select_related("author").prefetch_related("tags").order_by("pk")
    )
    pagination_class = BookPage

    def get_serializer_class(self):
        if self.action in {"list", "retrieve", "summary"}:
            return BookRead
        return APIBookSerializer

    @action(detail=True, methods=["get"])
    def summary(self, request, pk=None):
        book = self.get_object()
        serializer = self.get_serializer(book)
        return Response(serializer.data)
```

Use `get_object()` in detail actions to retain queryset filtering and object
permissions. Use `self.get_serializer()` to retain context and action-specific
selection. Standard create/update actions return the write serializer's output;
selecting `BookRead` for reads does not automatically reserialize a write
response with it. Here `APIBookSerializer` already declares nested read fields.

If a custom collection action must paginate, call `filter_queryset()`,
`paginate_queryset()`, and `get_paginated_response()` explicitly; the `@action`
decorator does not perform those steps for you.

Wire views and the router in the URL configuration:

```python
from django.urls import include, path
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register("books", BookAPI, basename="book")

urlpatterns = [
    path("manual/books/", CreateBookAPI.as_view(), name="manual-book-create"),
    path("generic/books/", BookCollectionAPI.as_view(), name="generic-book-list"),
    path(
        "generic/books/<int:pk>/", BookDetailAPI.as_view(), name="generic-book-detail"
    ),
    path("", include(router.urls)),
]
```

When the URL configuration is in a separate module, import the view classes
there. A `basename` is supplied explicitly, so route naming does not depend on
the router inferring it from the queryset.

## SchemaViewMixin with APIView

For schema-defined endpoints, `SchemaViewMixin` supplies
`get_validated_body()` and `schema_response()`. These are separate from the
compiled DRF serializer backend setting.

### Pydantic request and response models

```python
from pydantic import BaseModel, Field
from rest_framework.views import APIView

from fastdrf.typed import SchemaViewMixin
from fastdrf.views import DispatchOptimizationMixin


class GreetingInput(BaseModel):
    name: str = Field(min_length=1)


class GreetingOutput(BaseModel):
    greeting: str


class GreetingAPI(SchemaViewMixin, DispatchOptimizationMixin, APIView):
    input_schema = GreetingInput
    output_schema = GreetingOutput

    def post(self, request):
        body = self.get_validated_body()
        return self.schema_response({"greeting": f"Hello, {body.name}"})
```

`body` is a `GreetingInput`, not a dictionary. Invalid request data raises DRF's
validation exception with Pydantic's error codes. `schema_response()` validates
or represents the value through the output schema before creating a response.
An invalid output is an application error, not invalid client input; make sure
the handler satisfies the declared response schema.

### msgspec request and response Structs

```python
from typing import Annotated

import msgspec
from rest_framework.views import APIView

from fastdrf.typed import SchemaViewMixin
from fastdrf.views import DispatchOptimizationMixin


class SumInput(msgspec.Struct):
    values: Annotated[list[int], msgspec.Meta(min_length=1)]


class SumOutput(msgspec.Struct):
    total: int


class SumAPI(SchemaViewMixin, DispatchOptimizationMixin, APIView):
    input_schema = SumInput
    output_schema = SumOutput

    def post(self, request):
        body = self.get_validated_body()
        return self.schema_response(SumOutput(total=sum(body.values)))
```

The request must contain at least one integer. The default strict mode rejects
`{"values": ["1"]}`; it does not apply DRF's integer-string coercion. A msgspec
input schema and a Pydantic output schema cannot be paired on the same view.

For manual PATCH handling, `get_validated_body(partial=True)` returns the partial
schema instance. Preserve omitted fields when translating it into writes:
Pydantic supports `model_dump(exclude_unset=True)`; msgspec schemas may carry
`UNSET`. Do not blindly assign every attribute of a partial schema instance.
Using the serializer's `validated_data` avoids exposing omitted top-level fields.

### Bare schemas in generic views

```python
from rest_framework import generics

from catalog.models import Book
from catalog.serializers import BookOutput
from fastdrf.typed import SchemaViewMixin


class SchemaBookListAPI(SchemaViewMixin, generics.ListAPIView):
    queryset = Book.objects.select_related("author").order_by("pk")
    serializer_class = BookOutput
    pagination_class = BookPage
```

The mixin adapts the bare Pydantic model into a serializer. The same syntax works
with a msgspec Struct. DRF generic views without `SchemaViewMixin` require an
actual serializer class, such as `adapt(BookOutput)`, rather than a bare schema.
Load relations explicitly: automatic loading cannot inspect schema fields.

In a manual list handler, `self.schema_response(rows, many=True)` represents the
collection but does not paginate it. Use the generic list view above for a
standard paginated endpoint. An output schema alone also becomes the input
schema; that is suitable here because `ListAPIView` has no write action.

For writable generic views, set `input_schema` and `output_schema` instead.
Persistence uses the model of the view's declared `queryset`; a `get_queryset()`
override does not change the write model. Schema validation does not infer Django
uniqueness, relationship access, or model validation rules. Add those rules before
using automatic persistence, or provide an explicit serializer `create()`/`update()`.

## Choosing the backend: three independent decisions

| Layer | Selection | What it changes |
| --- | --- | --- |
| DRF serializer execution | `FASTDRF["SERIALIZER_BACKEND"]` or `Meta.serializer_backend` | How DRF-defined fields produce output and whether input recognition is attempted |
| Schema contract | `PydanticSerializer`, `MsgspecSerializer`, or `SchemaViewMixin` | Validation and representation rules of the API itself |
| JSON transport | DRF `parser_classes` and `renderer_classes` | How bytes become input data and output data becomes bytes |

Selecting `"pydantic"` for a DRF serializer does not make it a Pydantic schema
serializer. Selecting `"msgspec"` does not select the msgspec parser or renderer.

### python: compiled output without an optional schema dependency

```python
from catalog.serializers import BookRead


class PythonBookSerializer(BookRead):
    class Meta(BookRead.Meta):
        serializer_backend = "python"
```

The python backend compiles output readers for eligible DRF fields. It needs
neither msgspec nor Pydantic, and input validation stays on DRF. Strict/fast
parity, unsupported-field fallback, and optional delegation still apply. It is
not a schema library and there is no `PythonSerializer` schema equivalent.

### msgspec and pydantic as compiled DRF backends

```python
from catalog.serializers import BookRead


class MsgspecBookSerializer(BookRead):
    class Meta(BookRead.Meta):
        serializer_backend = "msgspec"


class PydanticBookSerializer(BookRead):
    class Meta(BookRead.Meta):
        serializer_backend = "pydantic"
```

Both preserve DRF's field contract in the default strict parity mode. Both can
recognize a conservative subset of already-canonical input; coercions, custom
validation, and unsupported input remain DRF's. Installing Pydantic does not
cause DRF's `CharField` to adopt Pydantic's string rules, for example.

The backend can also be selected project-wide. This is a Django settings
fragment, not a per-request switch:

```python
FASTDRF = {
    "SERIALIZER_BACKEND": "python",
    "SERIALIZER_BACKEND_PARITY": "strict",
    "SERIALIZER_BACKEND_FALLBACK": "drf",
}
```

`Meta.serializer_backend` overrides the project choice for a serializer.
`"fast"` parity relaxes the output contract; it is not a universally safe speed
switch. Review [the documented differences](serializers.md#parity), especially
Decimal formatting and backend errors, before enabling it.

### Features specific to schema serializers

| Concern | Pydantic schema serializer | msgspec schema serializer |
| --- | --- | --- |
| Supported schema | Pydantic 2 `BaseModel`; not `pydantic.v1.BaseModel` | `msgspec.Struct` |
| Default coercion | Model configuration; override with `Meta.strict` | Strict by default; override with `Meta.strict` |
| Naming | Input aliases and output aliases may differ | Struct `rename` and tags govern wire names |
| Callbacks | Field/model validators and serializers receive serializer context | `__post_init__` and msgspec hooks follow msgspec's API; there is no Pydantic-style `ValidationInfo.context` |
| Omitted patch fields | Supplied-field tracking; omitted top-level values stay out of validated data | `UNSET` stays out of validated data, on full requests too |
| Validation errors | Multiple errors with Pydantic codes | First error, generally `invalid` or `required` |
| Custom types | Pydantic schema/validation/serialization support | `Meta.dec_hook`, `enc_hook`, `schema_hook`, or registered msgspec types |
| Partial derivation limits | Validators, initialization hooks, default validation, and RootModel require an explicit patch contract | `__post_init__` and `array_like` require an explicit patch contract |

These features belong to schema serializers, not to generated classes hidden
inside a compiled DRF serializer. `Meta.strict` is a schema validation option;
`SERIALIZER_BACKEND_PARITY` controls compiled DRF output. Neither is a substitute
for the other. See [schema examples](serializer-examples.md#pydantic-nested-schema-validation-and-aliases)
for nested schemas, tagged unions, aliases, and explicit patch schemas.

### Parser and renderer selection per endpoint

```python
from fastdrf.msgspec.parsers import MsgspecJSONParser
from fastdrf.msgspec.renderers import MsgspecJSONRenderer


class MsgspecTransportSumAPI(SumAPI):
    parser_classes = [MsgspecJSONParser]
    renderer_classes = [MsgspecJSONRenderer]
```

This endpoint accepts JSON only and does not offer the browsable API renderer.
Add form/multipart parsers or `BrowsableAPIRenderer` explicitly if needed.
Parser choice does not replace serializer validation. msgspec's parser rejects
some JSON values DRF accepts, and its renderer differs for raw Decimal, bytes,
non-finite floats, and other values; see [transport differences](rendering.md).
Pydantic transport can be selected independently in the same way:

```python
from fastdrf.pydantic.parsers import PydanticJSONParser
from fastdrf.pydantic.renderers import PydanticJSONRenderer


class PydanticTransportGreetingAPI(GreetingAPI):
    parser_classes = [PydanticJSONParser]
    renderer_classes = [PydanticJSONRenderer]
```

The Pydantic parser does not perform schema validation. Any serializer can use
these classes; a Pydantic serializer can also keep DRF or msgspec transport.
Read the [Pydantic transport contract](rendering.md#pydantic-json-renderer-and-parser)
before changing JSON representations, especially raw Decimal and bytes values.

## Response and optimization boundaries

`fastdrf.response.Response` retains DRF's response lifecycle and releases request
back-references on `close()`. `DataResponse` is a different option: it needs
`DataResponseMixin` (included in `DispatchOptimizationMixin`) and, after direct
rendering, does not retain `.data`. See [DataResponse](views.md#dataresponse)
before using it with template-response middleware or tests expecting `.data`.

The optional `fastdrf.mixins.CreateModelMixin` and `UpdateModelMixin` optimize
post-save serializer lookup for framework-only paths. They are not needed for
correct writes and are not included in `DispatchOptimizationMixin`. Custom
`get_serializer_class()`, context, or persistence hooks can disable that shortcut
without disabling the endpoint or ordinary serializer compilation.

`ALLOWED_SERIALIZER_BACKENDS` restricts serializer *kinds* in `SchemaViewMixin`
views: `"drf"`, `"msgspec"`, and `"pydantic"`. A DRF serializer running with
the python backend is still kind `"drf"`; do not add `"python"` to that list.
The setting does not control which renderer or parser a view may use.
