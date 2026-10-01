"""
The same requests to DRF's viewsets and to viewsets with every fastdrf
opt-in, compared byte for byte.

Each request runs against DRF's ``ModelViewSet`` with DRF's serializers, and
against viewsets with fastdrf's mixins (``DispatchOptimizationMixin``,
``QueryOptimizationMixin``, the compiled write mixins), its kept-encoder
``JSONRenderer``, with fastdrf's serializers and with DRF's, and once more
answering reads with ``DataResponse``. Everything observable is compared:
status, headers, the body's bytes (or the exception a view raises), and the
database afterwards. The reference is DRF itself. Each profile is a set of
``FASTDRF`` options; the compiled profiles assert that compiled encoders
produced output, so that the test cannot stop covering them unnoticed.

``nox -s differential`` runs this module alone across the supported matrix.
"""

import datetime
import uuid

import django_filters
import pytest
from django.db import transaction
from django.test import override_settings
from django.urls import include, path
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, pagination, renderers, routers
from rest_framework import serializers as drf_serializers
from rest_framework import viewsets as drf_viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.test import APIClient

from fastdrf import compiler
from fastdrf import serializers as fastdrf_serializers
from fastdrf.mixins import CreateModelMixin, UpdateModelMixin
from fastdrf.renderers import JSONRenderer
from fastdrf.response import DataResponse
from fastdrf.views import DispatchOptimizationMixin, QueryOptimizationMixin
from tests.models import Author, Book, Edition, Tag

PROFILES = {
    "default": {},
    "msgspec": {
        "SERIALIZER_BACKEND": "msgspec",
        "CACHE_SERIALIZER_FIELDS": True,
        "FIELD_COPY_MODE": "compiled",
        "BATCH_RELATED_LOOKUPS": True,
    },
    "pydantic": {
        "SERIALIZER_BACKEND": "pydantic",
        "CACHE_SERIALIZER_FIELDS": True,
        "FIELD_COPY_MODE": "clone",
    },
    # Output compiled without msgspec or pydantic; input stays DRF's.
    "python": {
        "SERIALIZER_BACKEND": "python",
        "CACHE_SERIALIZER_FIELDS": True,
        "BATCH_RELATED_LOOKUPS": True,
    },
}


def serializers_for(module):
    class AuthorSerializer(module.ModelSerializer):
        class Meta:
            model = Author
            fields = ["id", "name"]

    class TagSerializer(module.ModelSerializer):
        class Meta:
            model = Tag
            fields = ["id", "name"]

    class BookSerializer(module.ModelSerializer):
        author_detail = AuthorSerializer(source="author", read_only=True)
        title = module.CharField(max_length=20)

        class Meta:
            model = Book
            fields = ["id", "title", "isbn", "pages", "author", "author_detail", "tags"]

        def validate_pages(self, value):
            if value == 13:
                raise module.ValidationError("Unlucky.")
            return value

    # The three below compile with strict parity.
    class PlainBookSerializer(module.ModelSerializer):
        class Meta:
            model = Book
            fields = ["id", "title", "isbn", "pages", "author"]

    class NestedBookSerializer(module.ModelSerializer):
        author = AuthorSerializer(read_only=True)
        tags = TagSerializer(many=True, read_only=True)

        class Meta:
            model = Book
            fields = ["id", "title", "pages", "author", "tags"]
            # fastdrf's QueryOptimizationMixin derives the lookups; DRF
            # ignores the option.
            auto_prefetch = True

    class EditionOutSerializer(module.ModelSerializer):
        class Meta:
            model = Edition
            fields = [
                "id",
                "code",
                "book",
                "translator",
                "released",
                "active",
                "rating",
                "format",
                "notes",
            ]

    # Every field type, for input: UUID, datetime, date, decimal, JSON, choices.
    class EditionSerializer(module.ModelSerializer):
        class Meta:
            model = Edition
            fields = "__all__"

    return {
        "books": BookSerializer,
        "plain-books": PlainBookSerializer,
        "nested-books": NestedBookSerializer,
        "editions": EditionSerializer,
        "edition-outputs": EditionOutSerializer,
    }


DRF_SERIALIZERS = serializers_for(drf_serializers)
FASTDRF_SERIALIZERS = serializers_for(fastdrf_serializers)
COMPILED = ("plain-books", "nested-books", "edition-outputs")


class Pages(pagination.PageNumberPagination):
    page_size = 2


class Limits(pagination.LimitOffsetPagination):
    default_limit = 2


class Cursor(pagination.CursorPagination):
    page_size = 2
    ordering = "id"


PAGINATORS = {"none": None, "pages": Pages, "limits": Limits, "cursor": Cursor}


