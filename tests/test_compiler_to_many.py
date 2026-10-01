"""
Nested ``many=True`` serializers on related managers, compiled.

DRF's ``ListSerializer`` represents ``manager.all()``: the prefetched objects
when there are any, a query otherwise. The compiled class must represent the
same objects in the same order, with the same queries, in the same thread.
"""

import datetime
import uuid

import pytest
from django.db import connection, transaction
from django.db.models import Prefetch
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fastdrf import compiler
from fastdrf import serializers as drf_serializers
from tests.models import Author, Book, Edition, Tag


def _data(serializer):
    return serializer.data


BACKENDS = ["msgspec", "pydantic", "python"]


class TagOut(drf_serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name"]


class BookTags(drf_serializers.ModelSerializer):
    # A forward many-to-many field.
    tags = TagOut(many=True, read_only=True)

    class Meta:
        model = Book
        fields = ["id", "title", "tags"]


class BookOut(drf_serializers.ModelSerializer):
    class Meta:
        model = Book
        fields = ["id", "title"]


class AuthorBooks(drf_serializers.ModelSerializer):
    # A reverse foreign key, with its tags nested once more.
    books = BookTags(many=True, read_only=True)

    class Meta:
        model = Author
        fields = ["id", "name", "books"]


class TagBooks(drf_serializers.ModelSerializer):
    # The reverse side of a many-to-many field, under another key.
    titles = BookOut(source="books", many=True, read_only=True)

    class Meta:
        model = Tag
        fields = ["id", "titles"]


@pytest.fixture
def library(db):
    ada, ursula = (
        Author.objects.create(name="Ada"),
        Author.objects.create(name="Ursula"),
    )
    red, blue, green = (
        Tag.objects.create(name=name) for name in ("red", "blue", "green")
    )
    first = Book.objects.create(title="First", isbn="1", author=ada)
    second = Book.objects.create(title="Second", isbn="2", author=ada)
    Book.objects.create(title="Untagged", isbn="3", author=ada)
    first.tags.set([green, red, blue])
    second.tags.set([blue])
    return ada, ursula


def both(serializer_factory, backend, parity="strict"):
    """DRF's output and the compiled one, and the queries each made."""
    with CaptureQueriesContext(connection) as drf_queries:
        drf = serializer_factory().data
    settings = {
        "SERIALIZER_BACKEND": backend,
        "SERIALIZER_BACKEND_PARITY": parity,
        "SERIALIZER_BACKEND_FALLBACK": "error",
    }
    with (
        override_settings(FASTDRF=settings),
        CaptureQueriesContext(connection) as queries,
    ):
        compiled = _data(serializer_factory())
    return drf, compiled, len(drf_queries), len(queries)


QUERYSETS = {
    "books": lambda: Book.objects.order_by("id"),
    "books, tags prefetched": lambda: Book.objects.prefetch_related("tags"),
    "books, tags prefetched in another order": lambda: Book.objects.prefetch_related(
        Prefetch("tags", queryset=Tag.objects.order_by("-name"))
    ),
}


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("queryset", QUERYSETS)
def test_forward_many_to_many(library, backend, queryset):
    drf, compiled, drf_queries, queries = both(
        lambda: BookTags(QUERYSETS[queryset](), many=True), backend
    )
    assert compiled == drf
    assert [len(book["tags"]) for book in compiled] == [3, 1, 0]
    assert queries == drf_queries


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("prefetch", [(), ("books",), ("books", "books__tags")])
def test_reverse_foreign_key_nested_twice(library, backend, prefetch):
    def factory():
        return AuthorBooks(
            Author.objects.prefetch_related(*prefetch).order_by("id"), many=True
        )

    drf, compiled, drf_queries, queries = both(factory, backend)
    assert compiled == drf
    assert compiled[1] == {"id": library[1].pk, "name": "Ursula", "books": []}
    assert queries == drf_queries
    # One object, not a list of them.
    drf, compiled, drf_queries, queries = both(lambda: AuthorBooks(library[0]), backend)
    assert compiled == drf
    assert queries == drf_queries


@pytest.mark.parametrize("backend", BACKENDS)
def test_reverse_many_to_many_under_another_key(library, backend):
    drf, compiled, *_ = both(
        lambda: TagBooks(Tag.objects.order_by("id"), many=True), backend
    )
    assert compiled == drf
    assert [len(tag["titles"]) for tag in compiled] == [1, 2, 1]


@pytest.mark.parametrize("backend", BACKENDS)
@settings(
    derandomize=True,
    database=None,
    deadline=None,
    max_examples=40,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    shelves=st.lists(
        st.lists(st.lists(st.integers(0, 3), max_size=4), max_size=3),
        min_size=1,
        max_size=3,
    ),
    prefetch=st.sampled_from([(), ("books",), ("books__tags",)]),
)
def test_generated_relations_match_drf(db, backend, shelves, prefetch):
    # Authors with books with tags, each in a generated order and number.
    with transaction.atomic():
        tags = [Tag.objects.create(name=f"t{n}") for n in range(4)]
        for books in shelves:
            author = Author.objects.create(name="a")
            for picked in books:
                book = Book.objects.create(
                    title="b", isbn=str(Book.objects.count()), author=author
                )
                book.tags.set([tags[n] for n in picked])

        def factory():
            return AuthorBooks(Author.objects.prefetch_related(*prefetch), many=True)

        drf, compiled, drf_queries, queries = both(factory, backend)
        assert compiled == drf
        assert queries == drf_queries
        transaction.set_rollback(True)


class AuthorOut(drf_serializers.ModelSerializer):
    class Meta:
        model = Author
        fields = ["id", "name"]


def book_detail(edition_fields):
    """A detail view's shape: a nested object and two nested lists."""

    class EditionOut(drf_serializers.ModelSerializer):
        class Meta:
            model = Edition
            fields = edition_fields

    class BookDetail(drf_serializers.ModelSerializer):
        author = AuthorOut(read_only=True)
        tags = TagOut(many=True, read_only=True)
        editions = EditionOut(many=True, read_only=True)

        class Meta:
            model = Book
            fields = ["id", "title", "author", "tags", "editions"]

    return BookDetail


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("parity", "edition_fields"),
    [
        ("strict", ["id", "book_id", "notes"]),
        ("fast", ["id", "book_id", "notes", "published"]),
    ],
)
@pytest.mark.parametrize("prefetch", [(), ("tags", "editions")])
def test_a_detail_with_a_reverse_foreign_keys_column(
    library, backend, parity, edition_fields, prefetch
):
    book = Book.objects.get(title="First")
    published = datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC)
    for notes in ("hardback", "paperback"):
        Edition.objects.create(
            code=uuid.uuid4(),
            book=book,
            published=published,
            price=1,
            format="hb",
            notes=notes,
        )
    BookDetail = book_detail(edition_fields)

    def factory():
        return BookDetail(
            Book.objects.select_related("author")
            .prefetch_related(*prefetch)
            .get(pk=book.pk)
        )

    drf, compiled, drf_queries, queries = both(factory, backend, parity)
    assert compiled == drf
    assert [edition["book_id"] for edition in compiled["editions"]] == [
        book.pk,
        book.pk,
    ]
    assert queries == drf_queries


