"""``PrefetchListSerializer``: one enrichment step per list, then DRF's representation."""

import pytest
from django.test import override_settings
from rest_framework import generics
from rest_framework import serializers as drf
from rest_framework.exceptions import APIException
from rest_framework.pagination import PageNumberPagination
from rest_framework.test import APIRequestFactory

from fastdrf import serializers
from fastdrf.list_serializers import PrefetchListSerializer
from tests.models import Author, Book

pytestmark = pytest.mark.django_db

prefetches = []


class RatedAuthors(PrefetchListSerializer):
    prefetch_related = ["books"]

    def prefetch(self, instances):
        # One call for all items, e.g. a bulk HTTP request.
        prefetches.append([author.name for author in instances])
        for author in instances:
            author.rating = len(author.name)


class AuthorSerializer(serializers.ModelSerializer):
    rating = drf.IntegerField(read_only=True)
    titles = drf.SerializerMethodField()

    class Meta:
        model = Author
        fields = ["name", "rating", "titles"]
        list_serializer_class = RatedAuthors

    def get_titles(self, author):
        # Reads the prefetched relation: no query per author.
        return [book.title for book in author.books.all()]


class PlainAuthorSerializer(AuthorSerializer):
    rating = drf.SerializerMethodField()

    class Meta(AuthorSerializer.Meta):
        list_serializer_class = serializers.ListSerializer

    def get_rating(self, author):
        return len(author.name)


class Pages(PageNumberPagination):
    page_size = 2


class AuthorList(generics.ListAPIView):
    authentication_classes = []
    permission_classes = []
    serializer_class = AuthorSerializer
    queryset = Author.objects.order_by("name")


@pytest.fixture(autouse=True)
def library():
    prefetches.clear()
    ada = Author.objects.create(name="Ada")
    Author.objects.create(name="Bo")
    Author.objects.create(name="Cyd")
    Book.objects.create(title="Notes", isbn="1", author=ada)
    Book.objects.create(title="Letters", isbn="2", author=ada)


def get(view):
    response = view(APIRequestFactory().get("/"))
    response.render()
    return response


EXPECTED = [
    {"name": "Ada", "rating": 3, "titles": ["Notes", "Letters"]},
    {"name": "Bo", "rating": 2, "titles": []},
    {"name": "Cyd", "rating": 3, "titles": []},
]


def test_one_prefetch_per_list_and_items_in_order(django_assert_num_queries):
    # The authors, then their books for all of them.
    with django_assert_num_queries(2):
        response = get(AuthorList.as_view())
    assert response.status_code == 200
    assert response.data == EXPECTED
    assert prefetches == [["Ada", "Bo", "Cyd"]]


def test_the_representation_is_drfs(django_assert_num_queries):
    queryset = Author.objects.order_by("name")
    # Without the list step: one query for the authors, one per author.
    with django_assert_num_queries(4):
        plain = PlainAuthorSerializer(queryset.all(), many=True).data
    with django_assert_num_queries(2):
        enriched = AuthorSerializer(queryset.all(), many=True).data
    assert enriched == plain == EXPECTED
    assert type(enriched) is type(plain)


def test_a_page_is_prefetched_not_the_whole_queryset():
    response = get(AuthorList.as_view(pagination_class=Pages))
    assert [row["name"] for row in response.data["results"]] == ["Ada", "Bo"]
    assert prefetches == [["Ada", "Bo"]]


def test_relations_are_prefetched_before_prefetch_runs():
    seen = []

    class Checking(RatedAuthors):
        def prefetch(self, instances):
            seen.extend(
                "books" in author._prefetched_objects_cache for author in instances
            )
            super().prefetch(instances)

    class CheckingSerializer(AuthorSerializer):
        class Meta(AuthorSerializer.Meta):
            list_serializer_class = Checking

    data = CheckingSerializer(Author.objects.order_by("name"), many=True).data
    assert data[0]["titles"] == ["Notes", "Letters"]
    assert seen == [True, True, True]


def test_an_error_while_prefetching_is_drfs_error():
    class Unavailable(APIException):
        status_code = 503
        default_detail = "Rating service unavailable."

    class Failing(RatedAuthors):
        def prefetch(self, instances):
            raise Unavailable

    class FailingSerializer(AuthorSerializer):
        class Meta(AuthorSerializer.Meta):
            list_serializer_class = Failing

    response = get(AuthorList.as_view(serializer_class=FailingSerializer))
    assert response.status_code == 503
    assert response.data == {"detail": "Rating service unavailable."}


def test_a_nested_relation_is_enriched_once_per_parent(django_assert_num_queries):
    calls = []

    class Titles(PrefetchListSerializer):
        def prefetch(self, instances):
            calls.append([book.title for book in instances])

    class BookSerializer(serializers.ModelSerializer):
        class Meta:
            model = Book
            fields = ["title"]
            list_serializer_class = Titles

    class WithBooks(serializers.ModelSerializer):
        books = BookSerializer(many=True)

        class Meta:
            model = Author
            fields = ["name", "books"]

    ada = Author.objects.get(name="Ada")
    # The author is loaded; its manager is read once.
    with django_assert_num_queries(1):
        data = WithBooks(ada).data
    assert data == {"name": "Ada", "books": [{"title": "Notes"}, {"title": "Letters"}]}
    assert calls == [["Notes", "Letters"]]


@override_settings(
    FASTDRF={"SERIALIZER_BACKEND": "msgspec", "SERIALIZER_BACKEND_FALLBACK": "drf"}
)
def test_a_compiling_backend_still_enriches_the_list():
    pytest.importorskip("msgspec")
    data = AuthorSerializer(Author.objects.order_by("name"), many=True).data
    assert data == EXPECTED
    assert prefetches == [["Ada", "Bo", "Cyd"]]


def test_exact_lists_are_not_copied():
    received = []

    class Values(PrefetchListSerializer):
        def prefetch(self, instances):
            received.append(instances)

    class ValueSerializer(drf.Serializer):
        value = drf.IntegerField()

        class Meta:
            list_serializer_class = Values

    items = [{"value": 3}]
    assert ValueSerializer(items, many=True).data == [{"value": 3}]
    assert received[0] is items
    assert ValueSerializer(iter([{"value": 4}]), many=True).data == [{"value": 4}]
    assert received[1] == [{"value": 4}]