class EditionFilter(django_filters.FilterSet):
    """A FilterSet of its own: ranges, a lookup across a relation, choices."""

    min_rating = django_filters.NumberFilter(field_name="rating", lookup_expr="gte")
    released_after = django_filters.DateFilter(field_name="released", lookup_expr="gt")
    book_title = django_filters.CharFilter(
        field_name="book__title", lookup_expr="icontains"
    )
    # ``?format=`` is DRF's renderer override.
    binding = django_filters.ChoiceFilter(
        field_name="format", choices=Edition._meta.get_field("format").choices
    )

    class Meta:
        model = Edition
        fields = ["active", "translator"]


def boom(self, request):
    raise RuntimeError("a view's own error")


class FastModelViewSet(
    DispatchOptimizationMixin,
    QueryOptimizationMixin,
    CreateModelMixin,
    UpdateModelMixin,
    drf_viewsets.ModelViewSet,
):
    renderer_classes = [JSONRenderer, renderers.BrowsableAPIRenderer]


class FastReadOnlyModelViewSet(
    DispatchOptimizationMixin, QueryOptimizationMixin, drf_viewsets.ReadOnlyModelViewSet
):
    renderer_classes = [JSONRenderer, renderers.BrowsableAPIRenderer]


def _data_response(response):
    """DRF's response of a generic action as a ``DataResponse``."""
    if response.status_code >= 400:
        return response
    headers = {
        name: value
        for name, value in response.items()
        if name.lower() != "content-type"
    }
    return DataResponse(response.data, status=response.status_code, headers=headers)


class DataReads:
    """Reads answered with ``DataResponse``; the view's own code."""

    def list(self, request, *args, **kwargs):
        return _data_response(super().list(request, *args, **kwargs))

    def retrieve(self, request, *args, **kwargs):
        return _data_response(super().retrieve(request, *args, **kwargs))


class DataModelViewSet(DataReads, FastModelViewSet):
    pass


class DataReadOnlyModelViewSet(DataReads, FastReadOnlyModelViewSet):
    pass


READ_ONLY = {
    drf_viewsets.ModelViewSet: drf_viewsets.ReadOnlyModelViewSet,
    FastModelViewSet: FastReadOnlyModelViewSet,
    DataModelViewSet: DataReadOnlyModelViewSet,
}


def queryset(resource):
    if "edition" in resource:
        return Edition.objects.order_by("id")
    return Book.objects.order_by("id")


def viewset(base, resource, serializer_class, paginator):
    attributes = {
        "queryset": queryset(resource),
        "serializer_class": serializer_class,
        "pagination_class": paginator,
        "permission_classes": [AllowAny],
        "authentication_classes": [],
    }
    if resource == "books":
        attributes |= {
            "filter_backends": [
                DjangoFilterBackend,
                filters.SearchFilter,
                filters.OrderingFilter,
            ],
            "filterset_fields": ["author", "pages", "tags"],
            "search_fields": ["title"],
            "ordering_fields": ["pages", "id"],
            "boom": action(detail=False)(boom),
        }
    if "edition" in resource:
        attributes |= {
            "filter_backends": [DjangoFilterBackend, filters.OrderingFilter],
            "filterset_class": EditionFilter,
            "ordering_fields": ["rating", "id"],
        }
    if resource == "edition-outputs":
        base = READ_ONLY[base]
    return type("Resource", (base,), attributes)


IMPLEMENTATIONS = {
    "drf": (drf_viewsets.ModelViewSet, DRF_SERIALIZERS),
    "fastdrf": (FastModelViewSet, FASTDRF_SERIALIZERS),
    "fastdrf-drf-serializer": (FastModelViewSet, DRF_SERIALIZERS),
    "fastdrf-data": (DataModelViewSet, FASTDRF_SERIALIZERS),
}

urlpatterns = []
for paginator_name, paginator in PAGINATORS.items():
    for name, (base, serializer_classes) in IMPLEMENTATIONS.items():
        router = routers.SimpleRouter()
        for resource, serializer_class in serializer_classes.items():
            router.register(
                resource, viewset(base, resource, serializer_class, paginator), resource
            )
        urlpatterns.append(path(f"{name}/{paginator_name}/", include(router.urls)))