@pytest.mark.parametrize("backend", BACKENDS)
def test_edits_of_a_nested_list_are_respected(library, backend):
    def popped():
        serializer = BookTags(Book.objects.all(), many=True)
        serializer.child.fields["tags"].child.fields.pop("name")
        return serializer

    def assigned():
        serializer = BookTags(Book.objects.all(), many=True)
        serializer.child.fields["tags"].child.to_representation = lambda tag: tag.name
        return serializer

    drf, compiled, *_ = both(lambda: BookTags(Book.objects.all(), many=True), backend)
    assert compiled == drf
    drf, compiled, *_ = both(popped, backend)
    assert compiled == drf
    assert compiled[0]["tags"][0].keys() == {"id"}
    drf = assigned().data
    with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
        assert _data(assigned()) == drf
    assert sorted(drf[0]["tags"]) == ["blue", "green", "red"]


class SortedTags(drf_serializers.ModelSerializer):
    # A list the prefetch set; absent when the queryset did not prefetch it.
    tags = TagOut(source="sorted_tags", many=True, read_only=True)

    class Meta:
        model = Book
        fields = ["id", "tags"]


class TagsProperty(drf_serializers.ModelSerializer):
    tags = TagOut(source="tag_list", many=True, read_only=True)

    class Meta:
        model = Book
        fields = ["id", "tags"]


class Named(drf_serializers.ModelSerializer):
    label = drf_serializers.SerializerMethodField()

    class Meta:
        model = Tag
        fields = ["id", "label"]

    def get_label(self, tag):
        return tag.name.upper()


class NamedTags(drf_serializers.ModelSerializer):
    tags = Named(many=True, read_only=True)

    class Meta:
        model = Book
        fields = ["id", "tags"]


class FirstOnly(drf_serializers.ListSerializer):
    def to_representation(self, data):
        return super().to_representation(data.all()[:1])


class FirstTag(TagOut):
    class Meta(TagOut.Meta):
        list_serializer_class = FirstOnly


class BookFirstTag(drf_serializers.ModelSerializer):
    tags = FirstTag(many=True, read_only=True)

    class Meta:
        model = Book
        fields = ["id", "tags"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_what_is_not_a_related_manager_stays_on_drf(library, backend):
    sorted_tags = Prefetch(
        "tags", queryset=Tag.objects.order_by("name"), to_attr="sorted_tags"
    )
    listed = list(Book.objects.all())
    for book in listed:
        book.tag_list = list(book.tags.all())
    cases = [
        (
            SortedTags,
            Book.objects.prefetch_related(sorted_tags),
            "is not a to-many relation",
        ),
        (TagsProperty, listed, "is not a to-many relation"),
        (NamedTags, Book.objects.all(), "Named.label has source='*'"),
        (BookFirstTag, Book.objects.all(), "overrides to_representation"),
    ]
    for serializer_class, queryset, reason in cases:
        assert reason in compiler.report(serializer_class(queryset, many=True))
        drf = serializer_class(queryset, many=True).data
        with override_settings(FASTDRF={"SERIALIZER_BACKEND": backend}):
            compiled = _data(serializer_class(queryset, many=True))
        assert compiled == drf


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("backend", BACKENDS)
def test_related_managers_are_queried_where_drf_would_query(backend):
    Author.objects.create(name="Ada")
    book = Book.objects.create(title="First", isbn="1", author=Author.objects.get())
    tag = Tag.objects.create(name="red")
    book.tags.add(tag)
    settings = {"SERIALIZER_BACKEND": backend, "SERIALIZER_BACKEND_FALLBACK": "error"}
    with override_settings(FASTDRF=settings):
        data = AuthorBooks(Author.objects.all(), many=True).data
    tags = [{"id": tag.pk, "name": "red"}]
    assert data[0]["books"] == [{"id": book.pk, "title": "First", "tags": tags}]
