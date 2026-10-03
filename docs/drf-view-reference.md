# DRF view examples by class

This reference covers DRF's general-purpose endpoint API: function views,
`APIView`, `GenericAPIView`, all nine concrete generic views, and all four public
ViewSet classes. It complements [API views and backend selection](view-examples.md).
`ViewSetMixin` is the action-binding implementation, not a standalone endpoint.
Special-purpose endpoints supplied by authentication or schema packages are not
CRUD view bases and are outside this reference.

Each endpoint block includes its framework and project imports. `catalog.models`
and `catalog.serializers` refer to the models and `BookRead`/`BookWrite` definitions
in [serializer usage examples](serializer-examples.md), not modules installed by
fastdrf. Put the following view classes in `catalog/views.py`.

`BookRead` exposes nested author/tag objects; `BookWrite` accepts relation IDs.
The querysets explicitly load those relations. Configure authentication,
permissions, filters, and pagination for your application as usual.

## Function-based view: `@api_view`

```python
from rest_framework.decorators import api_view

from catalog.models import Book
from catalog.serializers import BookRead
from fastdrf.response import Response


@api_view(["GET"])
def book_list(request):
    books = (
        Book.objects.select_related("author").prefetch_related("tags").order_by("pk")
    )
    return Response(BookRead(books, many=True, context={"request": request}).data)
```

The serializer backend works in function views too. A function cannot inherit
dispatch/query mixins; relation loading and pagination are explicit. This example
returns an unpaginated list. Use `ListAPIView` for standard paginated responses.

## `APIView`

```python
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView

from catalog.models import Book
from catalog.serializers import BookRead
from fastdrf.response import Response
from fastdrf.views import DispatchOptimizationMixin


class ManualBookDetail(DispatchOptimizationMixin, APIView):
    def get(self, request, pk):
        books = Book.objects.select_related("author").prefetch_related("tags")
        book = get_object_or_404(books, pk=pk)
        self.check_object_permissions(request, book)
        context = {"request": request, "view": self, "format": self.format_kwarg}
        return Response(BookRead(book, context=context).data)
```