def requests(author, other, tag, book, edition):
    body = {"title": "New", "isbn": "N1", "pages": 5, "author": author, "tags": [tag]}
    edition_body = {
        "code": str(uuid.UUID(int=9)),
        "book": book,
        "translator": other,
        "published": "2026-09-30T10:00:00Z",
        "released": "2026-09-30",
        "active": False,
        "rating": 4.5,
        "price": "12.50",
        "format": "hb",
        "extra": {"a": [1, None]},
        "notes": "n",
    }
    return [
        # Reads, pages, filters.
        ("get", "books/", None),
        ("get", "books/?page=2", None),
        ("get", "books/?page=9", None),
        ("get", "books/?page=x", None),
        ("get", "books/?limit=1&offset=1", None),
        ("get", "books/?limit=x", None),
        ("get", "books/?search=B1", None),
        ("get", "books/?ordering=-pages", None),
        ("get", "books/?ordering=nope", None),
        ("get", f"books/?author={author}", None),
        ("get", f"books/?author={other}", None),
        ("get", "books/?author=abc", None),  # 400
        ("get", "books/?author=999", None),  # 400: not a choice
        ("get", "books/?pages=11", None),
        ("get", "books/?pages=x", None),  # 400
        ("get", f"books/?tags={tag}", None),
        ("get", f"books/?author={author}&pages=12&ordering=-id", None),
        ("get", f"books/?author={author}&search=B&ordering=-pages", None),
        ("get", f"books/?author={author}&page=2", None),
        ("get", f"books/?author={author}&limit=1&offset=1", None),
        ("get", "books/?pages=x&page=2", None),
        ("get", "editions/?binding=pb", None),
        ("get", "editions/?binding=ebook", None),  # 400
        ("get", "editions/?format=pb", None),  # 404: DRF's renderer override
        ("get", "editions/?active=false", None),
        ("get", f"editions/?translator={other}", None),
        ("get", "editions/?min_rating=3", None),
        ("get", "editions/?min_rating=x", None),  # 400
        ("get", "editions/?released_after=2026-01-01", None),
        ("get", "editions/?released_after=nope", None),  # 400
        ("get", "editions/?book_title=b2&ordering=-rating", None),
        ("get", "edition-outputs/?binding=pb&ordering=-id", None),
        ("get", "books/?format=json", None),
        ("get", "books/?format=xml", None),
        ("head", "books/", None),
        ("options", "books/", None),
        ("options", f"books/{book}/", None),
        ("get", f"books/{book}/", None),
        ("get", "books/999/", None),
        ("get", "books/x/", None),
        ("get", "books/boom/", None),
        ("get", "plain-books/", None),
        ("get", f"plain-books/{book}/", None),
        ("get", "nested-books/", None),
        ("get", f"nested-books/{book}/", None),
        ("get", "editions/", None),
        ("get", f"editions/{edition}/", None),
        ("get", "edition-outputs/", None),
        ("get", f"edition-outputs/{edition}/", None),
        ("post", "edition-outputs/", {}),  # 405
        # Book writes.
        ("post", "books/", body),
        ("post", "books/", {**body, "isbn": "I0"}),  # unique
        ("post", "books/", {**body, "title": "x" * 21}),
        ("post", "books/", {**body, "title": ""}),
        ("post", "books/", {**body, "title": None}),
        ("post", "books/", {**body, "pages": 13}),
        ("post", "books/", {**body, "pages": -1}),
        ("post", "books/", {**body, "pages": "many"}),
        ("post", "books/", {**body, "pages": 1.5}),
        ("post", "books/", {**body, "pages": True}),
        ("post", "books/", {**body, "author": 999}),
        ("post", "books/", {**body, "author": None}),
        ("post", "books/", {**body, "author": "abc"}),
        ("post", "books/", {**body, "tags": [999]}),
        ("post", "books/", {**body, "tags": [tag, tag]}),
        ("post", "books/", {**body, "tags": "not a list"}),
        ("post", "books/", {key: v for key, v in body.items() if key != "isbn"}),
        ("post", "books/", {**body, "unknown": 1}),
        ("post", "books/", {}),
        ("post", "books/", [body]),
        ("post", "books/", "text"),
        (
            "post",
            "plain-books/",
            {key: body[key] for key in ("title", "isbn", "author")},
        ),
        ("post", "nested-books/", {"title": "N", "pages": 3}),
        ("put", f"books/{book}/", {**body, "isbn": "P1", "author": other}),
        ("put", f"books/{book}/", {"title": "Only"}),
        ("patch", f"books/{book}/", {"pages": 7}),
        ("patch", f"books/{book}/", {"pages": 13}),
        ("patch", f"books/{book}/", {"tags": []}),
        ("patch", f"books/{book}/", {"isbn": "I0"}),
        ("patch", "books/999/", {"pages": 7}),
        ("put", f"plain-books/{book}/", {"title": "P", "isbn": "P2", "author": other}),
        ("patch", f"plain-books/{book}/", {"pages": 8}),
        ("delete", f"books/{book}/", None),
        ("delete", "books/999/", None),
        ("put", "books/", body),  # 405
        # Edition writes: every field type.
        ("post", "editions/", edition_body),
        ("post", "editions/", {**edition_body, "code": "not a uuid"}),
        ("post", "editions/", {**edition_body, "published": "yesterday"}),
        ("post", "editions/", {**edition_body, "published": "2026-09-30T10:00:00"}),
        ("post", "editions/", {**edition_body, "released": "2026-13-01"}),
        ("post", "editions/", {**edition_body, "released": None}),
        ("post", "editions/", {**edition_body, "rating": "nan"}),
        ("post", "editions/", {**edition_body, "rating": None}),
        ("post", "editions/", {**edition_body, "price": "1.234"}),
        ("post", "editions/", {**edition_body, "price": "12345.00"}),
        ("post", "editions/", {**edition_body, "price": 3}),
        ("post", "editions/", {**edition_body, "format": "ebook"}),
        ("post", "editions/", {**edition_body, "translator": None}),
        ("post", "editions/", {**edition_body, "active": "yes"}),
        ("post", "editions/", {**edition_body, "extra": "not json object"}),
        ("patch", f"editions/{edition}/", {"notes": "changed", "rating": 1}),
        ("put", f"editions/{edition}/", edition_body),
        ("delete", f"editions/{edition}/", None),
    ]


def snapshot():
    return {
        "books": list(Book.objects.order_by("id").values()),
        "authors": list(Author.objects.order_by("id").values()),
        "tags": list(Book.tags.through.objects.order_by("id").values()),
        "editions": list(Edition.objects.order_by("id").values()),
    }


def described(response, prefix):
    # Pagination links and Location name the implementation's own URL.
    content = response.content.replace(prefix.encode(), b"/")
    headers = {name: value.replace(prefix, "/") for name, value in response.items()}
    if "Content-Length" in headers:
        headers["Content-Length"] = str(len(content))
    return {"status": response.status_code, "headers": headers, "content": content}


def observe(client, method, prefix, url, data):
    with transaction.atomic():
        try:
            # A savepoint: a database error leaves the snapshot possible.
            with transaction.atomic():
                response = getattr(client, method)(prefix + url, data, format="json")
        except Exception as exc:  # noqa: BLE001 -- the view's own error, compared
            result = {"raised": (type(exc), str(exc))}
        else:
            result = described(response, prefix)
        result["database"] = snapshot()
        transaction.set_rollback(True)
    return result


def fixtures():
    authors = [Author.objects.create(name=name) for name in ("Ann", "Bo")]
    tags = [Tag.objects.create(name=name) for name in ("a", "b")]
    for index in range(3):
        book = Book.objects.create(
            title=f"B{index}", isbn=f"I{index}", pages=10 + index, author=authors[0]
        )
        book.tags.set(tags[: index % 3])
    edition = None
    for index, (released, rating, translator) in enumerate(
        [(datetime.date(2026, 1, 2), 3.5, authors[1]), (None, None, None)]
    ):
        edition = Edition.objects.create(
            code=uuid.UUID(int=index + 1),
            book=book,
            translator=translator,
            published=datetime.datetime(2026, 9, 30, 8, tzinfo=datetime.UTC),
            released=released,
            rating=rating,
            price="1.50",
            format="pb",
            extra={"k": index},
        )
    return requests(authors[0].pk, authors[1].pk, tags[0].pk, book.pk, edition.pk)


def compiled_encoders():
    """The resources of ``COMPILED`` whose fastdrf serializer has an encoder."""
    return {
        resource
        for resource in COMPILED
        if any(
            isinstance(value, compiler.Encoder)
            for value in (
                compiler._compiled_by_class.get(FASTDRF_SERIALIZERS[resource]) or {}
            ).values()
        )
    }


@pytest.mark.django_db
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("paginator", PAGINATORS)
@override_settings(ROOT_URLCONF=__name__)
def test_fastdrf_answers_as_drf(profile, paginator):
    client = APIClient()
    compared = 0
    with override_settings(FASTDRF=PROFILES[profile]):
        cases = fixtures()
        for method, url, data in cases:
            reference = observe(client, method, f"/drf/{paginator}/", url, data)
            for name in IMPLEMENTATIONS.keys() - {"drf"}:
                ours = observe(client, method, f"/{name}/{paginator}/", url, data)
                assert ours == reference, (name, method, url, data)
                compared += 1
        if profile != "default":
            assert compiled_encoders() == set(COMPILED)
    assert compared == (len(IMPLEMENTATIONS) - 1) * len(cases)