`APIView` dispatches HTTP methods but does not provide generic serializer or
queryset helpers. See the [manual create example](view-examples.md#apiview-explicit-validation-and-response-serialization)
for `is_valid()` and `save()`.

## `GenericAPIView`

```python
from rest_framework.generics import GenericAPIView

from catalog.models import Book
from catalog.serializers import BookRead
from fastdrf.response import Response
from fastdrf.views import DispatchOptimizationMixin


class GenericBookDetail(DispatchOptimizationMixin, GenericAPIView):
    queryset = Book.objects.select_related("author").prefetch_related("tags")
    serializer_class = BookRead

    def get(self, request, *args, **kwargs):
        return Response(self.get_serializer(self.get_object()).data)
```

`get_object()` applies filtering and object permissions. `get_serializer()`
supplies context. `GenericAPIView` alone implements no GET/POST handler; provide
handlers or use the concrete classes below.

## Concrete generic views

| DRF class | Methods | URL kind |
| --- | --- | --- |
| `CreateAPIView` | POST | Collection |
| `ListAPIView` | GET | Collection |
| `RetrieveAPIView` | GET | Detail with lookup |
| `UpdateAPIView` | PUT, PATCH | Detail with lookup |
| `DestroyAPIView` | DELETE | Detail with lookup |
| `ListCreateAPIView` | GET, POST | Collection |
| `RetrieveUpdateAPIView` | GET, PUT, PATCH | Detail with lookup |
| `RetrieveDestroyAPIView` | GET, DELETE | Detail with lookup |
| `RetrieveUpdateDestroyAPIView` | GET, PUT, PATCH, DELETE | Detail with lookup |

DRF also handles OPTIONS and, for GET endpoints, HEAD. The examples use `pk` as
the lookup parameter. Changing `lookup_field` or `lookup_url_kwarg` requires a
matching URL pattern.

### CreateAPIView

```python
from rest_framework.generics import CreateAPIView

from catalog.models import Book
from catalog.serializers import BookWrite
from fastdrf.views import DispatchOptimizationMixin


class BookCreate(DispatchOptimizationMixin, CreateAPIView):
    queryset = Book.objects.all()
    serializer_class = BookWrite
```

### ListAPIView

```python
from rest_framework.generics import ListAPIView

from catalog.models import Book
from catalog.serializers import BookRead
from fastdrf.views import DispatchOptimizationMixin


class BookList(DispatchOptimizationMixin, ListAPIView):
    queryset = (
        Book.objects.select_related("author").prefetch_related("tags").order_by("pk")
    )
    serializer_class = BookRead
```

### RetrieveAPIView

```python
from rest_framework.generics import RetrieveAPIView

from catalog.models import Book
from catalog.serializers import BookRead
from fastdrf.views import DispatchOptimizationMixin


class BookRetrieve(DispatchOptimizationMixin, RetrieveAPIView):
    queryset = Book.objects.select_related("author").prefetch_related("tags")
    serializer_class = BookRead
```

### UpdateAPIView

```python
from rest_framework.generics import UpdateAPIView

from catalog.models import Book
from catalog.serializers import BookWrite
from fastdrf.views import DispatchOptimizationMixin


class BookUpdate(DispatchOptimizationMixin, UpdateAPIView):
    queryset = Book.objects.select_related("author").prefetch_related("tags")
    serializer_class = BookWrite
```

### DestroyAPIView

```python
from rest_framework.generics import DestroyAPIView

from catalog.models import Book
from catalog.serializers import BookRead
from fastdrf.views import DispatchOptimizationMixin


class BookDestroy(DispatchOptimizationMixin, DestroyAPIView):
    queryset = Book.objects.all()
    serializer_class = BookRead
```

Deletion returns 204 with no serialized object. There is no reason to prefetch
output relations for this endpoint.

### ListCreateAPIView

```python
from rest_framework.generics import ListCreateAPIView

from catalog.models import Book
from catalog.serializers import BookWrite
from fastdrf.views import DispatchOptimizationMixin


class BookListCreate(DispatchOptimizationMixin, ListCreateAPIView):
    queryset = (
        Book.objects.select_related("author").prefetch_related("tags").order_by("pk")
    )
    serializer_class = BookWrite
```

### RetrieveUpdateAPIView

```python
from rest_framework.generics import RetrieveUpdateAPIView

from catalog.models import Book
from catalog.serializers import BookWrite
from fastdrf.views import DispatchOptimizationMixin


class BookRetrieveUpdate(DispatchOptimizationMixin, RetrieveUpdateAPIView):
    queryset = Book.objects.select_related("author").prefetch_related("tags")
    serializer_class = BookWrite
```

### RetrieveDestroyAPIView

```python
from rest_framework.generics import RetrieveDestroyAPIView

from catalog.models import Book
from catalog.serializers import BookRead
from fastdrf.views import DispatchOptimizationMixin


class BookRetrieveDestroy(DispatchOptimizationMixin, RetrieveDestroyAPIView):
    queryset = Book.objects.select_related("author").prefetch_related("tags")
    serializer_class = BookRead
```

### RetrieveUpdateDestroyAPIView

```python
from rest_framework.generics import RetrieveUpdateDestroyAPIView

from catalog.models import Book
from catalog.serializers import BookWrite
from fastdrf.views import DispatchOptimizationMixin


class BookRetrieveUpdateDestroy(
    DispatchOptimizationMixin, RetrieveUpdateDestroyAPIView
):
    queryset = Book.objects.select_related("author").prefetch_related("tags")
    serializer_class = BookWrite
```

PATCH allows omitted fields; PUT runs full serializer validation. Both use
`update()`, and neither invents nested-write rules. Create returns 201, successful
update returns 200, and missing detail objects return 404. A class without the
requested handler returns 405. Default list behavior follows the project's
pagination settings; see [explicit pagination configuration](view-examples.md#generic-views-pagination-creation-and-patch).

## Building a generic view from model mixins

```python
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import CreateModelMixin, ListModelMixin

from catalog.models import Book
from catalog.serializers import BookWrite
from fastdrf.views import DispatchOptimizationMixin


class ComposedBookCollection(
    DispatchOptimizationMixin, ListModelMixin, CreateModelMixin, GenericAPIView
):
    queryset = (
        Book.objects.select_related("author").prefetch_related("tags").order_by("pk")
    )
    serializer_class = BookWrite

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.create(request, *args, **kwargs)
```

The other DRF model mixins are `RetrieveModelMixin`, `UpdateModelMixin`, and
`DestroyModelMixin`, imported from `rest_framework.mixins`. Their actions are
`retrieve()`, `update()`/`partial_update()`, and `destroy()`. Concrete generic
views already bind these actions to HTTP methods; composition is useful when
you need a different combination.

## ViewSet

```python
from rest_framework.viewsets import ViewSet

from catalog.models import Book
from catalog.serializers import BookRead
from fastdrf.response import Response
from fastdrf.views import DispatchOptimizationMixin


class ManualBookViewSet(DispatchOptimizationMixin, ViewSet):
    def list(self, request):
        books = (
            Book.objects.select_related("author")
            .prefetch_related("tags")
            .order_by("pk")
        )
        return Response(BookRead(books, many=True, context={"request": request}).data)
```

`ViewSet` uses named actions such as `list` rather than `get`, but has no generic
queryset/serializer helpers. Define only the actions this endpoint supports.

## GenericViewSet

```python
from rest_framework.mixins import CreateModelMixin, ListModelMixin, RetrieveModelMixin
from rest_framework.viewsets import GenericViewSet

from catalog.models import Book
from catalog.serializers import BookWrite
from fastdrf.views import DispatchOptimizationMixin


class SelectedBookViewSet(
    DispatchOptimizationMixin,
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    GenericViewSet,
):
    queryset = (
        Book.objects.select_related("author").prefetch_related("tags").order_by("pk")
    )
    serializer_class = BookWrite
```

`GenericViewSet` supplies generic helpers but no actions by itself. This
composition exposes list, create, and retrieve, but no update or delete action.

## ReadOnlyModelViewSet

```python
from rest_framework.viewsets import ReadOnlyModelViewSet

from catalog.models import Book
from catalog.serializers import BookRead
from fastdrf.views import DispatchOptimizationMixin


class ReadOnlyBookViewSet(DispatchOptimizationMixin, ReadOnlyModelViewSet):
    queryset = (
        Book.objects.select_related("author").prefetch_related("tags").order_by("pk")
    )
    serializer_class = BookRead
```

This class provides `list` and `retrieve`. It has no write actions.

## ModelViewSet

```python
from rest_framework.viewsets import ModelViewSet

from catalog.models import Book
from catalog.serializers import BookWrite
from fastdrf.views import DispatchOptimizationMixin


class FullBookViewSet(DispatchOptimizationMixin, ModelViewSet):
    queryset = (
        Book.objects.select_related("author").prefetch_related("tags").order_by("pk")
    )
    serializer_class = BookWrite
```

This class provides `list`, `create`, `retrieve`, `update`, `partial_update`, and
`destroy`. For action-specific serializers and `@action`, see
[the ViewSet guide](view-examples.md#viewsets-action-specific-serializers-and-routers).

## URL imports and action binding

Place this block in `catalog/urls.py`, after saving the view definitions above
in `catalog/views.py`:

```python
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from catalog.views import (
    BookCreate,
    BookDestroy,
    BookList,
    BookListCreate,
    BookRetrieve,
    BookRetrieveDestroy,
    BookRetrieveUpdate,
    BookRetrieveUpdateDestroy,
    BookUpdate,
    ComposedBookCollection,
    FullBookViewSet,
    GenericBookDetail,
    ManualBookDetail,
    ManualBookViewSet,
    ReadOnlyBookViewSet,
    SelectedBookViewSet,
    book_list,
)

router = DefaultRouter()
router.register("manual", ManualBookViewSet, basename="manual-book")
router.register("selected", SelectedBookViewSet, basename="selected-book")
router.register("readonly", ReadOnlyBookViewSet, basename="readonly-book")
router.register("full", FullBookViewSet, basename="full-book")

urlpatterns = [
    path("function/", book_list),
    path("api/<int:pk>/", ManualBookDetail.as_view()),
    path("generic/<int:pk>/", GenericBookDetail.as_view()),
    path("create/", BookCreate.as_view()),
    path("list/", BookList.as_view()),
    path("retrieve/<int:pk>/", BookRetrieve.as_view()),
    path("update/<int:pk>/", BookUpdate.as_view()),
    path("destroy/<int:pk>/", BookDestroy.as_view()),
    path("list-create/", BookListCreate.as_view()),
    path("retrieve-update/<int:pk>/", BookRetrieveUpdate.as_view()),
    path("retrieve-destroy/<int:pk>/", BookRetrieveDestroy.as_view()),
    path("retrieve-update-destroy/<int:pk>/", BookRetrieveUpdateDestroy.as_view()),
    path("composed/", ComposedBookCollection.as_view()),
    path("viewsets/", include(router.urls)),
]
```

Alternatively, bind actions without a router. A ViewSet requires an action map;
unlike an APIView, it is not wired with a bare `.as_view()`:

```python
from django.urls import path

from catalog.views import FullBookViewSet

manual_patterns = [
    path("books/", FullBookViewSet.as_view({"get": "list", "post": "create"})),
    path(
        "books/<int:pk>/",
        FullBookViewSet.as_view(
            {
                "get": "retrieve",
                "put": "update",
                "patch": "partial_update",
                "delete": "destroy",
            }
        ),
    ),
]
```

## DRF declarations with a msgspec execution backend

This is supported without writing a Struct:

```python
from catalog.models import Book
from fastdrf import serializers


class CompiledBookSerializer(serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "title", "pages"]
        serializer_backend = "msgspec"


incoming = CompiledBookSerializer(data={"title": "Notes", "pages": 12})
incoming.is_valid(raise_exception=True)
assert incoming.validated_data == {"title": "Notes", "pages": 12}

book = Book(id=1, title="Notes", pages=12)
assert CompiledBookSerializer(book).data == {"id": 1, "title": "Notes", "pages": 12}
```

Use fastdrf's serializer bases, not unchanged `rest_framework.serializers`
bases: fastdrf does not monkey-patch DRF. Its fields retain DRF syntax. The
msgspec extra must be installed. The example validates without saving because
its input deliberately does not define all fields needed to create a `Book`.

Output uses a generated Struct when the serializer and source are eligible.
Input uses msgspec recognition for supported canonical values; custom validators,
relations, coercions such as an integer supplied as a string, and unsupported
cases are validated by DRF instead. This is not unconditional replacement of
DRF's deserialization rules. Plain `Serializer` declarations can use input
recognition too, but strict compiled output normally requires a model-backed
source; see [eligibility](serializers.md#output-backends).

`.data` is Python data, not JSON bytes. To parse/render HTTP JSON with msgspec
as well, select its parser and renderer independently:

```python
from rest_framework.views import APIView

from fastdrf.msgspec.parsers import MsgspecJSONParser
from fastdrf.msgspec.renderers import MsgspecJSONRenderer
from fastdrf.response import Response


class CompiledBookEcho(APIView):
    parser_classes = [MsgspecJSONParser]
    renderer_classes = [MsgspecJSONRenderer]

    def post(self, request):
        serializer = CompiledBookSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data)
```

Keep this last class alongside `CompiledBookSerializer`, or import that class
from its application module. Parser/renderer differences still apply; strict
serializer parity does not guarantee identical JSON bytes for every value.
